import json
from dataclasses import replace
from decimal import Decimal

import pytest

from stroybot.domain import Line, UserError
from stroybot.service import Service
from stroybot.store import Store


def quantity(service, code="51"):
    return format(Decimal(next(i.today for i in service.snapshot().items if i.code == code)).normalize(), "f")


def confirm(service, draft):
    return service.confirm(draft.owner, draft.id, draft.revision)


def test_confirmation_add_correction_zero_and_preserve_others(service):
    draft = service.manual(1, "51", "56")
    assert quantity(service) == "0"
    confirm(service, draft)
    assert quantity(service) == "56"
    confirm(service, draft)
    assert quantity(service) == "56"
    draft = service.manual(1, "51", "4")
    service.manual(1, "13", "15")
    confirm(service, service.store.active(1))
    assert quantity(service) == "60"
    service.new(1, "set")
    correction = service.manual(1, "51", "50")
    confirm(service, correction)
    assert quantity(service) == "50"
    assert quantity(service, "13") == "15"
    service.new(1, "set")
    confirm(service, service.manual(1, "51", "0"))
    assert quantity(service) == "0"
    data = json.loads(service.bitrix.path.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert len(next(iter(data.values()))["report"]["positions"]) == 1


def test_stale_confirmation_owner_and_unresolved_block(service):
    draft = service.manual(1, "51", "56")
    original_revision = draft.revision
    service.change(1, 1, "quantity", "50")
    with pytest.raises(UserError, match="устарела"):
        service.confirm(1, draft.id, original_revision)
    with pytest.raises(UserError, match="принадлежит"):
        service.confirm(2, draft.id, original_revision)
    draft = service.store.active(1)
    draft.lines.append(Line("неизвестно", "5", "м", [], question="Что именно?"))
    service.store.save(draft)
    with pytest.raises(UserError, match="уточните"):
        confirm(service, draft)
    assert quantity(service) == "0"


def test_over_budget_warning(service):
    draft = service.manual(1, "13", "400")
    assert "превышение сметы" in service.preview(draft)


def test_fractional_pieces_block(service):
    draft = service.manual(1, "40", "1.5")
    with pytest.raises(UserError, match="целым"):
        confirm(service, draft)


def test_other_report_changes_base(service):
    first = service.manual(1, "51", "56")
    second = service.manual(2, "51", "10")
    confirm(service, first)
    with pytest.raises(UserError, match="данные изменились"):
        confirm(service, second)
    assert quantity(service) == "56"


def test_midnight_requires_new_confirmation(service):
    draft = service.manual(1, "51", "56")
    service.today = lambda: "2026-11-04"
    with pytest.raises(UserError, match="Дата изменилась"):
        confirm(service, draft)


def test_bitrix_outage_recovers_after_restart_without_readding(service, settings, tmp_path):
    actual = service.bitrix

    class Failing:
        def upsert(self, *args):
            raise UserError("Сбой API")

    service.bitrix = Failing()
    draft = service.manual(1, "51", "56")
    with pytest.raises(UserError, match="/retry"):
        confirm(service, draft)
    assert quantity(service) == "56"
    assert service.store.operation(draft.id)["stage"] == "sheets"
    another = service.manual(2, "13", "5")
    with pytest.raises(UserError, match="незавершённая"):
        confirm(service, another)
    reopened = Store(settings.database)
    try:
        restarted = Service(settings, reopened, service.sheets, actual)
        restarted.today = service.today
        restarted.resume(1)
        assert quantity(restarted) == "56"
        assert restarted.store.active(1) is None
        assert restarted.store.operation(draft.id)["stage"] == "done"
    finally:
        reopened.close()


def test_lost_sheets_response_recovers_absolute_values(service):
    actual = service.sheets

    class LostReply:
        url = ""
        read = actual.read

        def apply(self, *args):
            actual.apply(*args)
            raise UserError("Ответ потерян")

    service.sheets = LostReply()
    draft = service.manual(1, "51", "56")
    with pytest.raises(UserError):
        confirm(service, draft)
    service.sheets = actual
    service.resume(1)
    assert quantity(service) == "56"


def test_deduplicate_message_and_correction_changes(service):
    item = next(i for i in service.snapshot().items if i.code == "51")

    class FakeAI:
        def parse(self, *args):
            return [Line("кабель 56 м", "56", "м", [item.id], item.id)]

    service.recognizer = FakeAI()
    draft = service.report(1, "кабель 56 м", "chat:42")
    repeated = service.report(1, "кабель 56 м", "chat:42")
    assert len(repeated.lines) == 1
    service.change(1, 1, "delete")
    assert not service.store.active(1).lines
    service.manual(1, "13", "15")
    confirm(service, service.store.active(1))
    assert quantity(service) == "0"
    assert quantity(service, "13") == "15"
    assert service.report(1, "кабель 56 м", "chat:42").id == draft.id


def test_zero_correction_creates_date_column(service):
    service.new(1, "set")
    confirm(service, service.manual(1, "51", "0"))
    assert service.today() in service.snapshot().date_columns


def test_database_cannot_switch_storage(service, settings):
    with pytest.raises(UserError, match="другому режиму"):
        Service(replace(settings, mode="live"), service.store, service.sheets, service.bitrix)


def test_duplicate_set_rejected(service):
    service.new(1, "set")
    service.manual(1, "51", "50")
    draft = service.manual(1, "51", "10")
    with pytest.raises(UserError, match="дважды"):
        confirm(service, draft)
