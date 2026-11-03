"""Reviewer A's point 3 (2026-10-29): ID-07 for the filings the runs skipped with a period reason. ID-07: "the period end a
filing is keyed on equals its dei:DocumentPeriodEndDate, which in turn matches the EDGAR header periodOfReport, within
PER-08's tolerance" (A1 §3.1.7, p.32) -- PER-08: the two ends round to the same month end. Per accession: the EDGAR
filing index header's "Period of Report", the primary document's dei:DocumentPeriodEndDate (and dei:DocumentFiscalPeriodFocus,
DocumentFiscalYearFocus), and today's gateway payload periods (a GET of /financials, nothing stored). GET only.
usage: id07_check.py <out.json> <cik:form:year:accession> ..."""

import calendar
import html
import json
import re
import sys
import time
import urllib.request

OUT, *ITEMS = sys.argv[1:]
UA = {"User-Agent": "research@example.com"}
GW = "http://host.docker.internal:8000/edgar/edgar"


def get(url, timeout=120):
    time.sleep(0.4)
    return (
        urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
        .read()
        .decode("utf-8", "replace")
    )


def month_end(d):
    y, m, day = map(int, d[:10].split("-"))
    # PER-08: rounded to the nearest month end
    if day <= 15:
        m, y = (12, y - 1) if m == 1 else (m - 1, y)
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


out = []
for it in ITEMS:
    cik, form, year, acc = it.split(":")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
    rec = dict(cik=cik, form=form, accession=acc)
    try:
        idx = get(f"{base}/{acc}-index.html")
        m = re.search(r"Period of Report</div>\s*<div class=\"info\">([^<]+)</div>", idx)
        rec["period_of_report"] = m.group(1).strip() if m else None
        doc_url = None
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", idx, re.S | re.I):
            cells = [
                re.sub(r"<[^>]+>", "", x).strip()
                for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
            ]
            link = re.search(r'href="([^"]+\.htm[l]?)"', row, re.I)
            if len(cells) >= 4 and cells[3].upper() == form.upper() and link:
                href = link.group(1).replace("/ix?doc=", "")
                doc_url = "https://www.sec.gov" + href if href.startswith("/") else f"{base}/{href}"
                break
        rec["document"] = doc_url
        doc = get(doc_url) if doc_url else ""
        for k in ("DocumentPeriodEndDate", "DocumentFiscalPeriodFocus", "DocumentFiscalYearFocus"):
            mm = re.search(
                r'name="dei:' + k + r'"[^>]*>(.*?)</ix:non(?:Numeric|Fraction)>', doc, re.S | re.I
            )
            rec[k] = html.unescape(re.sub(r"<[^>]+>", "", mm.group(1))).strip() if mm else None
        try:
            gw = json.loads(
                get(
                    f"{GW}/financials/{cik}?form={form}&year={year}&accession_number={acc}",
                    timeout=300,
                )
            )
            data = gw.get("data") or {}
            per = set()
            for st in ("income_statement", "balance_sheet", "cash_flow"):
                s = data.get(st) or {}
                for k in s.get("periods") or s.get("columns") or []:
                    per.add(str(k))
            rec["gateway_success"] = gw.get("success")
            rec["gateway_periods"] = sorted(per)[:12]
            rec["gateway_keys"] = sorted(data)[:15]
        except Exception as exc:
            rec["gateway_error"] = str(exc)[:160]
    except Exception as exc:
        rec["error"] = str(exc)[:200]
    dpe = rec.get("DocumentPeriodEndDate")
    por = rec.get("period_of_report")
    if dpe and por:
        try:
            from datetime import datetime

            d = (
                datetime.strptime(dpe, "%B %d, %Y").date().isoformat()
                if not re.match(r"\d{4}-", dpe)
                else dpe
            )
        except ValueError:
            d = None
        rec["dpe_iso"] = d
        rec["id07_match"] = bool(d) and month_end(d) == month_end(por)
    out.append(rec)
    print(
        f"{acc} {form}: periodOfReport {por} | DocumentPeriodEndDate {dpe} ({rec.get('DocumentFiscalPeriodFocus')} "
        f"{rec.get('DocumentFiscalYearFocus')}) -> ID-07 {'match' if rec.get('id07_match') else 'NO MATCH'} | gateway "
        f"{rec.get('gateway_success')} {rec.get('gateway_keys')} {rec.get('gateway_error', '')}",
        flush=True,
    )
json.dump(out, open(OUT, "w"), indent=1)
