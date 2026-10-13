# Growth

The growth metrics are already computed: `revenue_growth`, `operating_income_growth`, `net_income_growth`
and `free_cash_flow_growth`. Read them; do not recompute them and do not ask for filing text. Each compares
the filing's own period with the same period a year earlier, so a higher value is the stronger reading
[MET-04], [PER-04].

## How to read them

- Read the four together. Revenue growing while operating income and net income fall says the growth is
  costing margin; the cash-flow growth says whether it is funded by the business [MET-04], [MD-22], [MD-23],
  [MD-24], [MD-25].
- Growth is computed only when the base period is positive. A company that lost money a year ago has no
  meaningful growth rate, so the metric is empty and flagged rather than very large or very small [MET-04].
- The base is the filing's own comparative period, taken as it was first reported; a later restatement does
  not change it [PER-01], [PER-04].
- A base fiscal year that began before the adoption of the lease standard makes the growth figure span two
  accounting regimes, so the value is kept and flagged as crossing the adoption [PER-12], [PER-13].
- A lender's net income growth can cross the adoption of the credit-loss standard in the same way, and is
  flagged for it [PER-14].
- `revenue_growth` is withheld when a filing reports a net income but no revenue [A-04].
- A loss can be temporary, cyclical, or a stage in the company's life; a single year's growth cannot say
  which [C9 p.3].

## What an empty value means

- `growth_base_not_positive` — the base period is zero or negative, so the rate would be meaningless
  [MET-04].
- `missing_input` — the current or the base period's figure is not stored. An empty value is never a zero
  [MET-00].
- `not_applicable` — the company type makes the metric meaningless [APP-02].
- `withheld (<rule>)` — a value the pipeline does not trust [A-04].

## What is not applicable, and why

- A bank or an insurer: its revenue and its cash flows do not carry the meaning they carry for an operating
  company, so the revenue and cash-flow growth figures are not applicable [APP-02], [APP-02b].
- A company whose base period is not positive: the growth rate is not applicable rather than extreme
  [MET-04].
