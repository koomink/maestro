import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

from maestro.config.models import KISConfig
from maestro.core.enums import OrderSide
from maestro.execution.brokers.kis.auth import KISAuthManager
from maestro.execution.brokers.kis.domestic_live_order import KISRestDomesticStockLiveOrderClient
from maestro.execution.brokers.kis.overseas_live_order import KISRestOverseasStockLiveOrderClient

OPENAPI_WORKBOOK_GLOB = "한국투자증권_오픈API_전체문서_*.xlsx"
XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def test_openapi_catalog_matches_supported_kis_endpoints_and_tr_ids():
    catalog = _api_catalog_rows()

    assert catalog["접근토큰발급(P)"] == {
        "method": "POST",
        "url": "/oauth2/tokenP",
        "real_tr_id": "",
        "demo_tr_id": "",
    }
    assert catalog["주식잔고조회"] == {
        "method": "GET",
        "url": "/uapi/domestic-stock/v1/trading/inquire-balance",
        "real_tr_id": "TTTC8434R",
        "demo_tr_id": "VTTC8434R",
    }
    assert catalog["매수가능조회"] == {
        "method": "GET",
        "url": "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        "real_tr_id": "TTTC8908R",
        "demo_tr_id": "VTTC8908R",
    }
    assert catalog["주식현재가 시세"] == {
        "method": "GET",
        "url": "/uapi/domestic-stock/v1/quotations/inquire-price",
        "real_tr_id": "FHKST01010100",
        "demo_tr_id": "FHKST01010100",
    }
    assert catalog["해외주식 잔고"] == {
        "method": "GET",
        "url": "/uapi/overseas-stock/v1/trading/inquire-balance",
        "real_tr_id": "TTTS3012R",
        "demo_tr_id": "VTTS3012R",
    }
    assert catalog["해외주식 매수가능금액조회"] == {
        "method": "GET",
        "url": "/uapi/overseas-stock/v1/trading/inquire-psamount",
        "real_tr_id": "TTTS3007R",
        "demo_tr_id": "VTTS3007R",
    }
    assert catalog["해외주식 현재체결가"] == {
        "method": "GET",
        "url": "/uapi/overseas-price/v1/quotations/price",
        "real_tr_id": "HHDFS00000300",
        "demo_tr_id": "HHDFS00000300",
    }
    assert catalog["해외주식 주문체결내역"] == {
        "method": "GET",
        "url": "/uapi/overseas-stock/v1/trading/inquire-ccnl",
        "real_tr_id": "TTTS3035R",
        "demo_tr_id": "VTTS3035R",
    }

    domestic_order = catalog["주식주문(현금)"]
    assert domestic_order["method"] == "POST"
    assert domestic_order["url"] == "/uapi/domestic-stock/v1/trading/order-cash"
    assert "TTTC0012U" in domestic_order["real_tr_id"]
    assert "TTTC0011U" in domestic_order["real_tr_id"]
    assert "VTTC0012U" in domestic_order["demo_tr_id"]
    assert "VTTC0011U" in domestic_order["demo_tr_id"]

    overseas_order = catalog["해외주식 주문"]
    assert overseas_order["method"] == "POST"
    assert overseas_order["url"] == "/uapi/overseas-stock/v1/trading/order"
    assert "TTTT1002U" in overseas_order["real_tr_id"]
    assert "TTTT1006U" in overseas_order["real_tr_id"]
    assert "VTTT1002U" in overseas_order["demo_tr_id"]
    assert "VTTT1001U" in overseas_order["demo_tr_id"]

    overseas_unfilled = catalog["해외주식 미체결내역"]
    assert overseas_unfilled["method"] == "GET"
    assert overseas_unfilled["url"] == "/uapi/overseas-stock/v1/trading/inquire-nccs"
    assert overseas_unfilled["real_tr_id"] == "TTTS3018R"
    assert overseas_unfilled["demo_tr_id"] == "모의투자 미지원"


def test_oauth_workbook_matches_websocket_approval_key_contract():
    rows = _oauth_sheet_rows("실시간 (웹소켓) 접속키 발급")
    field_map = {row[0]: row[1] for row in rows if len(row) > 1 and row[0]}
    elements = {row[1]: row for row in rows if len(row) > 1 and row[1]}

    assert field_map["HTTP Method"] == "POST"
    assert field_map["URL 명"] == "/oauth2/Approval"
    assert elements["grant_type"][4] == "Y"
    assert elements["appkey"][4] == "Y"
    assert elements["secretkey"][4] == "Y"
    assert elements["approval_key"][4] == "Y"


