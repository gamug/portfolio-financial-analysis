# Leverage

The leverage metrics are already computed: `debt_to_equity`, `debt_to_assets`, `interest_coverage` and
`net_debt_to_ebitda`, with `ebitda` as the building block they use. Read them; do not recompute them and do
not ask for filing text.

## What the numbers are

- `debt_to_equity` is debt over parent equity [MD-15]; `debt_to_assets` is debt over total assets [MD-16].
- Debt is the filed debt lines, narrowest concept first [MET-07]. A component is never used as the total
  unless the code holds a map whose identity a source states; a short-term debt map is one of them [ID-02b].
  Operating leases are excluded from debt, which departs from the source that treats a lease as an obligation
  of the same kind as debt [L-01], [MET-07].
- A combined debt-and-lease element is used only when it is the company's only long-term debt line, and is
  then flagged [MET-07].
- `interest_coverage` is operating income over interest expense, in the filed sign [MD-17]. It is empty only
  when interest expense is not positive; with an operating loss and positive interest it is valid, because
  its denominator is positive, and lower still means worse [MET-09].
- `net_debt_to_ebitda` is net debt over EBITDA [MD-18], with `ebitda` the sum [MD-01] defines. A denominator of
  zero leaves it empty; a negative denominator leaves it out of the continuous reading and flags it [MET-09].

## What an empty value means

- `missing_input` — a debt, cash or interest line the filing does not report. An empty value is never a zero
  and never a neutral reading [MET-00].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].
- `denominator_not_positive` — a denominator of zero has no ratio, and a negative one leaves the ratio out of
  the continuous reading and flags it [MET-09].
- `capture_failure` — a line the code could not read from the filing, so the metric is empty rather than
  filled with a component [ID-02b].
