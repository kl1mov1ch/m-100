import asyncio
import json
import zipfile
from io import BytesIO

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp import signed

from stroybot.domain import UserError
from stroybot.miniapp import create_app, state
from stroybot.tabular import DataTable, detect_header, read_file, structure

pytest_plugins = ("test_access",)


def test_xlsx_multiple_sheets_dates_and_formulas():
    from datetime import date

    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "Заказы"
    book.active.append(["Дата", "Значение", "Расчёт"])
    book.active.append([date(2026, 11, 3), 12.5, "=B2*2"])
    book.create_sheet("Примечания").append(["Любые данные"])
    stream = BytesIO()
    book.save(stream)
    sheets = read_file("Заказы.xlsx", stream.getvalue())
    assert [s["name"] for s in sheets] == ["Заказы", "Примечания"]
    assert sheets[0]["rows"][1] == ["2026-11-03T00:00:00", 12.5, "=B2*2"]


def test_csv_encoding_duplicates_and_header_selection():
    raw = "Заголовок отчёта\nФИО;Город;Город;Заметка\nИван;Минск;Гродно;0\n"
    rows = read_file("клиенты.csv", raw.encode("cp1251"))[0]["rows"]
    assert detect_header(rows) == 1
    columns, data, _ = structure(rows, 1)
    assert [c["name"] for c in columns] == ["ФИО", "Город", "Город", "Заметка"]
    assert len({c["id"] for c in columns}) == 4
    assert data[0] == ["Иван", "Минск", "Гродно", "0"]
    tsv = read_file("test.tsv", "Имя\tГород\nАнна\tМинск".encode("utf-16"))[0]["rows"]
    assert tsv[1] == ["Анна", "Минск"]


def test_json_arbitrary_fields_and_blank_header():
    rows = read_file(
        "sample.json",
        json.dumps(
            [{"Заказ": 3, "Статус": "В работе", "Метки": ["а", "б"]}, {"Заказ": 4, "Статус": None}],
            ensure_ascii=False,
        ).encode(),
    )[0]["rows"]
    cols, data, mapping = structure(rows, 0)
    assert cols[0]["name"] == "Заказ" and data[0][0] == 3
    assert json.loads(data[0][2]) == ["а", "б"]
    cols, data, _ = structure([["Иван", 3], ["Анна", 4]], -1)
    assert data[0][0] == "Иван" and cols[0]["name"] == "Колонка 1"


@pytest.mark.parametrize(
    "filename,data",
    [
        ("bad.xlsx", b"not a workbook"),
        ("bad.csv", b""),
        ("file.pdf", b"%PDF"),
        ("huge.csv", b"x" * (12 * 1024 * 1024 + 1)),
    ],
)
def test_invalid_imports_are_bounded_and_clear(filename, data):
    with pytest.raises(UserError):
        read_file(filename, data)


def test_ods_repetition_and_xml_entity_rejected():
    source = """<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet><table:table table:name="Лист"><table:table-row><table:table-cell><text:p>Имя</text:p></table:table-cell></table:table-row><table:table-row table:number-rows-repeated="2"><table:table-cell><text:p>Иван</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet></office:body></office:document-content>"""
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("content.xml", source)
    rows = read_file("file.ods", stream.getvalue())[0]["rows"]
    assert rows == [["Имя"], ["Иван"], ["Иван"]]
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("content.xml", '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x>&x;</x>')
    with pytest.raises(UserError):
        read_file("evil.ods", stream.getvalue())


def test_table_edits_are_versioned_idempotent_and_csv_safe(tmp_path):
    table = DataTable(tmp_path / "table.sqlite3")
    try:
        table.initialize([["ФИО", "Число"], ["Анна", 2]], 0, {"filename": "a.csv", "sheet": "Данные"})
        row = table.all()[0]
        table.save(1, {"c1": "=HYPERLINK(1)", "c2": 3}, row["id"], 1, "same-request")
        table.save(1, {"c1": "=HYPERLINK(1)", "c2": 3}, row["id"], 1, "same-request")
        assert len(table.history()) == 1
        assert "'=HYPERLINK" in table.history_export().decode("utf-8-sig")
        with pytest.raises(UserError):
            table.save(1, {"c1": "поздняя правка"}, row["id"], 1)
        with pytest.raises(UserError):
            table.save(2, {"c1": "x"}, request_id="same-request")
        assert b"'=HYPERLINK" in table.export()
        table.delete(1, row["id"], 2)
        assert table.all() == [] and len(table.history()) == 2
        assert table.db.execute("SELECT active FROM records").fetchone()[0] == 0
    finally:
        table.db.close()


