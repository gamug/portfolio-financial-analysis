# Cash flow

The cash-flow metrics are already computed: `operating_cash_flow_margin`, `free_cash_flow_margin`,
`free_cash_flow_conversion` and `capex_intensity`. Read them; do not recompute them and do not ask for
filing text. Together they say whether the earnings a company reports are backed by cash, and how much of
that cash it must spend to keep the business running [MD-26], [MD-27], [MD-28], [MD-29].

## How to read them

- `operating_cash_flow_margin` and `free_cash_flow_margin` are read beside the profitability margins: cash
  margin well below accounting margin is the weaker quality of earnings [MD-26], [MD-27].
- The pipeline's `free_cash_flow` is the firm's own cash generation, not a flow to equity holders, whatever
  the older name suggested [MD-04].
- `free_cash_flow_conversion` is empty when net income is not positive: there is nothing to convert, and a
  negative ratio would read as a stronger number than it is [MET-09], [MD-28].
- `capex_intensity` says how much of the cash generated must go back into the asset base. A capital-intensive
  company is expected to show a high one; compare it with the company's own history and its peers [MD-29].
- A regulated utility's negative free cash flow is the sector's normal state, funded from the capital
  markets, not a distress signal on its own [C13 p.19], [C13 p.11], [C14 p.38], [C15 p.2].
- A bank's cash-flow statement shows little or no capital expenditure, so its cash-flow metrics do not carry
  the meaning they carry for an operating company [APP-02b], [C2 p.8], [C2 p.1].
- A REIT's capital spending includes acquisitions of property, which an operating company would not report
  as capital expenditure; read its intensity with that in mind [APP-04b].

## What an empty value means

- `missing_input` — an operating cash flow, capital expenditure or revenue figure the filing does not
  report. An empty value is never a zero [MET-00], [MET-08].
- `denominator_not_positive` — net income is zero or negative, so the conversion has no reading [MET-09].
- `not_applicable` — a company type for which the metric is meaningless [APP-02], [APP-02b].
- `withheld (<rule>)` — a value the pipeline does not trust: an operating-cash-flow margin far outside the
  calibrated band, or a free-cash-flow margin or yield outside its own, is withheld [A-02], [A-04], [A-05].

## What is not applicable, and why

- A bank or an insurer: the cash-flow metrics are not applicable, because its capital expenditure, its
  working capital and its debt are not clearly defined [APP-02b], [C2 p.1].
- A REIT: its capital spending is not comparable with an operating company's, and its earnings measure is
  not GAAP net income alone [APP-04b], [APP-04].
