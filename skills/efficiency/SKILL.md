# Efficiency

The efficiency metrics are already computed: `asset_turnover`, `inventory_turnover` and
`receivables_turnover`. Read them; do not recompute them and do not ask for filing text. Each says how many
times a year the company turns a part of its balance sheet into revenue, so a higher value is the more
productive reading [MD-19], [MD-20], [MD-21].

## How to read them

- `asset_turnover` is the broadest reading: the revenue a company generates from the whole asset base. A
  capital-intensive company turns its assets slowly by nature; compare it with its own history and its peers,
  never with a company in another kind of business [MD-19].
- `inventory_turnover` and `receivables_turnover` describe the working capital a company ties up. A falling
  turnover means more inventory or more uncollected revenue per unit of sales, which is the weaker reading
  [MD-20], [MD-21].
- A company that sells services rather than goods has no inventory to turn, so `inventory_turnover` is not
  applicable to it rather than weak [APP-01], [APP-03].
- A bank or an insurer has no operating asset base of this kind, and its revenue does not resolve the way an
  operating company's does, so all three are not applicable [APP-01], [APP-02].
- `asset_turnover` is a revenue-based ratio: when a filing reports a net income but no revenue, the ratio is
  withheld rather than read [A-04].

## What an empty value means

- `missing_input` — a revenue, inventory or receivables line the filing does not report. An empty value is
  never a zero [MET-00].
- `not_applicable` — the company type or the balance sheet makes the ratio meaningless [APP-01], [APP-03],
  [APP-09].
- `withheld (<rule>)` — a value the pipeline does not trust, such as an asset turnover for a filing whose
  revenue does not resolve [A-04].

## What is not applicable, and why

- A bank, a savings institution or an insurer: all three ratios are not applicable [APP-01], [APP-02],
  [APP-03].
- A company with no inventory on its balance sheet: `inventory_turnover` is not applicable [APP-01].
- An unclassified balance sheet: the turnover ratios that depend on current items are not applicable
  [APP-09].