def test_import_projects_do_not_require_template_and_keep_source(hub):
    preview = hub.stage_import(1, "contacts.csv", "Имя;Телефон\nИван;+3751234".encode())
    assert preview["columns"][0]["name"] == "Имя"
    data = {"token": preview["token"], "name": "Контакты", "sheet": 0, "header": 0}
    key = hub.finish_import(1, data)
    assert hub.finish_import(1, data) == key
    with hub.scope(1):
        current = state(hub, 1)
        assert current["type"] == "table" and current["mode"] == "table"
        assert current["table"]["rows"][0]["values"]["c1"] == "Иван"
        assert current["items"] == []
        assert hub.sheets.table.meta("source_rows")[0] == ["Имя", "Телефон"]
    with pytest.raises(UserError):
        hub.import_preview(2, preview["token"], 0, 0)


def test_optional_mapping_arbitrary_units_and_catalogue_changes(hub):
    preview = hub.stage_import(1, "works.csv", "Комментарий;Единица;План;Артикул\nПокраска;м²;7,5;A".encode())
    hub.finish_import(1, {"token": preview["token"], "name": "Покраска", "sheet": 0, "header": 0})
    with hub.scope(1):
        table = hub.sheets.table
        table.configure({"name": "c1", "unit": "c2", "planned": "c3", "code": "c4"})
        item = hub.snapshot().items[0]
        assert item.unit == "м²" and item.planned == "7.5"
        draft = hub.manual(1, item.id, "2.5")
        hub.confirm(1, draft.id, draft.revision)
        assert hub.snapshot().items[0].today == "2.5"
        draft = hub.manual(1, item.id, "1")
        row = table.all()[0]
        table.save(1, {"c1": "Другая работа"}, row["id"], row["revision"])
        with pytest.raises(UserError, match="Справочник изменился"):
            hub.confirm(1, draft.id, draft.revision)


def test_upload_api_permissions_and_independent_records(hub):
    async def scenario():
        async with TestClient(TestServer(create_app(hub))) as client:
            admin = {"X-Telegram-Init-Data": signed("test-token", 1)}
            worker = {"X-Telegram-Init-Data": signed("test-token", 2)}
            denied = await client.post(
                "/api/import", headers=worker, json={"action": "preview", "token": "x"}
            )
            assert denied.status == 400
            form = FormData()
            form.add_field(
                "file", "Проект;Статус\nОфис;Новый".encode(), filename="projects.csv", content_type="text/csv"
            )
            response = await client.post("/api/import/upload", headers=admin, data=form)
            assert response.status == 200
            preview = await response.json()
            response = await client.post(
                "/api/import",
                headers=admin,
                json={
                    "action": "finish",
                    "token": preview["token"],
                    "name": "Объекты",
                    "sheet": 0,
                    "header": 0,
                },
            )
            key = (await response.json())["project_id"]
            response = await client.get("/api/table", headers=admin)
            table = await response.json()
            assert table["total"] == 1
            body = {
                "action": "save",
                "context_project": key,
                "request_id": "12345678-1234-1234-1234-123456789abc",
                "values": {"c1": "Школа", "c2": "В работе"},
            }
            assert (await client.post("/api/table", headers=admin, json=body)).status == 200
            assert (await client.post("/api/table", headers=admin, json=body)).status == 200
            assert (await (await client.get("/api/table", headers=admin)).json())["total"] == 2
            assert (await client.get("/api/table", headers=worker)).status == 400
            export = await client.get("/api/table.csv", headers=admin)
            assert export.status == 200 and (await export.read()).startswith(b"\xef\xbb\xbf")
            history = await client.get("/api/table/history.csv", headers=admin)
            history_text = await history.text(encoding="utf-8-sig")
            assert history.status == 200 and "Школа" in history_text, history_text

    asyncio.run(scenario())
