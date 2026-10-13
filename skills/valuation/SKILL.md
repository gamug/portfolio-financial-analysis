# Valuation

The valuation metrics are already computed: `market_capitalization`, `enterprise_value`,
`free_cash_flow_yield`, `enterprise_fcf_yield` and `sbc_adjusted_fcf_yield`. Read them; do not recompute them
and do not ask for filing text.

## What the numbers are

- `market_capitalization` is the close on a date times the latest share count publicly available on it — the
  cover-page count of the latest filing available by then [MD-34], [MKT-01]. As a stored metric the date is
  the filing's period end; VALORIZATION, the metric that ranks the universe, uses the analysis date
  [MKT-01].
- The count is the total basic count of every class, never a weighted-average count, basic or diluted
  [MKT-04]. The count and the price are on the same corporate-action basis [MKT-02], and a company with more
  than one class is valued class by class, an unlisted class through its conversion ratio to a listed one
  [MKT-03].
- `enterprise_value` adds debt and the noncontrolling interest to the market cap and nets the cash and
  short-term investments [MD-35]; it is empty rather than zero when one of those terms is missing, and so is
  any yield that divides by it [MD-35], [MET-00].
- `free_cash_flow_yield` is free cash flow over market cap [MD-36]; `enterprise_fcf_yield` reads the same
  firm's cash generation against the whole business [MD-38].
- `sbc_adjusted_fcf_yield` is the project's own measure of what share-based compensation does to the yield:
  a project measure, not a standard's [MD-37].
- An earnings yield with negative earnings is not a continuous value: the company carries a negative-earnings
  flag and only the positive part is used [MET-05].
- A REIT's operating and earnings measures are flagged, because real-estate depreciation depresses them
  [APP-04]; the sector's own earnings measure works from GAAP net income without the real-estate depreciation
  [C1_Nareit_FFO_White_Paper_2018 p.2], [C6 p.10].
- For a financial firm, debt is more akin to raw material than to a source of capital, and the notion of cost
  of capital and enterprise value may be meaningless as a consequence [C2 p.5]; its cash flows cannot be
  separated from its operations either [C2 p.14].

## What an empty value means

- `missing_input` — a share count, a price, a debt or a cash figure the filing or the price table does not
  report. An empty value is never a zero and never a neutral reading [MET-00].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].
- `capture_failure` — the market cap could not be formed from what the code could read [ID-02b].
- `prior_year_missing` — the cover count the market cap needs lies outside the window the reader can see
  [MKT-01].

## What a withheld value means

- `withheld (<rule>)` — the screen named in the parentheses does not trust the value: a market cap far
  outside its plausible range against total assets [A-03], or a free-cash-flow yield far outside its own
  [A-05].
