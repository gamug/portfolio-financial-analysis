# Profitability

The profitability metrics are already computed: `gross_margin`, `operating_margin`, `net_margin`,
`return_on_assets` and `return_on_equity`. Read them; do not recompute them and do not ask for filing text.

## What the numbers are

- `gross_margin` is the margin [MD-06] defines: `GrossProfit` when the filing reports it, else revenue less the
  cost of revenue [MD-06].
- `operating_margin` is operating income over revenue [MD-07]. `net_margin` is parent net income over revenue
  [MD-08], and its numerator is the earnings attributable to the parent, not the consolidated profit [ID-03].
- `return_on_assets` is NOPAT over total assets at the end of the prior year [MD-09]; `return_on_equity` is
  parent net income over parent equity at the same date [MD-10]. Both use the capital at the end of the prior
  fiscal year, never the ending capital [MET-02].
- NOPAT is taxed at the hypothetical rate the checklist declares, not at the rate a filing reports, so a
  filing's own tax lines do not reach the return [MET-03].
- A filed negative is a value, not a missing one; a wrong sign is rejected, never repaired [ID-11].

## What an empty value means

- `missing_input` — a numerator or a denominator the filing does not report. An empty value is never a zero
  and never a neutral reading [MET-00].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].
- `prior_year_missing` — the capital at the end of the prior fiscal year is not stored [MET-02].
- `denominator_not_positive` — a denominator of zero has no ratio, and a negative denominator leaves the
  ratio out of the continuous reading and flags it [MET-09].

## What a withheld value means

- `withheld (<rule>)` — the screen named in the parentheses does not trust the value, so the code withholds
  it rather than letting it be read: the net margin's calibrated band [A-01], or a revenue-denominated metric
  whose revenue does not resolve [A-04].
