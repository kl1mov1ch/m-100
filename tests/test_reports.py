import asyncio
import csv
from io import StringIO

import pytest
from aiohttp.test_utils import TestClient, TestServer

from stroybot.domain import UserError
from stroybot.miniapp import create_app
from stroybot.reports import changes, csv_file, period, safe_text
from test_miniapp import signed


def test_history_add_correct_zero_dedup_and_owner(service):
    draft = service.manual(1, "51", "5")
    service.confirm(1, draft.id, draft.revision)
    service.confirm(1, draft.id, draft.revision)
    service.new(1, "set")
    draft = service.manual(1, "51", "0")
    service.confirm(1, draft.id, draft.revision)
    report = changes(service, 1)
    assert len(report["rows"]) == 2
    correction, addition = report["rows"]
    assert (correction["before"], correction["delta"], correction["after"], correction["mode"]) == (
        "5",
        "-5",
        "0",
        "Исправление",
    )
    assert addition["code"] == "51" and addition["at"]
    assert changes(service, 2)["rows"] == []
    assert changes(service, 1, "2026-01-01", "2026-01-02")["rows"] == []
    data = csv_file(report)
    assert data.startswith(b"\xef\xbb\xbf")
    values = list(csv.reader(StringIO(data.decode("utf-8-sig")), delimiter=";"))
    assert len(values) == 3 and values[1][-2:] == ["-5", "0"]


def test_pending_changes_are_explicit_and_cancelled_not_reported(service):
    draft = service.manual(1, "51", "5")
    service.cancel(1)
    assert changes(service, 1)["rows"] == []
    draft = service.manual(1, "51", "5")

    def fail(*args):
        raise UserError("Недоступно")

    service.bitrix.upsert = fail
    with pytest.raises(UserError):
        service.confirm(1, draft.id, draft.revision)
    row = changes(service, 1)["rows"][0]
    assert row["stage"] == "sheets" and "ожидает" in row["status"]


@pytest.mark.parametrize(
    "start,end", [("bad", None), ("2026-11-04", "2026-11-03"), ("2020-01-01", "2026-11-03")]
)
def test_report_period_validation(start, end):
    with pytest.raises(UserError):
        period("2026-11-03", start, end)


def test_formula_injection_protection():
    for value in ["=HYPERLINK(1)", " +SUM(1)", "@test", "-cmd", "\ttest"]:
        assert safe_text(value).startswith("'")
    assert safe_text("Монтаж кабеля") == "Монтаж кабеля"


def test_report_api_requires_owner_auth(service):
    service.settings.token = "test-token"
    service.settings.allowed = {1, 2}
    draft = service.manual(1, "51", "5")
    service.confirm(1, draft.id, draft.revision)

    async def scenario():
        async with TestClient(TestServer(create_app(service))) as client:
            assert (await client.get("/api/report.csv")).status == 400
            response = await client.get(
                "/api/report", headers={"X-Telegram-Init-Data": signed("test-token", 2)}
            )
            assert (await response.json())["rows"] == []
            response = await client.get(
                "/api/report.csv", headers={"X-Telegram-Init-Data": signed("test-token")}
            )
            assert response.status == 200
            assert response.content_type == "text/csv"
            assert "attachment" in response.headers["Content-Disposition"]
            assert (await response.read()).startswith(b"\xef\xbb\xbf")

    asyncio.run(scenario())
