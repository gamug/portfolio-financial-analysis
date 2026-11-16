# Request to `portfolio-data-mining`: GICS sector and sub-industry for the S&P 500 leavers

**From:** `portfolio-financial-analysis`, stream B, 2026-10-14. **Raised by:** T-153 part 3 (the historical
universe; the source decided by reviewer A, 2026-10-14). **Blocks:** this repo's `T-100` (the full-universe run
on the historical universe). It does not block the final pilot (`T-143`).

## What is missing

`universe_history.db` (data-mining PR #49) has `gics_sector` and `gics_sub_industry` columns. They are filled
for the 503 `live_snapshot` rows and **empty for every `wikipedia_changes_backfill` row**. So no company that left
the index carries a classification.

Inside the backtest window (a membership that ended on or after 2022-01-01), that is **90 stints of 86
companies** (86 CIKs) that are not members today. The table at the end lists them. The 6 other CIKs with a stint
that ended in the window are members today (FB, WLTW, RE, FLT and SATS were renamed; ILMN left and came back),
and they keep today's classification.

## What was measured

Read-only, on `universe_history.db` (sha1 `fddbdccb71ab4243a0fb76b3027774474d352b99`), 2026-10-14:

```
rows by source:            live_snapshot 503, wikipedia_changes_backfill 391
stints ended >= 2022-01-01: 96 (92 CIKs); of those, CIKs not in today's list: 86, with 90 stints
gics_sector NULL:          96 of 96   gics_sub_industry NULL: 96 of 96
stints ended before 2023-03-17 (the GICS structure change): 22, of 20 CIKs
```

## What we need

For each of the 90 stints, the **GICS sector and sub-industry as Wikipedia's "List of S&P 500 companies" showed
them on the company's last day in the index**. Put them in the stint's existing `gics_sector` and
`gics_sub_industry` columns.

1. **Which revision.** Use the **last** revision of the page that still lists the company and is dated **on or
   before** the stint's `valid_to`. The edit that removes a company can land on the exit day or before it, so
   "the revision on `valid_to`" may no longer list it.
2. **Which row.** Find the company's row in that revision by **CIK** (the page's CIK column) and check the symbol.
   For Discovery (CIK 0001437107), DISCA and DISCK are separate stints. Take each from its own row.
3. **The label as written.** Copy the sector and sub-industry names exactly as that revision shows them, in the
   same form as the `live_snapshot` rows (names, not codes). This repo's company profile keys on the names.
   **Do not translate** a label from before the 2023-03-17 GICS change into today's structure. This repo
   resolves each such label per company from its own 10-K.
4. **The citation.** Record the revision id and its timestamp for every filled stint, the way L-11 cites the
   patch table's rows: new columns or a side table, whichever fits the builder. A filled stint without a
   revision id cannot be used here.
5. **What could not be filled.** If a company is missing from every revision on or before its `valid_to`, or
   the page's CIK column disagrees with ours, leave the stint empty and list it with the reason. Do not fill it
   from another source.

Please deliver it as a new `universe_history.db` with its sha1, and the list from point 5.

This repo cross-checks every delivered label against the SEC SIC code in each company's submissions file, but
only as a check, never as a source. It looks at REIT (6798), utilities (49xx), insurers (63xx) and banks
(602x). We open any disagreement ourselves and will tell you if one turns out to be a wrong row or revision.

## Why

`T-100` runs the whole pipeline over the **historical** S&P 500 universe, including the companies that later
left it (checklist PER-10 / PER-11 and L-11), so the backtest has no survivorship bias. The company type
(APP-00), the regulated-utility exemption (D-13), the REIT rules and the SECTOR score are all keyed on the GICS
sub-industry. A leaver with no classification gets none of them. The insurers, other financial firms, REITs and
utilities among the 86 would then be scored as plain operating companies, so the replay's figures by sector and
type would be wrong for exactly the companies PER-11 adds.

