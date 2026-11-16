"""Call the gateway's own filed-fact query on F1 examples (live EDGAR, research User-Agent, a few requests)."""

import json
import os
import sys

sys.path.insert(0, sys.argv[2] + "/src")
os.environ.setdefault("EDGAR_IDENTITY", "research@example.com")
import edgar

edgar.set_identity("research@example.com")
from sec_edgar.agent import _filed_nondimensional_values

for t, acc, form, end, item, concept, days, val in json.load(open(sys.argv[1])):
    x = edgar.find(acc).xbrl()
    kind = None if days is None else ("FY" if days >= 351 else "Q")
    got = _filed_nondimensional_values(x, f"us-gaap:{concept}", end, kind)
    if not got:
        got = _filed_nondimensional_values(x, f"us-gaap_{concept}", end, kind)
    print(
        t,
        form,
        end,
        item,
        concept,
        "expected",
        val,
        "gateway returns",
        got,
        "OK" if any(abs(g - val) < 1 for g in got) else "MISMATCH",
    )
