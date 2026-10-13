# Efficiency

The efficiency metrics are already computed: `asset_turnover`, `inventory_turnover` and
`receivables_turnover`. Read them; do not recompute them and do not ask for filing text.

## What the numbers are

- `asset_turnover` is revenue over total assets [MD-19].
- `inventory_turnover` is the cost of revenue over inventory [MD-20]; `receivables_turnover` is revenue over
  receivables [MD-21].
- Each is empty when its own denominator is not filed: a company with no inventory line has an empty
  `inventory_turnover`, not a computed one [MET-00].
- `asset_turnover` is revenue-based: when a filing reports a net income but no revenue, the code does not
  compute it and the value is withheld [A-04].

## What an empty value means

- `missing_input` — an inventory, receivables or revenue line the filing does not report. An empty value is
  never a zero and never a neutral reading [MET-00].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].

## What a withheld value means

- `withheld (<rule>)` — the screen named in the parentheses does not trust the value: a revenue-based metric
  whose revenue does not resolve is withheld rather than read [A-04].