L-03 already uses a live snapshot (today's classification, applied backwards) for current members. A leaver has
no entry in today's list, so the closest equivalent is its classification on its last day in the index. This
repo declares that in `docs/model_fixes.md`'s T-153 entry: leavers carry the classification in force on their
last membership day; current members carry today's snapshot; a reclassification after a company's exit is not
applied.

## The 90 stints

From `universe_history.db`: every stint with `valid_to` on or after 2022-01-01 whose CIK has no open stint today.
"Pre-2023" marks a `valid_to` before 2023-03-17. Those labels may follow the old GICS structure.

| Symbol | CIK | Company | valid_from | valid_to | Pre-2023 |
|---|---|---|---|---|---|
| GPS | 0000039911 | Gap | 1976-07-01 | 2022-02-02 | yes |
| XLNX | 0000743988 | Xilinx | 1976-07-01 | 2022-02-14 | yes |
| INFO | 0001598014 | IHS Markit | 2017-06-02 | 2022-03-01 | yes |
| PBCT | 0001378946 | People's United Financial | 1976-07-01 | 2022-04-03 | yes |
| DISCA | 0001437107 | Discovery, Inc. | 1976-07-01 | 2022-04-10 | yes |
| DISCK | 0001437107 | Discovery, Inc. | 2014-08-06 | 2022-04-10 | yes |
| CERN | 0000804753 | Cerner | 2010-04-29 | 2022-06-07 | yes |
| IPGP | 0001111928 | IPG Photonics | 2018-03-07 | 2022-06-20 | yes |
| UA | 0001336917 | Under Armour(Class C) | 2016-04-08 | 2022-06-20 | yes |
| UAA | 0001336917 | Under Armour(Class A) | 1976-07-01 | 2022-06-20 | yes |
| PENN | 0000921738 | Penn Entertainment | 2021-03-22 | 2022-09-18 | yes |
| PVH | 0000078239 | PVH | 2013-02-15 | 2022-09-18 | yes |
| CTXS | 0000877890 | Citrix Systems | 1976-07-01 | 2022-10-02 | yes |
| DRE | 0000783280 | Duke Realty | 2017-07-26 | 2022-10-02 | yes |
| NLSN | 0001492633 | Nielsen Holdings | 2013-07-08 | 2022-10-11 | yes |
| TWTR | 0001418091 | Twitter | 2018-06-07 | 2022-10-31 | yes |
| FBHS | 0001519751 | Fortune Brands Home & Security | 2016-06-22 | 2022-12-18 | yes |
| MBC | 0001941365 | MasterBrand | 2022-12-15 | 2022-12-18 | yes |
| ABMD | 0000815094 | Abiomed | 2018-05-31 | 2022-12-21 | yes |
| VNO | 0000899689 | Vornado Realty Trust | 1976-07-01 | 2023-01-04 | yes |
| SBNY | 0001288784 | Signature Bank | 2021-12-20 | 2023-03-14 | yes |
| SIVB | 0000719739 | SVB Financial Group | 2018-03-19 | 2023-03-14 | yes |
| LUMN | 0000018926 | Lumen Technologies | 1976-07-01 | 2023-03-19 | |
| FRC | 0001132979 | First Republic Bank | 2019-01-02 | 2023-05-03 | |
| DISH | 0001001082 | Dish Network | 2017-03-13 | 2023-06-19 | |
| FTRE | 0001965040 | Fortrea Holdings | 2023-07-03 | 2023-07-05 | |
| PHIN | 0001968915 | PHINIA | 2023-07-05 | 2023-07-05 | |
| AAP | 0001158449 | Advance Auto Parts | 2015-07-08 | 2023-08-24 | |
| LNC | 0000059558 | Lincoln National Corporation | 1976-07-01 | 2023-09-17 | |
| NWL | 0000814453 | Newell Brands | 1976-07-01 | 2023-09-17 | |
| DXC | 0001688568 | DXC Technology | 2017-04-04 | 2023-10-02 | |
| ATVI | 0000718877 | Activision Blizzard | 2015-08-28 | 2023-10-17 | |
| OGN | 0001821825 | Organon & Co. | 2021-06-03 | 2023-10-17 | |
| ALK | 0000766421 | Alaska Air Group | 2016-05-13 | 2023-12-17 | |
| SEDG | 0001419612 | SolarEdge | 2021-12-20 | 2023-12-17 | |
| SEE | 0001012100 | Sealed Air | 1976-07-01 | 2023-12-17 | |
| CDAY | 0001725057 | Ceridian | 2021-09-20 | 2024-01-31 | |
| WHR | 0000106640 | Whirlpool Corporation | 1976-07-01 | 2024-03-17 | |
| ZION | 0000109380 | Zions Bancorporation | 1976-07-01 | 2024-03-17 | |
| VFC | 0000103379 | VF Corporation | 1976-07-01 | 2024-04-02 | |
| XRAY | 0000818479 | Dentsply Sirona | 1976-07-01 | 2024-04-02 | |
| PXD | 0001038357 | Pioneer Natural Resources | 1976-07-01 | 2024-05-07 | |
| CMA | 0000028412 | Comerica | 1976-07-01 | 2024-06-23 | |
| RHI | 0000315213 | Robert Half | 1976-07-01 | 2024-06-23 | |
| WRK | 0001732845 | WestRock | 2015-07-01 | 2024-07-07 | |
| AAL | 0000006201 | American Airlines Group | 2015-03-23 | 2024-09-22 | |
| BIO | 0000012208 | Bio-Rad Laboratories | 2020-06-22 | 2024-09-22 | |
| ETSY | 0001370637 | Etsy | 2020-09-21 | 2024-09-22 | |
| BBWI | 0000701985 | Bath & Body Works, Inc. | 1976-07-01 | 2024-09-30 | |
| MRO | 0000101778 | Marathon Oil | 1976-07-01 | 2024-11-25 | |
| AMTM | 0002011286 | Amentum | 2024-09-30 | 2024-12-22 | |
| CTLT | 0001596783 | Catalent | 2020-09-21 | 2024-12-22 | |
| QRVO | 0001604778 | Qorvo | 2015-06-11 | 2024-12-22 | |
| BWA | 0000908255 | BorgWarner | 2011-12-16 | 2025-03-23 | |
| CE | 0001306830 | Celanese | 2018-12-24 | 2025-03-23 | |
| FMC | 0000037785 | FMC Corporation | 2009-08-19 | 2025-03-23 | |
| TFX | 0000096943 | Teleflex | 2019-01-18 | 2025-03-23 | |
| DFS | 0001393612 | Discover Financial | 2007-07-02 | 2025-05-18 | |
| JNPR | 0001043604 | Juniper Networks | 2006-06-02 | 2025-07-08 | |
| ANSS | 0001013462 | Ansys | 2017-06-19 | 2025-07-17 | |
| HES | 0000004447 | Hess Corporation | 1976-07-01 | 2025-07-22 | |
| WBA | 0001618921 | Walgreens Boots Alliance | 1976-07-01 | 2025-08-27 | |
| CZR | 0001590895 | Caesars Entertainment | 2021-03-22 | 2025-09-21 | |
| ENPH | 0001463101 | Enphase Energy | 2021-01-07 | 2025-09-21 | |
| MKTX | 0001278021 | MarketAxess | 2019-07-01 | 2025-09-21 | |
| KMX | 0001170010 | CarMax | 2010-06-28 | 2025-10-30 | |
| EMN | 0000915389 | Eastman Chemical Co. | 1976-07-01 | 2025-11-03 | |
| IPG | 0000051644 | Interpublic Group | 1976-07-01 | 2025-11-27 | |
| K | 0000055067 | Kellanova | 1976-07-01 | 2025-12-10 | |
| LKQ | 0001065696 | LKQ Corporation | 2016-05-23 | 2025-12-21 | |
| MHK | 0000851968 | Mohawk Industries | 2013-12-23 | 2025-12-21 | |
| SOLS | 0002064953 | Solstice Advanced Materials | 2025-10-30 | 2025-12-21 | |
| DAY | 0001725057 | Dayforce | 2024-02-01 | 2026-02-08 | |
| LW | 0001679273 | Lamb Weston | 2018-12-03 | 2026-03-22 | |
| MOH | 0001179929 | Molina Healthcare | 2022-03-02 | 2026-03-22 | |
| MTCH | 0000891103 | Match Group | 2021-09-20 | 2026-03-22 | |
| PAYC | 0001590955 | Paycom | 2020-01-28 | 2026-03-22 | |
| HOLX | 0000859737 | Hologic | 2016-03-30 | 2026-04-08 | |
| CTRA | 0000858470 | Coterra Energy | 1976-07-01 | 2026-05-06 | |
| EPAM | 0001352010 | EPAM Systems | 2021-12-14 | 2026-06-01 | |
| CPB | 0000016732 | Campbell's | 1976-07-01 | 2026-06-21 | |
| POOL | 0000945841 | Pool Corporation | 2020-10-07 | 2026-06-21 | |
| CAG | 0000023217 | Conagra Brands | 1976-07-01 | 2026-06-29 | |
| EA | 0000712515 | Electronic Arts | 1976-07-01 | 2026-08-04 | |
| AVB | 0000915912 | AvalonBay Communities | 2007-01-10 | 2026-08-17 | |
| BLDR | 0001316835 | Builders FirstSource | 2023-12-18 | 2026-09-20 | |
| TAP | 0000024545 | Molson Coors | 1976-07-01 | 2026-09-20 | |
| TTD | 0001671933 | The Trade Desk | 2025-07-18 | 2026-09-20 | |
| CTVA | 0001755672 | Corteva | 2019-06-03 | 2026-10-05 | |
| WBD | 0001437107 | Warner Bros. Discovery | 2022-04-11 | 2026-10-05 | |
