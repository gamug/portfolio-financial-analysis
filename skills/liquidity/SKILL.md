# Liquidity

The liquidity metrics are already computed: `current_ratio`, `quick_ratio` and `cash_ratio`. Read them; do
not recompute them and do not ask for filing text. Each compares the assets a company can turn into cash
within the year with the obligations it must meet within the year; a higher value is the more comfortable
reading, and the three differ only in how much of the current assets they count [MD-12], [MD-13], [MD-14].

## How to read them

- The three ratios are read together. A comfortable `current_ratio` beside a thin `quick_ratio` says the
  comfort rests on inventory, which is the least certain of the current assets [MD-12], [MD-13].
- `cash_ratio` is the strictest reading: only cash and short-term investments count against the current
  liabilities [MD-14]. Cash that a filing reports inside another line is never counted a second time
  [ID-02a].
- The definitions of `quick_ratio` and `cash_ratio` are source-pending, so today's formula is kept until
  the second author is loaded [MD-13], [MD-14]. Read them as the pipeline's own measures, not as a
  standard's.
- A company that funds itself from deposits rather than from trade payables has no meaningful current
  ratio: the metric is not applicable to it [APP-01], [APP-02].

## What an empty value means

- `missing_input` — a current asset or current liability line the filing does not report. An empty value is
  never a zero [MET-00].
- `not_applicable` — the balance sheet is not classified, or the company type makes the ratio meaningless
  [APP-09], [APP-01].
- A value withheld by a data-quality screen is not read: the pipeline withholds a value it does not trust
  rather than scoring it [A-04].

## What is not applicable, and why

- An unclassified balance sheet: none of the three is applicable, because the filing separates no current
  assets from no current liabilities [APP-09].
- A bank or an insurer: not applicable, because its funding and its assets are not current in the operating
  sense the ratio assumes [APP-01], [APP-02].
