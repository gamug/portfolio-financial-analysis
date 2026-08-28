"""SEC primary-document fetcher: index.json resolution + retry, all mocked."""

from __future__ import annotations

import httpx
import pytest

from fundamental_agent.filing_text import FilingTextError, fetch_primary_document

_INDEX = {
    "directory": {
        "item": [
            {"name": "0000320193-23-000106-index.htm"},
            {"name": "aapl-20230930.htm"},
            {"name": "R1.htm"},
            {"name": "exhibit99.txt"},
        ]
    }
}


def _client(handler: object) -> httpx.Client:
    return httpx.Client(
        base_url="https://www.sec.gov",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=True,
    )


def test_resolves_primary_htm_and_returns_source_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, json=_INDEX)
        return httpx.Response(200, text="<html><body>Item 1A. Risk Factors</body></html>")

    html, url = fetch_primary_document(
        "0000320193", "0000320193-23-000106", client=_client(handler)
    )
    assert "Risk Factors" in html
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"
    )
    # picked the shortest non-index .htm
    assert seen[-1].endswith("/aapl-20230930.htm")


def test_retries_on_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, text="slow down")
            return httpx.Response(200, json=_INDEX)
        return httpx.Response(200, text="<html>body</html>")

    html, _ = fetch_primary_document("320193", "0000320193-23-000106", client=_client(handler))
    assert "body" in html
    assert calls["n"] == 2


def test_missing_htm_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"directory": {"item": [{"name": "only.txt"}]}})

    with pytest.raises(FilingTextError, match="no primary"):
        fetch_primary_document("320193", "acc", client=_client(handler))
