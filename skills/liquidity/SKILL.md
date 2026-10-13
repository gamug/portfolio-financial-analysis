# Liquidity

The liquidity metrics are already computed: `current_ratio`, `quick_ratio` and `cash_ratio`. Read them; do not
recompute them and do not ask for filing text.

## What the numbers are

- `current_ratio` is current assets over current liabilities [MD-12]; `quick_ratio` is the same numerator
  without inventory [MD-13]; `cash_ratio` counts only cash and short-term investments [MD-14].
- Cash a filing reports inside another line is never counted a second time [MD-14].
- The three depend on the balance-sheet form: when a filing reports no current-asset and current-liability
  line, the code leaves them empty rather than computing them from other lines [APP-09], [MET-00].
- `quick_ratio` and `cash_ratio` are source-pending in the checklist's Annex B: no loaded source states their
  formula, so the code keeps its own until the second author is loaded [MD-13], [MD-14].

## What an empty value means

- `missing_input` — a current-asset or current-liability line the filing does not report. An empty value is
  never a zero and never a neutral reading [MET-00].
- `not_applicable` — the code's decision, from its applicability matrix, that the metric does not apply to
  this company; it is never the model's to decide [APP-00].
