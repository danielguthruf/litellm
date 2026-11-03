import os
from collections.abc import Iterator
from pathlib import Path
from typing import Final
from uuid import uuid4

import psycopg
import pytest
from litellm_proxy_extras.utils import ProxyExtrasDBManager
from psycopg import sql

_MIGRATIONS: Final = Path(__file__).parents[4] / "litellm-proxy-extras/litellm_proxy_extras/migrations"
_CREATE: Final = _MIGRATIONS / "20261027000000_add_accounting_ledger_tables/migration.sql"
_DROP: Final = _MIGRATIONS / "20261103030000_drop_accounting_event_tables/migration.sql"
_OLD_OBJECTS: Final = (
    "LiteLLM_AccountingEvent",
    "LiteLLM_AccountingDelivery",
    "AccountingEventKind",
    "AccountingDeliveryTarget",
)


@pytest.fixture
def schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, psycopg.Connection[tuple[object, ...]]]]:
    base: Final = os.environ["DATABASE_URL"].split("?")[0]
    name: Final = f"accounting_drop_{uuid4().hex}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
        _apply(connection, _CREATE)
        monkeypatch.setenv("DATABASE_URL", f"{base}?schema={name}")
        try:
            yield name, connection
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _apply(connection: psycopg.Connection[tuple[object, ...]], migration: Path) -> None:
    with connection.transaction():
        connection.execute(migration.read_bytes())


def _deliver(connection: psycopg.Connection[tuple[object, ...]], state: str) -> None:
    connection.execute(
        'INSERT INTO "LiteLLM_AccountingDelivery" (event_id, target, version, state) '
        "VALUES (%s, 'LiteLLM_VerificationToken', 1, %s::\"AccountingDeliveryState\")",
        (uuid4().hex, state),
    )


def _remaining(connection: psycopg.Connection[tuple[object, ...]], schema: str) -> tuple[object, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND relname = ANY(%s) "
            "UNION ALL SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
            "WHERE n.nspname=%s AND t.typtype = 'e' AND typname = ANY(%s)",
            (schema, list(_OLD_OBJECTS), schema, list(_OLD_OBJECTS)),
        ).fetchall()
    )


@pytest.mark.parametrize("state", ["pending", "parked"])
def test_unsettled_deliveries_block_both_the_migration_and_schema_push(
    schema: tuple[str, psycopg.Connection[tuple[object, ...]]], state: str
) -> None:
    name, connection = schema
    _deliver(connection, "applied")
    _deliver(connection, state)

    with pytest.raises(RuntimeError, match="pending or parked rows"):
        ProxyExtrasDBManager.raise_if_accounting_deliveries_unsettled()
    with pytest.raises(psycopg.errors.RaiseException, match="pending or parked rows"):
        _apply(connection, _DROP)
    assert sorted(_remaining(connection, name)) == sorted(_OLD_OBJECTS)


def test_settled_deliveries_let_the_migration_drop_every_old_object_and_rerun(
    schema: tuple[str, psycopg.Connection[tuple[object, ...]]],
) -> None:
    name, connection = schema
    _deliver(connection, "applied")

    assert ProxyExtrasDBManager.raise_if_accounting_deliveries_unsettled() is None
    _apply(connection, _DROP)
    _apply(connection, _DROP)

    assert _remaining(connection, name) == ()
    assert connection.execute(
        "SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
        "WHERE n.nspname=%s AND typname='AccountingDeliveryState'",
        (name,),
    ).fetchone() == (1,)
    assert ProxyExtrasDBManager.raise_if_accounting_deliveries_unsettled() is None


@pytest.mark.parametrize("use_migrate", [True, False], ids=["migrate", "push"])
@pytest.mark.parametrize("use_v2_resolver", [True, False], ids=["v2", "v1"])
def test_startup_refuses_unsettled_deliveries_before_prisma_records_anything(
    schema: tuple[str, psycopg.Connection[tuple[object, ...]]], use_migrate: bool, use_v2_resolver: bool
) -> None:
    name, connection = schema
    _deliver(connection, "pending")

    with pytest.raises(RuntimeError, match="pending or parked rows"):
        ProxyExtrasDBManager._run_migrations(use_migrate=use_migrate, use_v2_resolver=use_v2_resolver)
    assert (
        connection.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname='_prisma_migrations'",
            (name,),
        ).fetchone()
        is None
    )
    assert sorted(_remaining(connection, name)) == sorted(_OLD_OBJECTS)
