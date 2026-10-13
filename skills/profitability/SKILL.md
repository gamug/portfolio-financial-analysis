# Profitability

The profitability metrics are already computed: `gross_margin`, `operating_margin`, `net_margin`,
`return_on_assets` and `return_on_equity`. Read them; do not recompute them and do not ask for filing text.
A margin is a share of revenue; a return is a ratio of earnings to the capital that produced them. A
negative value is a loss, not a missing number [MET-09].

## How to read them

- A margin or a return above the company's own history and its peers' is the stronger reading. The pipeline
  computes neither the cost of equity nor the cost of capital, so a return is never read against an assumed
  one [C7 p.4].
- `net_margin` uses parent earnings, so a company with a noncontrolling interest is not credited with income
  it does not own [ID-03].
- `return_on_assets` and `return_on_equity` divide by the capital at the end of the prior fiscal year, not
  the current one [MET-02]. `return_on_assets`' numerator is after a hypothetical tax, so it is comparable
  across companies whatever rate the filing shows [MET-03].
- `gross_margin` is read only when the filing reports a cost of revenue; a company that presents no such
  line has an empty margin, not a strong one [ID-02b], [MET-00].
- A large cash balance depresses `return_on_equity`: the cash earns a low, riskless return [C7 p.12]. Read a
  cash-rich company's return with that in mind.
- Negative book equity leaves `return_on_equity` without a reading; the return on invested capital is then
  the measure of the business [APP-07], [C7 p.12].
- A loss can be temporary, cyclical, or a stage in the company's life; the numbers alone cannot say which
  [C9 p.3].

## What an empty value means

- `missing_input` — a numerator or a denominator the filing does not report. An empty value is never a zero
  and never a neutral reading [MET-00].
- `not_applicable` — the company type makes the metric meaningless; the code decided this, not the filing
  [APP-01], [APP-02], [APP-03].
- `prior_year_missing` — the prior fiscal year's capital is not stored [MET-02].
- `denominator_not_positive` — the denominator is not positive, so the ratio has no reading [MET-09].
- `withheld (<rule>)` — a data-quality screen does not trust the value: a margin outside the calibrated
  band is withheld, and a value withheld for one metric is not read through another [A-01], [A-04].

## What is not applicable, and why

- A bank, a savings institution or an insurer: the margins, the returns and the asset turnover are not
  applicable, because its revenue and its debt do not carry the meaning they carry for an operating company
  [APP-01], [APP-02], [APP-03].
- A REIT: its earnings measure is not GAAP net income alone [APP-04], and its own tax treatment makes its
  return on assets read differently [APP-04c].
- Negative book equity: the return on equity is not applicable [APP-07].
