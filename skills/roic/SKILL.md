# Return on invested capital

The metrics are already computed: `return_on_invested_capital`,
`return_on_invested_capital_effective_rate` and `effective_tax_rate`, with `nopat` and `invested capital` as
the building blocks the first uses. Read them; do not recompute them and do not ask for filing text. The
return on invested capital is the profit a company earns on all the capital it employs, debt and equity
together, so a higher value is the stronger reading [MD-11], [MET-02].

## How to read it

- The return is read against the cost of the capital that funds it: a return below that cost destroys value,
  a return above it creates it. The pipeline computes no cost of capital, so never assume one; compare the
  company with its own history and its peers [C7 p.4].
- The numerator is after a hypothetical tax at the rate the checklist declares, not the rate the filing
  happens to show, so the return is comparable across companies and is not moved by a filing's tax-line
  defects [MET-03], [MD-05].
- A REIT is the exception: its own effective rate is used, because it distributes the income the corporate
  rate would tax [APP-04c].
- The denominator is the capital at the end of the prior fiscal year, not the current one [MET-02].
- The return is read only when invested capital is positive. A company with non-positive invested capital has
  no meaningful return; that is a not-applicable reading, not an exceptionally strong one [APP-07].
- Two distortions are not adjusted for, and both make a research- or lease-intensive company's return look
  worse or better than the economics warrant. Expensed research is capital the balance sheet never shows
  [C7 p.19]. An operating lease is an obligation of the same kind as debt, and it is not added [L-01],
  [C7 p.30].
- `effective_tax_rate` is the filing's own rate and is empty when pre-tax income is not positive: no source
  defines a rate there, and it is never replaced by a default [L-05], [MD-30].
- `return_on_invested_capital_effective_rate` is the same return computed at the filing's own rate instead
  of the declared one; read the pair to see how much of the return is a tax choice [MET-03].
- A bank's capital is not invested in the operating assets this return assumes, so it does not carry the
  meaning it carries for an operating company [APP-02], [C2 p.5].

## What an empty value means

- `missing_input` — an operating income, tax or capital line the filing does not report. An empty value is
  never a zero [MET-00], [ID-02b].
- `denominator_not_positive` — invested capital is not positive, so the return has no reading [APP-07].
- `prior_year_missing` — the prior fiscal year's capital is not stored [MET-02].
- `not_applicable` — a company type for which the return is meaningless [APP-02].

## What is not applicable, and why

- A bank, a savings institution or an insurer: the return is not applicable, and the enterprise value with
  it [APP-02], [C2 p.5].
- Non-positive invested capital: not applicable [APP-07].
