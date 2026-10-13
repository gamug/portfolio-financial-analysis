# Leverage

The leverage metrics are already computed: `debt_to_equity`, `debt_to_assets`, `interest_coverage` and
`net_debt_to_ebitda`, with `ebitda` as the building block they use. Read them; do not recompute them and do
not ask for filing text. A higher debt ratio, or a thinner coverage, is the more stretched reading.

## How to read them

- Debt is the debt the filing reports: its long-term and its short-term lines [MET-07]. An operating lease
  is an obligation of the same kind as debt, but it is not added to it, so a lease-intensive company reads
  less leveraged here than its obligations warrant [L-01], [C7 p.30].
- `debt_to_equity` is not applicable when book equity is negative, and nothing follows from the sign of
  equity alone [APP-07].
- `interest_coverage` is a valid reading even when operating income is negative, as long as interest expense
  is positive: a loss-making company that covers its interest less well than before is the worse credit, not
  an absent one [MET-09]. It is empty when interest expense is not positive; a negative filed interest
  expense is a valid filed value and is never used as a divisor [ID-11], [MET-09].
- `net_debt_to_ebitda` is empty when `ebitda` is negative, and empty when it is zero; a company whose net
  debt is negative (a net cash position) has a valid negative reading, flagged as such, not a missing one
  [MET-09], [MD-03].
- A financial firm's debt is a raw material, not a source of capital, so its leverage ratios do not carry
  the meaning they carry for an operating company [APP-02], [C2 p.5].

## What an empty value means

- `missing_input` — a debt, cash or interest line the filing does not report. An empty value is never a zero
  [MET-00], [ID-02b].
- `not_applicable` — negative book equity, or a company type for which the ratio is meaningless [APP-07],
  [APP-02].
- `denominator_not_positive` — the denominator is zero or negative, so the ratio has no reading [MET-09].
- `withheld (<rule>)` — a value the pipeline does not trust: a negative-equity filing whose net debt/EBITDA
  or interest coverage also looks distressed has its debt-to-equity and coverage withheld [APP-07].

## What is not applicable, and why

- Negative book equity: `debt_to_equity` is not applicable [APP-07].
- A bank or an insurer: the debt ratios are not applicable, and enterprise value with them, because its
  deposits are not debt in the sense the ratio assumes [APP-02], [C2 p.5], [C2 p.14].
- A REIT that reports only unsecured and secured debt: no debt line resolves, so the debt ratios are empty
  rather than low [ID-02b]. A company with no debt line at all on a complete balance sheet has zero debt,
  flagged as such [ID-02b].
