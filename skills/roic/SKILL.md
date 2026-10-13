# Return on invested capital

The metrics are already computed: `return_on_invested_capital`,
`return_on_invested_capital_effective_rate` and `effective_tax_rate`, with `nopat` and `invested capital` as
the building blocks the first uses [MD-11], [MD-05], [MD-02]. Read them; do not recompute them and do not ask
for filing text.

## What the numbers are

- `return_on_invested_capital` is NOPAT over invested capital at the end of the prior year [MD-11]; the
  capital is the sum [MD-02] defines, at that same date [MD-02], [MET-02].
- NOPAT is after a hypothetical tax at the rate the checklist declares, not at a rate the filing reports
  [MET-03]. A REIT is outside that corporate rate: its own effective rate is used when its pre-tax income is
  positive, and a tax of zero otherwise; the declared rate is never applied to one [APP-04c].
- `return_on_invested_capital_effective_rate` is the same return computed at the filing's own effective rate
  instead of the declared one — the robustness pair the checklist asks for, so a reading can be seen with and
  without the rate choice [MET-03].
- A return is read against the cost of the capital that funds it: what the measure is for is comparing the
  return on invested capital with the cost of capital [C7 p.4].
- Two distortions are not adjusted for. Expensed research is capital that the balance sheet does not show
  [C7 p.19]. A lease is treated by the source as an obligation of the same kind as debt, and operating leases
  are excluded from this code's debt [C7 p.30], [L-01].
- The return is read only when invested capital is positive [APP-07].
- `effective_tax_rate` is the filing's own rate — income tax over pre-tax income, in the filed sign [MD-30] —
  and the checklist declares that it is empty when pre-tax income is not positive [L-05].

## What an empty value means

- `missing_input` — an operating income, tax or capital line the filing does not report. An empty value is
  never a zero and never a neutral reading [MET-00].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].
- `denominator_not_positive` — a denominator of zero has no ratio, and a negative one leaves the ratio out of
  the continuous reading and flags it [MET-09].
- `prior_year_missing` — the capital at the end of the prior fiscal year is not stored [MET-02].
- `capture_failure` — a line the code could not read from the filing, so the metric is empty rather than
  filled with a component [ID-02b].
