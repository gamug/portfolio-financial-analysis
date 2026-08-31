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
    # picked the non-index, non-fragment .htm body
    assert seen[-1].endswith("/aapl-20230930.htm")


def test_prefers_the_largest_non_exhibit_htm_over_directory_order() -> None:
    # SEC lists the directory alphabetically, so the exhibit sorts first; the real
    # 10-K body is larger and must win.
    index = {
        "directory": {
            "item": [
                {"name": "corp10k2023exhibit1019.htm", "size": "12000"},
                {"name": "ex-32.htm", "size": "3000"},
                {"name": "jpm-20231231.htm", "size": "1800000"},
                {"name": "R7.htm", "size": "999999"},
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, json=index)
        return httpx.Response(200, text="<html>Item 1A. Risk Factors ... Item 7. MD&A</html>")

    _, url = fetch_primary_document("19617", "0000019617-24-000123", client=_client(handler))
    assert url.endswith("/jpm-20231231.htm")


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


_INDEX_HTML = (
    "<html><body><table summary='Document Format Files'>"
    "<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>"
    "<tr><td>1</td><td>FORM 10-K</td>"
    "<td><a href='/ix?doc=/Archives/edgar/data/34088/000003408822000011/xom-20211231.htm'>"
    "xom-20211231.htm</a></td><td>10-K</td><td>6159522</td></tr>"
    "<tr><td>2</td><td>EX-21</td>"
    "<td><a href='/Archives/edgar/data/34088/000003408822000011/xomex21.htm'>xomex21.htm</a></td>"
    "<td>EX-21</td><td>999999999</td></tr>"
    "</table></body></html>"
)


def test_form_index_html_resolves_body_by_type_under_filer_cik() -> None:
    # Asset CIK (2115436) differs from the accession's filer CIK (34088); the archive
    # path and the -index.html both hang off the filer CIK, and the body is the row
    # whose Type is the form -- not the far larger exhibit.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("-index.html"):
            return httpx.Response(200, text=_INDEX_HTML)
        return httpx.Response(200, text="<html>Item 1A. Risk Factors ... Item 7. MD&A</html>")

    _, url = fetch_primary_document(
        "0002115436", "0000034088-22-000011", form="10-K", client=_client(handler)
    )
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/34088/000003408822000011/xom-20211231.htm"
    )
    assert not any("index.json" in path for path in seen)


def test_form_index_html_falls_back_to_index_json_when_no_body_row() -> None:
    only_exhibits = (
        "<html><body><table>"
        "<tr><td>1</td><td>d</td>"
        "<td><a href='/Archives/edgar/data/320193/000032019323000106/ex99.htm'>ex99.htm</a></td>"
        "<td>EX-99</td><td>1</td></tr>"
        "</table></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("-index.html"):
            return httpx.Response(200, text=only_exhibits)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, json=_INDEX)
        return httpx.Response(200, text="<html>body</html>")

    _, url = fetch_primary_document(
        "320193", "0000320193-23-000106", form="10-K", client=_client(handler)
    )
    assert url.endswith("/aapl-20230930.htm")


def test_empty_200_body_raises(mocker: object) -> None:
    mocker.patch("fundamental_agent.filing_text.time.sleep")  # type: ignore[attr-defined]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, json=_INDEX)
        return httpx.Response(200, content=b"")

    with pytest.raises(FilingTextError, match=r"empty body|after 3 attempts"):
        fetch_primary_document("320193", "0000320193-23-000106", client=_client(handler))