def test_kis_order_tr_ids_follow_openapi_catalog(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")

    domestic_real = KISRestDomesticStockLiveOrderClient(_config("kis_domestic_stock"))
    domestic_demo = KISRestDomesticStockLiveOrderClient(
        _config("kis_domestic_stock", paper_trading=True)
    )
    overseas_real = KISRestOverseasStockLiveOrderClient(_config("kis_overseas_stock"))
    overseas_demo = KISRestOverseasStockLiveOrderClient(
        _config("kis_overseas_stock", paper_trading=True)
    )

    assert domestic_real._order_tr_id(OrderSide.BUY) == "TTTC0012U"
    assert domestic_real._order_tr_id(OrderSide.SELL) == "TTTC0011U"
    assert domestic_demo._order_tr_id(OrderSide.BUY) == "VTTC0012U"
    assert domestic_demo._order_tr_id(OrderSide.SELL) == "VTTC0011U"
    assert overseas_real._order_tr_id(OrderSide.BUY, "NASD") == "TTTT1002U"
    assert overseas_real._order_tr_id(OrderSide.SELL, "NASD") == "TTTT1006U"
    assert overseas_demo._order_tr_id(OrderSide.BUY, "NASD") == "VTTT1002U"
    assert overseas_demo._order_tr_id(OrderSide.SELL, "NASD") == "VTTT1001U"


def test_kis_oauth_request_uses_openapi_content_type(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.delenv("TEST_KIS_ACCESS_TOKEN", raising=False)
    transport = TokenTransport()
    config = _config("kis_overseas_stock")

    KISAuthManager(config, transport).get_access_token()

    assert transport.calls[0]["url"].endswith("/oauth2/tokenP")
    assert transport.calls[0]["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert "charset" not in transport.calls[0]["headers"]


def test_kis_websocket_approval_key_uses_openapi_contract(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.delenv("TEST_KIS_APPROVAL_KEY", raising=False)
    transport = TokenTransport()
    config = _config("kis_overseas_stock")

    approval_key = KISAuthManager(config, transport).get_websocket_approval_key()

    assert approval_key.approval_key == "issued-approval-key"
    assert approval_key.source == "oauth"
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["url"].endswith("/oauth2/Approval")
    assert transport.calls[0]["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert transport.calls[0]["json_body"] == {
        "grant_type": "client_credentials",
        "appkey": "app-key",
        "secretkey": "app-secret",
    }


def test_kis_websocket_approval_key_can_come_from_env(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_APPROVAL_KEY", "env-approval-key")
    transport = TokenTransport()
    config = _config("kis_overseas_stock")

    approval_key = KISAuthManager(config, transport).get_websocket_approval_key()

    assert approval_key.approval_key == "env-approval-key"
    assert approval_key.source == "env"
    assert transport.calls == []


def _config(broker_product: str, *, paper_trading: bool = False) -> KISConfig:
    return KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        approval_key_env="TEST_KIS_APPROVAL_KEY",
        broker_products=[broker_product],
        paper_trading=paper_trading,
    )


class TokenTransport:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, url, *, headers, params=None, json_body=None, timeout_seconds=10.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params or {},
                "json_body": json_body or {},
                "timeout_seconds": timeout_seconds,
            }
        )
        if url.endswith("/oauth2/Approval"):
            return {"approval_key": "issued-approval-key"}
        return {"access_token": "issued-token", "access_token_token_expired": "2099-01-01 00:00:00"}


def _api_catalog_rows() -> dict[str, dict[str, str]]:
    workbook = _openapi_workbook_path()
    with ZipFile(workbook) as archive:
        shared_strings = _shared_strings(archive)
        sheet_paths = _sheet_paths(archive)
        rows = _worksheet_rows(archive, shared_strings, sheet_paths["API 목록"])

    header = rows[0]
    required = {
        "API 명": header.index("API 명"),
        "실전 TR_ID": header.index("실전 TR_ID"),
        "모의 TR_ID": header.index("모의 TR_ID"),
        "HTTP Method": header.index("HTTP Method"),
        "URL 명": header.index("URL 명"),
    }
    catalog = {}
    for row in rows[1:]:
        padded = row + [""] * len(header)
        name = padded[required["API 명"]]
        catalog[name] = {
            "method": padded[required["HTTP Method"]],
            "url": padded[required["URL 명"]],
            "real_tr_id": padded[required["실전 TR_ID"]],
            "demo_tr_id": padded[required["모의 TR_ID"]],
        }
    return catalog


def _openapi_workbook_path() -> Path:
    candidates = sorted(
        path
        for path in Path("docs").glob("*.xlsx")
        if "오픈API" in unicodedata.normalize("NFC", path.name)
        and "전체문서" in unicodedata.normalize("NFC", path.name)
    )
    assert candidates, f"missing KIS OpenAPI workbook matching {OPENAPI_WORKBOOK_GLOB}"
    return candidates[0]


def _oauth_workbook_path() -> Path:
    candidates = sorted(
        path
        for path in Path("docs").glob("*.xlsx")
        if "OAuth" in unicodedata.normalize("NFC", path.name)
    )
    assert candidates, "missing KIS OAuth workbook"
    return candidates[0]


def _oauth_sheet_rows(sheet_name: str) -> list[list[str]]:
    workbook = _oauth_workbook_path()
    with ZipFile(workbook) as archive:
        shared_strings = _shared_strings(archive)
        sheet_paths = _sheet_paths(archive)
        return _worksheet_rows(archive, shared_strings, sheet_paths[sheet_name])


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(text.text or "" for text in item.findall(".//main:t", XLSX_NS))
        for item in root.findall("main:si", XLSX_NS)
    ]


def _sheet_paths(archive: ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relmap = {
        rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("rel:Relationship", XLSX_NS)
    }
    return {
        sheet.attrib["name"]: "xl/"
        + relmap[
            sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        ].lstrip("/")
        for sheet in workbook.findall("main:sheets/main:sheet", XLSX_NS)
    }


def _worksheet_rows(
    archive: ZipFile,
    shared_strings: list[str],
    sheet_path: str,
) -> list[list[str]]:
    root = ET.fromstring(archive.read(sheet_path))
    rows = []
    for row in root.findall("main:sheetData/main:row", XLSX_NS):
        cells = {
            _column_index(cell.attrib.get("r", "A1")): _cell_value(cell, shared_strings)
            for cell in row.findall("main:c", XLSX_NS)
        }
        if cells:
            rows.append([cells.get(index, "") for index in range(max(cells) + 1)])
    return rows


def _column_index(cell_ref: str) -> int:
    letters = "".join(character for character in cell_ref if character.isalpha())
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter.upper()) - 64
    return index - 1


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    value = cell.find("main:v", XLSX_NS)
    if cell.get("t") == "s" and value is not None:
        return shared_strings[int(value.text or "0")]
    if cell.get("t") == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//main:t", XLSX_NS))
    return value.text if value is not None else ""
