"""Markowitz / Modern Portfolio Theory benchmark portfolio.

``quant`` builds a mean-variance-optimized portfolio from the total-return series
derived in :mod:`quant.returns`, over a score-independent liquidity/data universe
gate. Its output is the base-case benchmark the blended-score ``cycle`` book is
evaluated against.

This package is a leaf: it reads shared tables via SQL and imports only
``kg_schema``. Nothing in ``cycle`` / ``pricing_agent`` / ``fundamental_agent`` /
``entity_resolution`` imports it, so its numeric dependencies (numpy, scipy,
cvxpy) stay off their import path.
"""
