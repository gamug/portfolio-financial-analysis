"""``scripts/sec_xcheck/common.py``: the SEC cache client, period matching and the read-only database opener (T-147).

Hermetic: no network. ``SecClient`` is only ever used offline or against a monkeypatched fetcher."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sec_xcheck import common
from sec_xcheck.common import SecClient, cik10, cik_family, duration_matches, match_value

COMPANYFACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                            "val": 100.0,
                            "accn": "0000000001-24-000001",
                        },
                        {
                            "start": "2023-10-01",
                            "end": "2023-12-31",
                            "val": 30.0,
                            "accn": "0000000001-24-000001",
                        },
                    ]
                }
            },
            "Assets": {
                "units": {
                    "USD": [{"end": "2023-12-31", "val": 500.0, "accn": "0000000001-24-000001"}]
                }
            },
        }
    }
}


def test_match_value_is_exact_to_the_dollar_or_a_part_per_million() -> None:
    assert match_value(100.0, 100.0)
    assert match_value(1_000_000_000.4, 1_000_000_000.0)  # within 1 ppm
    assert not match_value(1_000_100_000.0, 1_000_000_000.0)
    assert not match_value(5.0, 6.0)  # beyond the 0.5 dollar floor


@pytest.mark.parametrize(
    ("form", "start", "end", "expected"),
    [
        ("10-K", "2023-01-01", "2023-12-31", True),  # 364 days: a fiscal year
        ("10-K", "2023-10-01", "2023-12-31", False),  # a quarter is not a year
        ("10-Q", "2023-10-01", "2023-12-31", True),  # 91 days: a quarter
        ("10-Q", "2023-01-01", "2023-12-31", False),
        ("10-K", None, "2023-12-31", False),  # an instant has no start
    ],
)
def test_duration_matches_the_form_length(
    form: str, start: str | None, end: str, expected: bool
) -> None:
    assert duration_matches(form, start, end) is expected


def test_index_by_accession_groups_facts_and_keeps_the_unit() -> None:
    index = common.index_by_accession(COMPANYFACTS)
    facts = index["0000000001-24-000001"]
    assert {c for _t, c, _f in facts} == {"Revenues", "Assets"}
    assert all(f["_unit"] == "USD" for _t, _c, f in facts)


def test_cik_helpers() -> None:
    assert cik10(320193) == "0000320193"
    assert cik_family("2115436") == (
        "0002115436",
        "0000034088",
    )  # XOM's holding company and its predecessor
    assert cik_family("320193") == ("0000320193",)
    assert common.is_accession("0000320193-24-000123")
    assert not common.is_accession("320193")


def test_offline_client_reads_the_cache_and_refuses_to_fetch(tmp_path: Path) -> None:
    path = tmp_path / "companyfacts" / "CIK0000000001.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(COMPANYFACTS))
    client = SecClient(tmp_path, offline=True)
    assert client.get("companyfacts", 1) == COMPANYFACTS
    with pytest.raises(FileNotFoundError, match="not cached"):
        client.get("companyfacts", 2)
    assert client.requests == 0  # nothing touched the network


def test_online_client_caches_every_response_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_fetch(self: SecClient, url: str, retries: int = 3) -> bytes:
        calls.append(url)
        return json.dumps({"name": "X"}).encode()

    monkeypatch.setattr(SecClient, "_fetch", fake_fetch)
    client = SecClient(tmp_path, offline=False)
    assert client.get("submissions", 320193) == {"name": "X"}
    assert client.get("submissions", 320193) == {"name": "X"}  # served from the cache
    assert calls == ["https://data.sec.gov/submissions/CIK0000320193.json"]


def test_fetch_is_throttled_to_ten_requests_per_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(common.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(common.time, "sleep", fake_sleep)
    client = SecClient(tmp_path)
    for _ in range(3):
        client._throttle()
    assert all(s >= 1.0 / common.MAX_REQUESTS_PER_SECOND - 1e-9 for s in sleeps[1:])


def test_user_agent_comes_from_the_environment_else_the_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert common.user_agent() == "research@example.com"
    monkeypatch.setenv("SEC_USER_AGENT", "Someone me@example.org")
    assert common.user_agent() == "Someone me@example.org"


def test_write_atomic_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    common.write_atomic(target, b"{}")
    assert target.read_bytes() == b"{}"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_open_ro_refuses_to_write(tmp_path: Path) -> None:
    path = tmp_path / "ro.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    conn = common.open_ro(str(path))
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (1)")
