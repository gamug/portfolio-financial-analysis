"""L-43 (reviewer A, 2026-10-29, point 2 -- MKT-03: "An unlisted class is valued through its conversion ratio to a listed
class, cited from the company's own filing and recorded with its accession"): for every filing with a multi-class cover of
the 54 companies whose market caps carry market_cap_incomplete_classes (l43_filings.json), the primary document on EDGAR
and in it (1) the cover's listed classes -- each dei:TradingSymbol / dei:Security12bTitle fact with its context's class
member -- and (2) every sentence of the visible text that states how a class converts or exchanges into another (convert,
conversion, exchange, one-for-one, share-for-share, equivalent number), with the conversion-rate figures near it. GET only,
a pause between requests; resumable (one JSON line per accession). Never writes the database.
usage: l43_read.py <l43_filings.json> <out.jsonl>"""

import html
import json
import os
import re
import sys
import time
import urllib.request

LIST, OUT = sys.argv[1:3]
UA = {"User-Agent": "research@example.com"}


def get(url):
    time.sleep(0.35)
    for attempt in range(4):
        try:
            return (
                urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120)
                .read()
                .decode("utf-8", "replace")
            )
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def primary(cik, acc, form):
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", get(f"{base}/{acc}-index.html"), re.S | re.I):
        cells = [
            re.sub(r"<[^>]+>", "", x).strip()
            for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        ]
        link = re.search(r'href="([^"]+\.htm[l]?)"', row, re.I)
        if len(cells) >= 4 and cells[3].upper().startswith(form.upper()) and link:
            href = link.group(1).replace("/ix?doc=", "")
            return "https://www.sec.gov" + href if href.startswith("/") else f"{base}/{href}"


CTX = re.compile(r"<xbrli:context[^>]*id=\"([^\"]+)\"[^>]*>(.*?)</xbrli:context>", re.S | re.I)
MEMBER = re.compile(
    r"<xbrldi:explicitMember[^>]*>\s*([^<\s]+)\s*</xbrldi:explicitMember>", re.S | re.I
)
DEI = re.compile(
    r"<ix:nonNumeric[^>]*name=\"dei:(TradingSymbol|Security12bTitle|NoTradingSymbolFlag)\"[^>]*contextRef=\"([^\"]+)\"[^>]*>(.*?)</ix:nonNumeric>"
    r"|<ix:nonNumeric[^>]*contextRef=\"([^\"]+)\"[^>]*name=\"dei:(TradingSymbol|Security12bTitle|NoTradingSymbolFlag)\"[^>]*>(.*?)</ix:nonNumeric>",
    re.S | re.I,
)
KW = re.compile(
    r"convertib|convert|conversion|exchangeab|one-for-one|one for one|share-for-share|share for share|"
    r"equivalent number|one-to-one",
    re.I,
)
CLS = re.compile(r"\bclass|\bseries|\bcommon stock|\bordinary share|\bunits?\b", re.I)


def sentences(text):
    """The sentence around each keyword (bounded to 500 characters each side; linear time)."""
    out, last_end = [], -1
    for m in KW.finditer(text):
        if m.start() < last_end:
            continue
        a = max(text.rfind(". ", max(0, m.start() - 500), m.start()), m.start() - 500, 0)
        b = text.find(". ", m.end(), m.end() + 500)
        b = m.end() + 500 if b < 0 else b + 1
        s = text[a:b].strip(". ").strip()
        last_end = b
        if CLS.search(s) and s not in out:
            out.append(s[:1000])
    return out


done = {json.loads(line)["acc"] for line in open(OUT)} if os.path.exists(OUT) else set()
todo = [x for x in json.load(open(LIST)) if x["acc"] not in done]
print(len(done), "done,", len(todo), "to read", flush=True)
fh = open(OUT, "a")
for x in todo:
    rec = dict(x)
    try:
        url = primary(x["cik"], x["acc"], x["form"])
        rec["document"] = url
        doc = get(url)
        members = {
            c.group(1): [m.group(1) for m in MEMBER.finditer(c.group(2))] for c in CTX.finditer(doc)
        }
        dei = []
        for m in DEI.finditer(doc):
            name, ctx, val = (
                (m.group(1), m.group(2), m.group(3))
                if m.group(1)
                else (m.group(5), m.group(4), m.group(6))
            )
            dei.append(
                (
                    name,
                    members.get(ctx, []),
                    html.unescape(re.sub(r"<[^>]+>", "", val)).strip()[:120],
                )
            )
        rec["dei"] = dei
        vis = re.sub(r"<ix:header>.*?</ix:header>", " ", doc, flags=re.S | re.I)
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", vis))).replace(" ", " ")
        rec["sentences"] = sentences(text)[:60]
    except Exception as exc:
        rec["error"] = str(exc)[:200]
    fh.write(json.dumps(rec) + "\n")
    fh.flush()
    print(
        x["t"],
        x["acc"],
        len(rec.get("dei", [])),
        "dei,",
        len(rec.get("sentences", [])),
        "sentences",
        rec.get("error", ""),
        flush=True,
    )
print("done", flush=True)
