"""Which recorded filing failures are about the filing itself, and which about the service (T-017).

A "data" failure is deterministic: the same filing fails the same way on every run, so
retrying it spends calls and (for the exit rule) fails the step for nothing. Everything else --
a gateway outage, a retry that ran out, an HTTP error, an LLM provider error, any exception
nobody listed -- is a "service" failure: it may clear on its own, so the filing stays pending
and the step stays failed when nothing completed.

"data" is an explicit allowlist, each entry with the line that raises or records it. An error
nobody listed is "service": a new failure is retried and seen, never silently excused.
"""

from __future__ import annotations

from typing import Literal

FailureClass = Literal["data", "service"]

# ``analysis_run_error.stage`` values that are data failures.
DATA_STAGES: frozenset[str] = frozenset(
    {
        "period",  # _record_unusable: no period of its own (pipeline.py:579-589)
        "identity",  # FilingIdentityError: another company's filing (pipeline.py:360, :476-489)
    }
)

# ``analysis_run_error.message`` substrings that are data failures (stage ``process``).
DATA_MESSAGE_MARKERS: tuple[str, ...] = (
    # The gateway's own extraction failure, inside EdgarError's "unsuccessful payload" message
    # (edgar_client.py:205). Not the whole payload message: "No '10-K'/'10-Q' filing found" is
    # also an unsuccessful payload, but a filing that may appear later, so it stays "service".
    "Failed to extract financials",
    "reconciliation failed",  # EdgarError, statements failed the gateway's reconciliation (:193)
    "refusing to overwrite",  # FilingLabelCollision (db.py:396-397)
    "refusing to replace",  # FilingReplacementRefused (db.py:425)
)

# What a transport failure and an HTTP status error say; documented, not listed as data:
#   "failed after N attempts"  (edgar_client.py:128), httpx.HTTPStatusError, LLM provider errors.


def classify(stage: str | None, message: str | None) -> FailureClass:
    """``"data"`` when the failure is on the allowlist, else ``"service"``."""
    if stage in DATA_STAGES:
        return "data"
    text = message or ""
    if any(marker in text for marker in DATA_MESSAGE_MARKERS):
        return "data"
    return "service"
