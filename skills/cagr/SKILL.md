# Compound annual growth

The metrics are already computed: `revenue_cagr`, `net_income_cagr` and `operating_cash_flow_cagr`. Read them;
do not recompute them and do not ask for filing text.

## What the numbers are

- Each is a compound annual rate over the fiscal years of one annual filing, on revenue [MD-31], on parent net income
  [MD-32] and on operating cash flow [MD-33]. Both ends come from that one filing, which by regulation carries
  three fiscal years of income and cash-flow statements [PER-04].
- The rate is a geometric average, which a source calls the more accurate measure of true growth in past
  earnings, especially where year-to-year growth has been erratic [C8 p.3].
- The rate is empty when either end is not positive, and flagged [MET-04].
- A series that spans the adoption of an accounting standard is flagged as non-comparable [PER-12]; the
  credit-loss standard is the rule's own case for a lender's earnings series [PER-14].

## What an empty value means

- `growth_base_not_positive` — an end of the series is zero or negative, so the rate would be meaningless
  [MET-04].
- `missing_input` — a fiscal year the filing does not report. An empty value is never a zero and never a
  neutral reading [MET-00].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].
