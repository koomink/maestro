# KIS Fixture Redaction

Use this format before adding real KIS response fixtures to tests or docs.
Fixtures must prove parser behavior without exposing account, order, token, or
cash-sensitive data.

## Redaction Rules

- Replace account numbers with `12345678-01`.
- Replace app keys, app secrets, access tokens, approval IDs, and Telegram IDs
  with deterministic placeholders.
- Replace broker order numbers with stable fake IDs such as `9001`.
- Keep symbols, exchange codes, TR_IDs, field names, status strings, and numeric
  edge cases needed by parser tests.
- Round cash, buying power, prices, quantities, and notionals to small synthetic
  values. Preserve signs and zero/nonzero semantics.
- Remove headers except pagination fields such as `tr_cont`, `ctx_area_fk200`,
  and `ctx_area_nk200`.
- Never commit raw KIS HTTP requests, authorization headers, token cache files,
  or screenshots from broker tools.

## Fixture Shape

```json
{
  "source": "kis_overseas_stock",
  "endpoint": "/uapi/overseas-stock/v1/trading/inquire-ccnl",
  "tr_id": "TTTS3035R",
  "case": "filled_us_stock_order",
  "response": {
    "rt_cd": "0",
    "output": [
      {
        "odno": "9001",
        "pdno": "AAPL",
        "sll_buy_dvsn_cd": "02",
        "ft_ord_qty": "2",
        "ft_ccld_qty": "2",
        "ft_ccld_unpr3": "100.00",
        "prcs_stat_name": "체결"
      }
    ]
  }
}
```

Normal test runs must stay fake-client or redacted-fixture based and must not
call KIS network endpoints.
