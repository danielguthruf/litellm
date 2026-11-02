"""
The deployment outcome callbacks ``RustCore`` serves as members: ``deployment_callback_on_failure``,
``deployment_callback_on_success`` and ``sync_deployment_callback_on_success``. router-core counts
the outcome and decides the cooldown; this module reads litellm's logging kwargs, drops the
failures the Python router does not hold against the deployment, and drives the Rust call. The
stream wrappers report a stream's outcome through the same members when the stream ends.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from litellm.litellm_core_utils.core_helpers import as_str_mapping

if TYPE_CHECKING:
    from litellm.router_rust.steps.callbacks import OptionalChecks
    from litellm.router_rust.surface.registry import HostRegistry
    from litellm.router_utils.cooldown_cache import CooldownCache
    from litellm.rust_bridge._native import (
        RouterCall,
        RouterCoreHandle,
        _FailureReportSteps,  # pyright: ignore[reportPrivateUsage]  # the stub's step shapes
        _SuccessReportSteps,  # pyright: ignore[reportPrivateUsage]  # see above
    )


def _cast_status(exception_status: object) -> int | None:
    if isinstance(exception_status, int) and not isinstance(exception_status, bool):
        return exception_status
    if isinstance(exception_status, str):
        try:
            return int(exception_status)
        except ValueError:
            return 500
    return None


def failure_status(exception_status: object) -> int | None:
    """``cast_exception_status_to_int``: a number as given, a numeric string read as one, any
    other string 500. A number no status can hold reads as no status."""
    status: Final = _cast_status(exception_status)
    return status if status is not None and 0 <= status <= 65535 else None


def cooled_in_running_loop(cooled: bool) -> bool:
    """``set_cooldown_deployments`` schedules the cooldown event with ``asyncio.create_task``
    after writing the cooldown, so outside a running loop it raises ``RuntimeError`` then."""
    import asyncio

    if cooled:
        asyncio.get_running_loop()
    return cooled


def _configured_cooldown(model_info: object, litellm_params: Mapping[str, object]) -> float | None:
    """``_first_present(model_info, litellm_params, key="cooldown_time")``."""
    for source in (as_str_mapping(model_info), litellm_params):
        value = source.get("cooldown_time") if source is not None else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None


def _success_target(kwargs: Mapping[str, object]) -> tuple[str, Mapping[str, object]] | None:
    """The deployment id and metadata a success callback counts under, or None."""
    params: Final = as_str_mapping(kwargs.get("litellm_params")) or {}
    metadata: Final = as_str_mapping(params.get("metadata"))
    if metadata is None:
        return None
    model_info: Final = as_str_mapping(params.get("model_info")) or {}
    deployment_id: Final = model_info.get("id")
    if metadata.get("model_group") is None or deployment_id is None:
        return None
    return str(deployment_id), metadata


class RustHealthCallbacks:
    """Outcome callbacks read into the Rust core's health state."""

    _handle: "RouterCoreHandle"

    def _drive_sync_steps(self, call: "RouterCall[_FailureReportSteps | _SuccessReportSteps]") -> Mapping[str, object]:
        raise NotImplementedError

    async def _drive_cache_steps(
        self, call: "RouterCall[_FailureReportSteps | _SuccessReportSteps]"
    ) -> Mapping[str, object]:
        raise NotImplementedError

    def _core_get_allowed_fails_from_policy(self, exception: Exception) -> int | None:
        """The router ``allowed_fails_policy``'s allowance for this exception, decided by router-core."""
        from litellm.router_rust.core import attempt_failure

        return self._handle.allowed_fails_for(dict(attempt_failure(exception)))

    def deployment_callback_on_failure(
        self,
        kwargs: Mapping[str, object],
        completion_response: object,
        start_time: object,
        end_time: object,
    ) -> bool:
        """Count the failure and cool the deployment down when the rules say so."""
        call: Final = self._failure_call(kwargs)
        return call is not None and cooled_in_running_loop(bool(self._drive_sync_steps(call).get("cooled")))

    async def report_stream_failure(self, kwargs: Mapping[str, object]) -> None:
        """A stream's failure after its attempt returned it, as an async call's failure callbacks
        record it: counted, cooled down by the rules, and one more request this minute
        (``async_deployment_callback_on_failure``)."""
        call: Final = self._failure_call(kwargs, counts_request=True)
        if call is not None:
            _ = await self._drive_cache_steps(call)

    def report_stream_failure_sync(self, kwargs: Mapping[str, object]) -> None:
        """``report_stream_failure`` for a sync stream: a sync call's failure callback counts no request."""
        call: Final = self._failure_call(kwargs)
        if call is not None:
            _ = self._drive_sync_steps(call)

    def _failure_call(
        self, kwargs: Mapping[str, object], counts_request: bool = False
    ) -> "RouterCall[_FailureReportSteps] | None":
        """The Rust call recording a logged failure, or None for one the Python router does not hold
        against the deployment."""
        from litellm.litellm_core_utils import core_helpers
        from litellm.router_rust.core import attempt_failure
        from litellm.router_utils.cooldown_handlers import (
            is_background_response_poll_not_found,
            is_caller_scoped_auth_failure,
            is_caller_timeout_408,
            is_failure_outside_selected_deployment,
            is_video_lookup_not_found,
        )
        from litellm.types.router import Deployment

        exception: Final = kwargs.get("exception")
        if is_failure_outside_selected_deployment(exception if isinstance(exception, BaseException) else None):
            return None
        litellm_params: Final = as_str_mapping(kwargs.get("litellm_params")) or {}
        model_info: Final = litellm_params.get("model_info", {})
        if isinstance(exception, Exception) and is_background_response_poll_not_found(exception, litellm_params):
            return None
        if is_video_lookup_not_found(exception, kwargs):
            return None
        exception_status: Final[object] = getattr(exception, "status_code", "")
        if is_caller_timeout_408(kwargs, exception_status):  # pyright: ignore[reportArgumentType]  # the status as raised
            return None
        info: Final = as_str_mapping(model_info)
        if info is None:
            return None
        deployment_id: Final = info.get("id")
        if isinstance(deployment_id, str):
            record: Final = self._handle.deployment(deployment_id)
            deployment: Final = Deployment(**dict(record)) if record is not None else None  # pyright: ignore[reportArgumentType]  # a model_list entry
            if is_caller_scoped_auth_failure(deployment, exception_status):  # pyright: ignore[reportArgumentType]  # see above
                return None
        if deployment_id is None:
            return None
        described: Final[Mapping[str, object]] = (
            attempt_failure(exception) if isinstance(exception, BaseException) else {"ok": False, "kind": "APIError"}
        )
        failure: Final = {**described, "status": failure_status(exception_status)}
        metadata: Final[object] = core_helpers.get_litellm_metadata_from_kwargs(dict(kwargs))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # an untyped helper's dict
        group: Final = (as_str_mapping(metadata) or {}).get("model_group")  # pyright: ignore[reportUnknownArgumentType]  # read as a mapping
        return self._handle.report_failure(
            str(deployment_id),
            failure,
            deployment_cooldown=True,
            count_only=exception_status == "",
            requested_group=group if isinstance(group, str) else None,
            cooldown_time=_configured_cooldown(model_info, litellm_params),
            counts_request=counts_request,
        )

    def sync_deployment_callback_on_success(
        self,
        kwargs: Mapping[str, object],
        completion_response: object,
        start_time: object,
        end_time: object,
    ) -> str | None:
        """Count a success this minute; returns the counter's key."""
        from litellm.litellm_core_utils.batch_utils import is_batch_retrieve_call_type

        if is_batch_retrieve_call_type(kwargs.get("call_type")):
            return None
        target: Final = _success_target(kwargs)
        if target is None:
            return None
        key: Final = self._drive_sync_steps(self._handle.count_success(target[0])).get("key")
        return key if isinstance(key, str) else None

    async def repin_after_success(self, kwargs: Mapping[str, object], completion_response: object) -> None:
        """The session re-pin ``deployment_callback_on_success`` runs for a success logged against a
        deployment the router serves (``_repin_session_after_success``). The Rust core runs it from the
        attempt itself, or from the stream wrapper when a stream ends."""
        from litellm.litellm_core_utils.batch_utils import is_batch_retrieve_call_type

        call_type: Final = kwargs.get("call_type")
        if call_type == "_arealtime" or is_batch_retrieve_call_type(call_type):
            return
        target: Final = _success_target(kwargs)
        # RustCore sets these as attributes in its constructor.
        hosted: Final[HostRegistry] = self.__dict__["_hosted"]  # pyright: ignore[reportAny]  # RustCore's own attribute
        if target is None or hosted._core_get_deployment(model_id=target[0]) is None:  # pyright: ignore[reportPrivateUsage]  # the host registry's id lookup
            return
        checks: Final[OptionalChecks] = self.__dict__["_checks"]  # pyright: ignore[reportAny]  # see above
        cooldowns: Final[CooldownCache] = self.__dict__["_cooldowns"]  # pyright: ignore[reportAny]  # see above
        await checks.repin_session_after_success(
            as_str_mapping(kwargs.get("litellm_params")) or {}, completion_response, cooldowns
        )

    async def deployment_callback_on_success(
        self,
        kwargs: Mapping[str, object],
        completion_response: object,
        start_time: object,
        end_time: object,
    ) -> None:
        """Count a success and the call's tokens and request against a tracked deployment."""
        from litellm.litellm_core_utils.batch_utils import is_batch_retrieve_call_type
        from litellm.router_utils.router_callbacks.track_deployment_metrics import get_counted_usage_tokens

        call_type: Final = kwargs.get("call_type")
        if call_type == "_arealtime" or is_batch_retrieve_call_type(call_type):
            return
        logged: Final = as_str_mapping(kwargs.get("standard_logging_object"))
        if logged is None:
            return
        target: Final = _success_target(kwargs)
        if target is None:
            return
        await self.repin_after_success(kwargs, completion_response)
        tokens: Final[object] = logged.get("total_tokens", 0)
        total: Final = float(tokens) if isinstance(tokens, int | float) else 0.0
        counted: Final = get_counted_usage_tokens(as_str_mapping(kwargs.get("litellm_params")) or {})
        await self._drive_cache_steps(
            self._handle.report_success(
                target[0],
                total if counted is None else max(0.0, total - counted),
                1.0 if counted is None else 0.0,
            )
        )


__all__ = [
    "RustHealthCallbacks",
    "cooled_in_running_loop",
    "failure_status",
]
