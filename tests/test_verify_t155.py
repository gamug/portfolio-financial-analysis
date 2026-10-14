"""``scripts/verify_t155.py``: the succession classification's pure pieces and its reading table."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "verify_t155", Path(__file__).resolve().parent.parent / "scripts" / "verify_t155.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_t155"] = verify
_SPEC.loader.exec_module(verify)


def test_to_text_drops_markup_and_collapses_space() -> None:
    assert verify.to_text("<p>Hello&nbsp; <b>world</b></p>\n<p>2</p>") == "Hello world\n2"


def test_succession_sentence_prefers_the_sentence_that_names_the_predecessor() -> None:
    text = (
        "This Current Report establishes New X as the successor issuer to Old X pursuant to Rule 12g-3(a). "
        "Other filler text that is longer."
    )
    assert verify.succession_sentence(text, "Old X") == (
        "This Current Report establishes New X as the successor issuer to Old X pursuant to Rule 12g-3(a)."
    )


def test_succession_sentence_falls_back_to_a_rule_12g_3_sentence() -> None:
    text = "The new shares are deemed registered under Section 12(b) pursuant to Rule 12g-3(a)."
    assert verify.succession_sentence(text, "New X") == text


def test_succession_sentence_is_none_without_a_succession() -> None:
    assert verify.succession_sentence("A plain 8-K about a dividend.", "X") is None


def test_the_reading_covers_every_register_line() -> None:
    with verify.REGISTER.open(newline="", encoding="utf-8") as fh:
        register = list(csv.DictReader(fh))
    assert len(register) == 19
    for row in register:
        assert (row["ticker"], row["accession_number"]) in verify.READING


def test_the_amendment_uses_the_original_filings_evidence() -> None:
    assert verify.EVIDENCE_OF["0002041610-25-000029"] == "0001193125-25-175046"
