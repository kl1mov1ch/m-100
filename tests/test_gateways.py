import json
from dataclasses import replace

import httpx
import pytest

from stroybot.domain import UserError, parse_table
from stroybot.gateways import Bitrix24, GoogleSheets
from stroybot.recognition import Recognizer


def fake_google(rows, handler):
    class Credentials:
        valid = True
        token = "fake-token"

    gateway = GoogleSheets.__new__(GoogleSheets)
    gateway.credentials = Credentials()
    gateway.client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway.sheet = "Лист1"
    gateway.root = "https://sheets.googleapis.com/v4/spreadsheets/fake"
    gateway.url = "https://docs.google.com/spreadsheets/d/fake/edit"
    gateway.grid_columns = 0
    return gateway


def test_google_atomic_date_create_formulas_and_repeat(rows):
    writes = []

    def handle(request):
        if request.method == "GET":
            if "/values/" in str(request.url):
                return httpx.Response(200, json={"values": rows})
            return httpx.Response(
                200,
                json={
                    "sheets": [
                        {
                            "properties": {
                                "title": "Лист1",
                                "sheetId": 42,
                                "gridProperties": {"columnCount": 85},
                            }
                        }
                    ]
                },
            )
        payload = json.loads(request.content)
        writes.append(payload)
        for change in payload["requests"]:
            if "updateCells" in change:
                update = change["updateCells"]
                r, c = update["start"]["rowIndex"], update["start"]["columnIndex"]
                while len(rows[r]) <= c:
                    rows[r].append(None)
                value = update["rows"][0]["values"][0]["userEnteredValue"]
                rows[r][c] = next(iter(value.values()))
        return httpx.Response(200, json={"replies": []})

    gateway = fake_google(rows, handle)
    item = next(i for i in gateway.read("2026-11-03").items if i.code == "51")
    gateway.apply("2026-11-03", {item.id: "0"}, {item.id: "56"})
    gateway.apply("2026-11-03", {item.id: "0"}, {item.id: "56"})
    assert len(writes) == 1
    assert rows[54][49] == 56
    assert rows[54][8] == "=SUM(J55:CZ55)"
    assert any("appendDimension" in r for r in writes[0]["requests"])
    assert all(
        r.get("updateCells", {}).get("start", {}).get("sheetId", 42) == 42 for r in writes[0]["requests"]
    )
    gateway.client.close()


def test_google_conflicting_external_edit_blocks_write(rows):
    snapshot = parse_table(rows, "2026-08-31")
    item = next(i for i in snapshot.items if i.code == "13")

    def handle(request):
        assert request.method == "GET"
        if "/values/" in str(request.url):
            return httpx.Response(200, json={"values": rows})
        return httpx.Response(
            200,
            json={
                "sheets": [
                    {"properties": {"title": "Лист1", "sheetId": 42, "gridProperties": {"columnCount": 104}}}
                ]
            },
        )

    gateway = fake_google(rows, handle)
    with pytest.raises(UserError, match="изменились"):
        gateway.apply("2026-08-31", {item.id: "0"}, {item.id: "56"})
    gateway.client.close()


def test_google_write_permission_check_uses_identical_replacement(rows):
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        action = payload["requests"][0]["findReplace"]
        assert action["find"] == action["replacement"]
        assert action["sheetId"] == 42 and action["includeFormulas"] is False
        calls.append(action)
        return httpx.Response(200, json={"replies": [{"findReplace": {"occurrencesChanged": 0}}]})

    gateway = fake_google(rows, handle)
    gateway.sheet_id = 42
    gateway.check_write_access()
    assert len(calls) == 1
    gateway.client.close()


def test_bitrix_lost_add_response_then_reconcile_and_correct(settings):
    deals, calls = {}, []
    lose_reply = True

    def handle(request):
        nonlocal lose_reply
        method = request.url.path.rsplit("/", 1)[-1].replace(".json", "")
        payload = json.loads(request.content)
        calls.append(method)
        if method == "crm.deal.list":
            items = [
                {"ID": key, "COMMENTS": value["COMMENTS"]}
                for key, value in deals.items()
                if value["ORIGIN_ID"] == payload["filter"]["=ORIGIN_ID"]
            ]
            return httpx.Response(200, json={"result": items})
        if method == "crm.deal.add":
            deals["1"] = payload["fields"]
            if lose_reply:
                lose_reply = False
                raise httpx.ReadTimeout("secret-webhook-must-not-leak")
            return httpx.Response(200, json={"result": 1})
        if method == "crm.deal.update":
            deals[str(payload["id"])].update(payload["fields"])
            return httpx.Response(200, json={"result": True})
        if method == "crm.deal.get":
            return httpx.Response(200, json={"result": deals[str(payload["id"])]})
        raise AssertionError(method)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        gateway = Bitrix24(replace(settings, bitrix_url="https://example.test/rest/1/secret/"), client)
        with pytest.raises(UserError) as error:
            gateway.upsert("day-key", "Отчёт", {"quantity": "56"})
        assert "secret" not in str(error.value)
        gateway.upsert("day-key", "Отчёт", {"quantity": "56"})
        gateway.upsert("day-key", "Отчёт", {"quantity": "50"})
    assert len(deals) == 1
    assert calls.count("crm.deal.add") == 1
    assert json.loads(deals["1"]["COMMENTS"]) == {"quantity": "50"}


def test_llm_structured_example_and_voice_contract(settings, rows):
    snapshot = parse_table(rows, "2026-11-03")
    by_code = {i.code: i for i in snapshot.items}
    response_lines = [
        {
            "source": name,
            "quantity": qty,
            "unit": by_code[code].unit,
            "candidate_ids": [by_code[code].id],
            "question": None,
        }
        for code, name, qty in [
            ("13", "медь 6-ка", 15),
            ("14", "медь 9-ка", 15),
            ("16", "изоляция 6", 15),
            ("17", "изоляция 9", 15),
            ("51", "кабель 3х2,5", 56),
            ("40", "хомуты", 10),
        ]
    ]

    def handle(request):
        if request.url.path.endswith("/audio/transcriptions"):
            assert b"voice.ogg" in request.content
            assert b"audio/ogg" in request.content
            return httpx.Response(200, json={"text": "медь 6-ка 15 м"})
        payload = json.loads(request.content)
        assert payload["response_format"]["json_schema"]["strict"] is True
        catalog = json.loads(payload["messages"][1]["content"])["catalog"]
        assert len(catalog) <= 18
        assert {line["candidate_ids"][0] for line in response_lines} <= {item["id"] for item in catalog}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps({"lines": response_lines})}}
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        recognizer = Recognizer(replace(settings, llm_key="fake", llm_model="test"), client)
        lines = recognizer.parse(
            "медь 6-ка 15 м и 9-ка 15 м, изоляция на них столько же, кабель 3х2,5 56 м и хомуты 10 шт",
            snapshot,
        )
        assert len(lines) == 6
        assert not any(line.question for line in lines)
        assert recognizer.transcribe(b"OggS-fake") == "медь 6-ка 15 м"


def test_voice_failure_and_invalid_model_output(settings, rows):
    snapshot = parse_table(rows, "2026-11-03")

    def handle(request):
        if "audio" in request.url.path:
            return httpx.Response(503, json={"detail": "secret"})
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        recognizer = Recognizer(replace(settings, llm_key="fake", llm_model="test"), client)
        with pytest.raises(UserError, match="503"):
            recognizer.transcribe(b"fake")
        with pytest.raises(UserError, match="не завершил"):
            recognizer.parse("кабель 5 м", snapshot)
