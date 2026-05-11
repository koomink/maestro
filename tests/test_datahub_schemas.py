from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from maestro.core.enums import AssetType
from maestro.datahub.schemas import (
    SUPPORTED_DATA_TYPES,
    OHLCVBar,
    PricePoint,
    SymbolData,
    SymbolMetadata,
)


def test_price_point_requires_positive_price():
    with pytest.raises(ValidationError):
        PricePoint(symbol="MOCK_ETF_A", timestamp=datetime.now(UTC), price=0, source="test")


def test_ohlcv_bar_validates_shape():
    with pytest.raises(ValidationError, match="high must be greater"):
        OHLCVBar(
            symbol="MOCK_ETF_A",
            timestamp=datetime.now(UTC),
            open=100,
            high=90,
            low=95,
            close=100,
            volume=1,
            source="test",
        )


def test_symbol_data_defaults_are_dashboard_friendly():
    data = SymbolData(symbol="MOCK_ETF_A")

    assert data.latest_price is None
    assert data.bars == []
    assert data.is_stale is False
    assert data.warnings == []


def test_symbol_metadata_is_lightweight():
    metadata = SymbolMetadata(
        symbol="MOCK_ETF_A",
        asset_type=AssetType.DOMESTIC_ETF,
        quantity_step=1,
        min_order_quantity=1,
        min_order_notional=1000,
    )

    assert metadata.currency == "KRW"
    assert metadata.tradable is True


def test_supported_data_types_include_llm_research_contract_needs():
    assert {
        "technical_indicators",
        "financial_statements",
        "insider_transactions",
    }.issubset(SUPPORTED_DATA_TYPES)
