from decimal import Decimal

import pytest

from stroybot.domain import UserError, number, parse_table
from stroybot.recognition import validate


def test_generated_template_catalog_and_remaining(rows):
    snapshot = parse_table(rows, "2026-11-03")
    assert len(snapshot.items) == 62
    copper = next(i for i in snapshot.items if i.code == "13")
    assert copper.completed == "140"
    assert copper.remaining == Decimal(360)
    assert len(snapshot.date_columns["2026-09-30"]) == 2
    assert len({i.id for i in snapshot.items}) == 62
    assert next(i for i in snapshot.items if i.code == "51").unit == "м"
    assert snapshot.target_column() == (50, True)


def test_duplicate_current_day_blocks_write(rows):
    snapshot = parse_table(rows, "2026-09-30")
    with pytest.raises(UserError, match="дата повторяется"):
        snapshot.target_column()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", True, None, "1e999", "0.00000001"])
def test_reject_unsafe_quantities(bad):
    with pytest.raises(UserError):
        number(bad)


def test_catalog_row_move_preserves_ids(rows):
    before = parse_table(rows, "2026-11-03")
    rows[16], rows[17] = rows[17], rows[16]
    after = parse_table(rows, "2026-11-03")
    assert {i.id for i in before.items} == {i.id for i in after.items}


def test_hallucinated_id_rejected(rows):
    with pytest.raises(UserError, match="вне справочника"):
        validate(
            {
                "lines": [
                    {
                        "source": "кабель",
                        "quantity": 5,
                        "unit": "м",
                        "candidate_ids": ["invented"],
                        "question": None,
                    }
                ]
            },
            parse_table(rows, "2026-11-03"),
        )


def test_identical_names_force_clarification(rows):
    snapshot = parse_table(rows, "2026-11-03")
    drill = next(i for i in snapshot.items if i.code == "44")
    line = validate(
        {
            "lines": [
                {
                    "source": "сверлили",
                    "quantity": 2,
                    "unit": "шт",
                    "candidate_ids": [drill.id],
                    "question": None,
                }
            ]
        },
        snapshot,
    )[0]
    assert line.item_id is None
    assert len(line.candidates) == 2
    assert line.question


def test_unit_conflict_requires_correction(rows):
    snapshot = parse_table(rows, "2026-11-03")
    copper = next(i for i in snapshot.items if i.code == "13")
    line = validate(
        {
            "lines": [
                {
                    "source": "медь 6 5 кг",
                    "quantity": 5,
                    "unit": "кг",
                    "candidate_ids": [copper.id],
                    "question": None,
                }
            ]
        },
        snapshot,
    )[0]
    assert line.question and line.unit == "кг"


def test_undated_volume_is_not_silently_ignored(rows):
    rows[16][49] = 15
    with pytest.raises(UserError, match="объём без даты"):
        parse_table(rows, "2026-11-03")


def test_model_question_requires_explicit_selection(rows):
    snapshot = parse_table(rows, "2026-11-03")
    cable = next(i for i in snapshot.items if i.code == "51")
    line = validate(
        {
            "lines": [
                {
                    "source": "кабель",
                    "quantity": 15,
                    "unit": "м",
                    "candidate_ids": [cable.id],
                    "question": "Это кабель 3х2,5?",
                }
            ]
        },
        snapshot,
    )[0]
    assert line.item_id is None
    assert line.question
