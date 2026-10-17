"""The corporate-action contract shared across packages (T-153 part 3d).

``corporate_action`` is one table with two writers: ``quant backfill-actions`` (the pricing
gateway's dividends and splits) and ``pricing_agent import-tiingo`` (the private Tiingo file's).
The engine-version literals live here so both writers and ``quant``'s reader agree without any
package importing another (``quant`` imports ``kg_schema`` only, SPEC FR-009).
"""

from __future__ import annotations

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
