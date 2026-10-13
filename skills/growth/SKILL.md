# Growth

The growth metrics are already computed: `revenue_growth`, `operating_income_growth`, `net_income_growth`
and `free_cash_flow_growth`. Read them; do not recompute them and do not ask for filing text.

## What the numbers are

- Each is the change between two periods of the same filing, over the earlier one [MD-22]. The other three
  apply the same definition to operating income [MD-23], to parent net income [MD-24] and to free cash flow
  [MD-25].
- Both ends come from this filing's own statements: growth compares periods of the same filing, so the base
  is the comparative column this filing carries, whatever that filing restated [PER-04].
- The rate is empty when the base period is not positive, and flagged [MET-04].
- A base fiscal year before an accounting standard's adoption makes the figure non-comparable across two
  regimes, and it is flagged rather than dropped [PER-12]; the lease standard is the rule's own case for the
  leverage and asset metrics [PER-13], and the credit-loss standard the case for a lender's earnings growth
  [PER-14].
- A loss, or a fall in earnings, can be temporary, long-term, or a stage in the company's life; the numbers
  alone do not say which [C9 p.3].

## What an empty value means

- `growth_base_not_positive` — the base period is zero or negative, so the rate would be meaningless
  [MET-04].
- `missing_input` — the current or the base period's figure is not stored. An empty value is never a zero and
  never a neutral reading [MET-00].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].

## What a withheld value means

- `withheld (<rule>)` — the screen named in the parentheses does not trust the value: a revenue-based metric
  whose revenue does not resolve is withheld rather than read [A-04].
