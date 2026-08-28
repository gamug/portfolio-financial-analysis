"""Shared fixtures. The ``financials_*.json`` files are real EDGAR gateway captures."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from fundamental_agent import db
from fundamental_agent.statements import Statements

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES / name).read_text())
    return cast("dict[str, Any]", payload["data"])


@pytest.fixture
def aapl_10k() -> Statements:
    return Statements.from_payload(_load("financials_AAPL_10-K_2023.json"))


@pytest.fixture
def jpm_10k() -> Statements:
    return Statements.from_payload(_load("financials_JPM_10-K_2023.json"))


@pytest.fixture
def msft_10q() -> Statements:
    return Statements.from_payload(_load("financials_MSFT_10-Q_2024.json"))


@pytest.fixture
def nvda_10k() -> Statements:
    return Statements.from_payload(_load("financials_NVDA_10-K_2024.json"))


@pytest.fixture
def raw_aapl_payload() -> dict[str, Any]:
    return _load("financials_AAPL_10-K_2023.json")


@pytest.fixture
def constituents_html() -> str:
    return (FIXTURES / "sp500_constituents.html").read_text()


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn
