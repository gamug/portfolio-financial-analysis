# Sources cited by the specialist skills

Every source citation in a `skills/<group>/SKILL.md` has its verbatim quote here, with the page of the
local PDF and the bullet it backs. The documents are the loaded sources of
`docs/checklist_sec/sources_register.md` (not committed; `docs/checklist_sec/sec documentation/`), and the
quotes are verified against them with `scripts/sec_xcheck/quotecheck.py` (a local run -- the PDFs are not
committed, so this is not a CI gate). Where the checklist itself already quotes a source, the quote is the
checklist's own wording, so the two documents cannot drift apart.

A skill cites a source as `[C7 p.12]` (the register's document, then its PDF page). A citation that no
loaded source supports is not written: a statement with no backing is not a statement.

| Source | Page | Quote | Backs |
|---|---|---|---|
| C7 | p.4 | "In effect, this is what we are trying to do when we compute the return on invested capital and compare it to the cost of capital." | roic: a return on capital is read against the cost of capital |
| C7 | p.4 | "We can focus on just the equity invested in projects and measure the return on this equity investment; this would then have to be compared to the cost of equity." | profitability: a return on equity is read against the cost of equity |
| C7 | p.12 | "the return on equity for firms with significant cash balances will be depressed by the low and riskless returns earned by cash" | profitability: a large cash balance depresses the return on equity |
| C7 | p.12 | "When this occurs, the return on equity becomes a meaningless number and you may have to revert back to a return on invested capital." | profitability, roic: a negative book equity leaves the return on equity without a reading |
| C7 | p.19 | "Under the rationale that the products of research are too uncertain and difficult to quantify, accounting standards have generally required that R&D spending be expensed in the period in which they occur." | roic: an expensed research budget is not on the balance sheet as capital |
| C7 | p.19 | "Research expenses, notwithstanding the uncertainty about future benefits, should be capitalized." | roic: the adjustment our return on capital does not make |
| C7 | p.30 | "A long-term lease creates the same kind of obligations as debt, and it must be viewed in a similar light." | leverage, roic: an operating lease is debt |
| C8 | p.3 | "The geometric average is a much more accurate measure of true growth in past earnings, especially when year-to-year growth has been erratic." | cagr: the compound annual rate is a geometric average |
| C9 | p.3 | "There are several reasons why firms have negative or abnormally low earnings, some of which can be viewed as temporary, some of which are long-term, and some of which relate to where a firm stands in the life cycle." | profitability, growth: a loss's cause is not in the numbers |
| C4 | p.13 | "If earnings are positive, E(+)/P is the ratio of total earnings to market equity and E/P dummy is 0. If earnings are negative, E(+)/P is 0 and E/P dummy is 1." | valuation: an earnings yield with negative earnings |
| C4 | p.18 | "we use a dummy variable for E/P when earnings are negative." | valuation: the same |
| C2 | p.5 | "debt for a financial service firm is more akin to raw material than to a source of capital; the notion of cost of capital and enterprise value may be meaningless as a consequence." | leverage, roic, valuation: debt and enterprise value for a financial firm |
| C2 | p.14 | "Estimating cash flows prior to debt payments at a weighted average cost of capital is problematic, when debt and debt payments cannot be easily identified" | valuation: enterprise value for a financial firm |
| C2 | p.1 | "The first is that the cash flows to a financial service firm cannot be easily estimated, since items like capital expenditures, working capital and debt are not clearly defined." | cashflow: a financial firm's cash flows |
| C2 | p.8 | "the statement of cash flows to a bank show little or no capital expenditures and correspondingly low depreciation." | cashflow, efficiency: a bank's cash-flow statement |
| C1_Nareit_FFO_White_Paper_2018 | p.2 | "Net income (calculated in accordance with GAAP), excluding: Depreciation and amortization related to real estate." | valuation: a REIT's earnings under GAAP |
| C6 | p.10 | "Equity REITs’ financial viability is based on GAAP net income from continuing operations, and/or Funds From Operations (FFO), if reported." | valuation: the REIT earnings measure |
| C13 | p.19 | "Partly as a result of construction cycles, the utility sector has experienced prolonged periods of negative free cash flow – essentially, the sum of its dividends and its capital expenditures for maintenance and growth of its infrastructure frequently exceeds cash from operations, such that a portion of capital expenditures must routinely be financed by debt or equity." | cashflow: a regulated utility's negative free cash flow |
| C13 | p.11 | "In a sector that is typically free cash flow negative (due to large capital expenditures and dividends) and that routinely needs to refinance very large maturities of long-term debt" | cashflow: the same, as the sector's normal state |
| C14 | p.38 | "The industry was able to access supportive stock and bond markets to finance over $178 billion in capital spending." | cashflow: the utility sector funds its capital spending from the markets |
| C15 | p.2 | "continued negative discretionary cash flow" | cashflow: a utility's negative cash flow is expected, not a distress signal |
