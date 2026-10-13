# Compound annual growth

The metrics are already computed: `revenue_cagr`, `net_income_cagr` and `operating_cash_flow_cagr`. Read
them; do not recompute them and do not ask for filing text. Each is a compound annual rate over the
fiscal years the filing itself reports, so a higher value is the stronger reading [MD-31], [MD-32],
[MD-33], [PER-04].

## How to read them

- The rate is a geometric average, which is the more accurate measure of past growth than a simple average,
  and it matters most where year-to-year growth has been erratic [C8 p.3], [MD-31].
- Read the three together: revenue compounding while net income or operating cash flow does not says the
  growth is not reaching the bottom line [MD-31], [MD-32], [MD-33].
- The rate is computed only when every end of the series is positive. A company whose earliest or latest
  figure is not positive has no meaningful rate, so the metric is empty and flagged [MET-04].
- The series is the filing's own fiscal-year columns, so a company's rate reflects what that filing
  reported, not a rebuilt series [PER-04].
- A series that spans the adoption of the lease standard, or a lender's net income series that spans the
  credit-loss standard, is kept and flagged: the rate is real, but it compares two accounting regimes
  [PER-12], [PER-13], [PER-14].

## What an empty value means

- `growth_base_not_positive` — an end of the series is zero or negative, so the rate would be meaningless
  [MET-04].
- `missing_input` — a fiscal year the filing does not report. An empty value is never a zero [MET-00].
- `not_applicable` — the company type makes the rate meaningless, or the filing carries no multi-year
  comparison [APP-02], [PER-04].
- `withheld (<rule>)` — a value the pipeline does not trust [A-04].

## What is not applicable, and why

- A company whose filing carries no usable multi-year series: the rate is not applicable [PER-04].
- A bank or an insurer: its revenue and cash-flow series do not carry the meaning they carry for an
  operating company, so those rates are not applicable [APP-02].
- A series whose base is not positive: not applicable rather than extreme [MET-04].
