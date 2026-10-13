# Valuation

The valuation metrics are already computed: `market_capitalization`, `enterprise_value`,
`free_cash_flow_yield`, `enterprise_fcf_yield` and `sbc_adjusted_fcf_yield`. Read them; do not recompute
them and do not ask for filing text. A yield compares the cash a company generates with the price the market
puts on it, so a higher yield is the cheaper reading [MD-36], [MD-38].

## How to read them

- `market_capitalization` is the price of the equity: the share count the filing's cover page made public on
  the analysis date, times the close. The count is the total basic count, never a weighted average, and a
  split between the count's date and the price's date is adjusted for [MKT-01], [MKT-04], [MKT-02].
- A company with more than one class of shares has a count per class; the value uses each class's own count
  and price rather than one class's [MKT-03].
- `enterprise_value` is the value of the business as a whole rather than of its equity alone, so it includes
  the noncontrolling interest and nets the cash and short-term investments when the filing reports them
  apart from cash [MD-35], [MD-02].
- An enterprise value is empty, never zero, when a term it needs is missing: a company whose debt or cash
  does not resolve has no enterprise value, and a yield that divides by it has none either [MET-00],
  [ID-02b].
- `free_cash_flow_yield` and `enterprise_fcf_yield` read the same cash generation against the two capital
  bases; read them together to separate an equity view from a whole-business view [MD-36], [MD-38].
- `sbc_adjusted_fcf_yield` is the project's own measure of what share-based compensation does to the yield.
  It is a project measure, not a standard's; say that rather than reasoning about dilution [MD-37].
- An earnings yield with negative earnings is not a continuous value: the company carries a negative-earnings
  flag and only the positive part is used [MET-05], [C4 p.13], [C4 p.18].
- A REIT's GAAP earnings understate its operating performance, because depreciation of its property is
  deducted from income that is largely distributed [APP-04], [C1_Nareit_FFO_White_Paper_2018 p.2],
  [C6 p.10].
- A financial firm has no meaningful enterprise value: its debt is a raw material and its cash flows cannot
  be separated from its operations [APP-02], [C2 p.5], [C2 p.14].

## What an empty value means

- `missing_input` — a share count, a price, a debt or a cash figure the filing or the price table does not
  report. An empty value is never a zero and never a neutral reading [MET-00].
- `not_applicable` — a company type for which the value is meaningless [APP-02].
- `capture_failure` — the market cap could not be formed: a suspect share scale, a stale or incomplete cover
  count [MKT-01], [MKT-04].
- `prior_year_missing` — the cover count the market cap needs lies outside the window the reader can see
  [MKT-01].
- `withheld (<rule>)` — a value the pipeline does not trust: a market cap far outside its plausible range
  against total assets, or a free-cash-flow yield far outside its own, is withheld [A-03], [A-05].

## What is not applicable, and why

- A bank, a savings institution or an insurer: `enterprise_value` and the enterprise yield are not
  applicable [APP-02], [C2 p.5].
- A REIT: its earnings measure is not GAAP net income alone [APP-04].
