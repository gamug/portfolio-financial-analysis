#!/usr/bin/env python3
"""Check every tracked corporate-action quote is a contiguous verbatim span of its cited filing.

    uv run python scripts/check_event_quotes.py [--events PATH]

Read-only: fetches each cited filing from SEC (User-Agent ``research@example.com``), caches it
outside the repo (a temp dir), and for every row of the tracked list splits ``evidence_quote`` on
``" [...] "`` and requires **each segment** to be a contiguous span of the filing's text.

The comparison ignores everything but the letters and digits: the filing HTML is fetched, its tags
are stripped, entities unescaped, the text NFKC-normalized, then lowercased and reduced to
``[a-z0-9]`` only -- so spacing, punctuation and typography can never turn a real match into a miss.
A quote is one contiguous span, or several non-adjacent spans joined by ``" [...] "`` (each segment
verbatim, in order). Prints ``OK``/``MISS`` per row and the total.

A tool, not a test (the test suite stays hermetic); T-100's citation pass reuses it.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
import tempfile
import time
import unicodedata
import urllib.request
from pathlib import Path

from kg_schema.corporate_actions import CorporateActionEvent, read_events

UA = "research@example.com"
ELISION = " [...] "
_CACHE = Path(tempfile.gettempdir()) / "check_event_quotes"
_TAG = re.compile(r"(?s)<[^>]+>")
_SCRIPT = re.compile(r"(?is)<(script|style).*?</\1>")


def normalize(s: str) -> str:
    """Letters and digits only, lowercased: the comparison the reviewer specified."""
    s = html.unescape(s)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def fetch(url: str) -> str:
    _CACHE.mkdir(parents=True, exist_ok=True)
    path = _CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".htm")
    if not path.exists():
        request = urllib.request.Request(  # noqa: S310 - the tracked list's own SEC https URL
            url, headers={"User-Agent": UA}
        )
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            path.write_bytes(response.read())
        time.sleep(0.2)
    return path.read_text(errors="ignore")


def filing_text(url: str) -> str:
    body = _SCRIPT.sub(" ", fetch(url))
    return normalize(_TAG.sub(" ", body))


def segments_ok(quote: str, text: str) -> bool:
    """Every ``" [...] "``-separated segment is a contiguous span, in order."""
    position = 0
    segments = quote.split(ELISION)
    if not any(normalize(seg) for seg in segments):
        return False
    for segment in segments:
        needle = normalize(segment)
        if not needle:
            continue
        found = text.find(needle, position)
        if found < 0:
            return False
        position = found + len(needle)
    return True


def check(events: tuple[CorporateActionEvent, ...]) -> int:
    misses = 0
    cache: dict[str, str] = {}
    for event in events:
        try:
            text = cache.setdefault(event.evidence_url, filing_text(event.evidence_url))
            ok = segments_ok(event.evidence_quote, text)
        except Exception as exc:
            ok = False
            text = ""
            print(f"MISS {event.cik} {event.ticker} {event.ex_date}: fetch failed ({exc})")
        if ok:
            print(f"OK   {event.cik} {event.ticker} {event.ex_date}")
        else:
            if text:
                print(f"MISS {event.cik} {event.ticker} {event.ex_date} {event.evidence_url}")
            misses += 1
    print(f"\n{len(events)} rows: {len(events) - misses} OK, {misses} MISS")
    return 1 if misses else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", help="the tracked CSV (default: the packaged list)")
    args = parser.parse_args(argv)
    return check(read_events(args.events))


if __name__ == "__main__":
    sys.exit(main())
