# Cash flow

The cash-flow metrics are already computed: `operating_cash_flow_margin`, `free_cash_flow_margin`,
`free_cash_flow_conversion` and `capex_intensity`. Read them; do not recompute them and do not ask for
filing text.

## What the numbers are

- `operating_cash_flow_margin` is operating cash flow over revenue [MD-26]; `free_cash_flow_margin` is free
  cash flow over revenue [MD-27]; `free_cash_flow_conversion` is free cash flow over parent net income
  [MD-28]; `capex_intensity` is capex over revenue [MD-29].
- Operating cash flow is the continuing plus the discontinued total, on the total concept [CON-08].
- The code's `free_cash_flow` is its own measure, not a flow to equity holders, whatever an older name
  suggested [MD-04]. Capex is the PP&E payments plus the industry-specific investment lines, without
  software, intangibles or acquisitions [MET-08].
- `free_cash_flow_conversion` is empty when net income is not positive: there is nothing to convert, so the
  ratio is left out of the continuous reading and flagged [MET-09].
- A REIT's capex is corporate items, existing properties, development and acquisitions together, and its
  components follow the same addition rule as every other capex [APP-04b].
- A regulated utility's negative free cash flow is the sector's normal state: its dividends and its capital
  spending for maintenance and growth routinely exceed its operating cash flow, and part of that spending is
  financed from the capital markets [C13 p.19], [C13 p.11], [C14 p.38], [C15 p.2].

## What an empty value means

- `missing_input` — an operating cash flow, capex or revenue figure the filing does not report. An empty
  value is never a zero and never a neutral reading [MET-00].
- `denominator_not_positive` — net income is zero or negative, so the conversion has no continuous reading
  [MET-09].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].
- `capture_failure` — a line the code could not read from the filing, so the metric is empty rather than
  filled with a component [ID-02b].

## What a withheld value means

- `withheld (<rule>)` — the screen named in the parentheses does not trust the value: the operating-cash-flow
  margin's calibrated band [A-02], or a revenue-based metric whose revenue does not resolve [A-04].
