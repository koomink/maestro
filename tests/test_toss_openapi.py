import json
from pathlib import Path


def test_toss_openapi_fixture_is_canonical_document():
    spec = json.loads(Path("docs/toss_openapi.json").read_text())

    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["title"] == "토스증권 Open API"
    assert spec["servers"] == [{"url": "https://openapi.tossinvest.com"}]


def test_toss_openapi_fixture_contains_required_backbone_operations():
    spec = json.loads(Path("docs/toss_openapi.json").read_text())
    operation_ids = {
        operation["operationId"]
        for path_item in spec["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }

    assert {
        "issueOAuth2Token",
        "getAccounts",
        "getHoldings",
        "getPrices",
        "getBuyingPower",
        "createOrder",
        "getOrders",
        "getOrder",
    } <= operation_ids
