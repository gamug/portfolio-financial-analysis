"""Roadmap step 7: candidate ``sharedExecutiveWith`` edges from news co-occurrence.

Reads the news repo's ``urls.db`` strictly read-only (only two indexed query
shapes, never a scan of ``articles`` / ``body_text``), pairs up assets whose
articles mention the same person, filters out pundits / politicians / NER noise,
and writes ``shared_executive_edge`` into ``KG_FINANCIAL_DB``.
"""

from __future__ import annotations

__all__ = ["run"]

from entity_resolution.pipeline import run
