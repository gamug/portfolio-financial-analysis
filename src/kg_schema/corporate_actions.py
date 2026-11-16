"""The corporate-action contract shared across packages (T-153 part 3d).

``corporate_action`` is one table with two writers: ``quant backfill-actions`` (the pricing
gateway's dividends and splits) and ``pricing_agent import-tiingo`` (the private Tiingo file's).
The engine-version literals live here so both writers and ``quant``'s reader agree without any
package importing another (``quant`` imports ``kg_schema`` only, SPEC FR-009).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .identity import normalize_cik

#: The pricing gateway's engines, newest first (T-085).
GATEWAY_ENGINES: tuple[str, ...] = ("corpact-v2", "corpact-v1")

#: The private Tiingo import's own build (T-153 part 3d, D6). A distinct engine, so the gateway's
#: engines keep their meaning and the gateway wins when a line comes back (rule 2): quant reads it
#: *after* :data:`GATEWAY_ENGINES`.
TIINGO_ENGINE = "corpact-tiingo-v1"

#: Every engine a reader should fold in, newest-priority first.
READABLE_ENGINES: tuple[str, ...] = (*GATEWAY_ENGINES, TIINGO_ENGINE)

#: The vendor markers a row can carry in ``corporate_action.source`` and ``price_daily.source``.
GATEWAY_SOURCE = "pricing-gateway"
TIINGO_SOURCE = "tiingo"


# -- the event list (D10) ---------------------------------------------------

#: The tracked, cited list of the events behind every recorded SPLIT row. One row per event,
#: each citing the company's own filing; see the file's own header for why the class matters.
EVENTS_CSV = Path(__file__).resolve().parent / "data" / "corporate_action_events.csv"

#: An event that multiplies the count (the price series' basis changes with it).
SPLIT = "split"
#: An event that divides the count (a true split's mirror image).
REVERSE_SPLIT = "reverse_split"
#: An event where the *price* changes and the count does not (a distribution of another
#: company's shares to the parent's holders).
SPINOFF = "spinoff"
#: A recorded factor that a single class cannot describe: the vendor folded a reverse split and
#: a spin-off into one ratio (DD 2019-06-03, 0.4725 = a 1-for-3 reverse split and the 1-for-3
#: Corteva distribution). The recorded value is the vendor's own price adjustment, which no
#: filing states, so it is never applied: a cap the event would enter is **refused**
#: (``market_cap.COMBINED_ACTION``) rather than valued from a decomposed guess (T-153 part 3d,
#: D10, reviewer A 2026-10-18).
REVERSE_SPLIT_SPINOFF = "reverse_split_spinoff"
#: The mirror combined class (T-143 part A item 9): a split-like factor that folds a separation and
#: a share reclassification (MTCH 2020-07-01, 3.502 = 2.1584 x 1.622; no filing states the 1.622).
#: Treated exactly like :data:`REVERSE_SPLIT_SPINOFF` -- see :data:`COMBINED_CLASSES`.
SPLIT_SPINOFF = "split_spinoff"

#: The classes whose recorded factor folds more than one event, so no single filing states it. The
#: factor is never applied; a cap the event would enter is refused (``market_cap.COMBINED_ACTION``).
COMBINED_CLASSES = (REVERSE_SPLIT_SPINOFF, SPLIT_SPINOFF)

EVENT_CLASSES = (SPLIT, REVERSE_SPLIT, SPINOFF, *COMBINED_CLASSES)


@dataclass(frozen=True)
class CorporateActionEvent:
    """One recorded event, classed and cited (a row of :data:`EVENTS_CSV`)."""

    cik: str
    ticker: str
    ex_date: str
    value: float
    event_class: str
    evidence_form: str
    evidence_url: str
    evidence_date: str
    evidence_quote: str
    rules: str


@lru_cache(maxsize=1)
def read_events(path: str | None = None) -> tuple[CorporateActionEvent, ...]:
    """Every row of the tracked list, validated (MKT-02: an uncited row is not a row)."""
    source = Path(path) if path else EVENTS_CSV
    rows: list[CorporateActionEvent] = []
    for row in csv.DictReader(_records(source)):
        event_class = row["event_class"].strip()
        if event_class not in EVENT_CLASSES:
            raise ValueError(
                f"{source}: {row['ticker']} {row['ex_date']} has class {event_class!r}; "
                f"one of {EVENT_CLASSES} is required"
            )
        for key in ("cik", "ex_date", "value", "evidence_url", "evidence_date", "evidence_quote"):
            if not row[key].strip():
                raise ValueError(f"{source}: {row['ticker']} {row['ex_date']} has no {key}")
        rows.append(
            CorporateActionEvent(
                cik=normalize_cik(row["cik"]) or row["cik"].strip(),
                ticker=row["ticker"].strip(),
                ex_date=row["ex_date"].strip(),
                value=float(row["value"]),
                event_class=event_class,
                evidence_form=row["evidence_form"].strip(),
                evidence_url=row["evidence_url"].strip(),
                evidence_date=row["evidence_date"].strip(),
                evidence_quote=row["evidence_quote"].strip(),
                rules=row["rules"].strip(),
            )
        )
    return tuple(rows)


def _records(path: Path) -> list[str]:
    """The file's data lines: the CSV header plus records, without the ``#`` comments."""
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


def event_index(
    events: tuple[CorporateActionEvent, ...] | None = None,
) -> dict[tuple[str, str], CorporateActionEvent]:
    """``{(CIK, ex_date): event}`` -- a CIK's several share classes share one event."""
    rows = events if events is not None else read_events()
    return {(event.cik, event.ex_date): event for event in rows}


def explained(
    events: tuple[CorporateActionEvent, ...] | None = None, *, cik: str, ex_date: str, value: float
) -> CorporateActionEvent | None:
    """The event that explains a recorded SPLIT row, or ``None`` when the list does not.

    The recorded factor must match the list's (``value`` is the vendor's own ratio), so a row
    the list has drifted away from is *unexplained* and refuses the cap rather than applying a
    stale guess."""
    event = event_index(events).get((normalize_cik(cik) or cik, ex_date))
    if event is None:
        return None
    if abs(event.value - value) > 1e-6 * max(1.0, abs(value)):
        return None
    return event
