from typing import Final

import pytest

from scripts.check_schema_compatibility import check_breaking_changes, parse_model_fields

ORIGINAL: Final = "model Item {\n id String @id\n label String?\n}\n"


def test_aaaaaschema_compatibility() -> None:
    added: Final = "model Item {\n id String @id\n label String?\n count Int?\n}\nmodel Extra {\n id String @id\n}\n"
    assert check_breaking_changes(parse_model_fields(ORIGINAL), parse_model_fields(added)) == ()


def test_parse_model_fields_retains_types_and_attributes() -> None:
    assert parse_model_fields(ORIGINAL) == {"Item": {"id": "String @id", "label": "String?"}}


def test_parse_model_fields_keeps_json_defaults_and_empty_models() -> None:
    schema: Final = 'model Item {\n data Json @default("{}")\n}\nmodel Empty {\n}\n'
    assert parse_model_fields(schema) == {"Item": {"data": 'Json @default("{}")'}, "Empty": {}}


@pytest.mark.parametrize(
    ("schema", "expected"),
    (
        ("", ("Breaking: Model Item was removed",)),
        ("model Item {\n id String @id\n}\n", ("Breaking: Field Item.label was removed",)),
        (
            "model Item {\n id String @id\n label String\n}\n",
            ("Breaking: Field Item.label changed from optional to required",),
        ),
        (
            "model Item {\n id String @id\n label Int?\n}\n",
            ("Breaking: Field Item.label changed type from String? to Int?",),
        ),
    ),
)
def test_schema_compatibility_rejects_removed_models_fields_and_narrowed_types(
    schema: str, expected: tuple[str, ...]
) -> None:
    assert check_breaking_changes(parse_model_fields(ORIGINAL), parse_model_fields(schema)) == expected


def test_an_allowed_model_may_be_dropped_while_every_other_removal_still_breaks() -> None:
    previous: Final = parse_model_fields(ORIGINAL + "model Scaffold {\n id String @id\n note String?\n}\n")
    allowed: Final = frozenset({"Scaffold"})

    assert check_breaking_changes(previous, parse_model_fields(ORIGINAL), allowed) == ()
    assert check_breaking_changes(previous, parse_model_fields(""), allowed) == ("Breaking: Model Item was removed",)
    assert check_breaking_changes(
        previous, parse_model_fields(ORIGINAL + "model Scaffold {\n id String @id\n}\n"), allowed
    ) == ("Breaking: Field Scaffold.note was removed",)
