# KIS cash transaction API survey

Question asked: can the KIS Open API tell us *why* an account's cash moved, so
that a deposit, a dividend, a tax withholding and a currency conversion can be
told apart without asking the operator?

Source: `docs/한국투자증권_오픈API_전체문서_20260507_030000.xlsx`, the official
endpoint catalogue shipped with this repository. Findings are pinned by
`tests/test_kis_openapi_specs.py` so a newer workbook that changes them fails.

## Answer: no, not for the accounts Maestro uses

Every endpoint under `[국내주식] 주문/계좌` and `[해외주식] 주문/계좌` is an
order, a balance, or trade profit and loss. None reports a cash movement with a
cause attached. Maestro's KIS accounts are all `kis_domestic_stock` or
`kis_overseas_stock`, so none of them can be served this way.

KIS does expose exactly this concept — for a different account type:

| API | TR_ID | URL |
| --- | --- | --- |
| 해외선물옵션 기간계좌거래내역 | `OTFM3114R` | `/uapi/overseas-futureoption/v1/trading/inquire-period-trans` |

It takes `ACNT_TR_TYPE_CD` (`1` all, `2` 입출금, `3` 결제) and returns
`fm_iofw_amt`, a deposit/withdrawal amount. That is the shape we wanted. It is
an overseas futures and options endpoint, and there is no stock equivalent.

So the operator-confirmation path is not a stopgap for KIS: it is the only
option the broker offers for stock accounts.

## What the closest candidates actually return

**해외주식 일별거래내역** — `CTOS4001R`,
`/uapi/overseas-stock/v1/trading/inquire-period-trans`. The name promises a
transaction history, but the request takes `SLL_BUY_DVSN_CD` (`00` all, `01`
sell, `02` buy) and every output field is trade-shaped: `trad_dt`, `sttl_dt`,
`sll_buy_dvsn_cd`, `pdno`, `ccld_qty`, `ovrs_stck_ccld_unpr`, fees. There is no
row type for anything that is not a trade. Trades are the one cash movement
Maestro already explains from its own fills, so this adds nothing to
classification.

It is still worth having for something else: `sttl_dt` is the settlement date
per trade, and the fee fields are per trade. That is directly useful for
telling a settlement-timing difference apart from a real cash movement, which
is what KIS candidate detection needs.

**기간별계좌권리현황조회** — `CTRGA011R`,
`/uapi/domestic-stock/v1/trading/period-rights`. Account-scoped domestic
corporate actions, with `rght_type_cd`, `cash_dfrm_dt` (cash payment date) and
`last_alct_amt` (allotted amount). This is a genuine domestic dividend and
rights source. It reports the entitlement, not the cash line, so it can
corroborate a cash change rather than replace the operator's confirmation.

**해외주식 기간별권리조회** — `CTRGT011R`,
`/uapi/overseas-price/v1/quotations/period-rights`. Despite the name this sits
under `overseas-price/quotations`, and the documented sample returns
instruments the account does not hold. It is a market-wide corporate action
calendar, not an account statement. Useful only as a calendar to correlate
against.

**기간별매매손익현황조회** — `TTTC8715R`. Realised profit and loss per trade
with `fee` and `tl_tax`. Trades only.

## The constraint that limits verification

Every endpoint named above is `모의투자 미지원` — no paper-trading support.
None can be exercised against the KIS mock environment, so a response fixture
can only come from a live account. That is why this survey stops at the
official specification: the remaining verification needs live credentials and
an operator decision to use them.

## What this means for the work that depended on it

- **Automatic classification of KIS cash movements is not available.** Keep
  operator confirmation as the way a cash change becomes a classified flow.
- **Hardening KIS candidate detection does not depend on this API.** It needs
  stability across snapshots and a settlement horizon, and
  `_settlement_elapsed_days` in `execution/reconciliation.py` already supplies
  the latter from data Maestro holds. `CTOS4001R` would sharpen it for overseas
  accounts by giving a real `sttl_dt` instead of an inferred one.
- **A dividend corroboration signal is available for domestic accounts** via
  `CTRGA011R`, if the operator wants a cash-drift observation to arrive already
  suggesting `dividend` rather than `unexplained`. It cannot confirm on its own.
- **Broker-side automatic conversion still has no source.** Nothing in the stock
  account catalogue reports an FX leg.
