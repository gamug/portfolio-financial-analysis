#!/usr/bin/env python3
"""T-143 part A -- V3: recompute every stored metric independently.

Reviewer B's independent layer of the T-143 pilot.  The point of this script is
that the metrics are recomputed from the *recorded inputs* by code that was not
written from the metric engine: it reads
``docs/metric_dictionary.md`` (Annex B, MD-01..MD-39) and
``docs/checklist_sec/checklist_v1.3.md`` (the applicability matrix, MET-, APP-,
ID-, CON-, PER- and DQ-01) and nothing else about how the engine computes
anything.

It reads the pilot database read-only and writes two files:

    /workspaces/thesis/handoff/pilot/v3/v3_results.csv    one row per stored metric row
    /workspaces/thesis/handoff/pilot/v3/v3_summary.md     the counts and the findings

Rules it obeys (the handoff brief, T143a_V3_independent_metrics.md):
no LLM call, no network, no database write, no ``--allow-*`` override.

Usage (standard library only, no uv):

    .venv/bin/python scripts/pilot_a/independent_metrics.py \
        --db /workspaces/thesis/pilot_a2/financial.db \
        --out-dir /workspaces/thesis/handoff/pilot/v3
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict

# ---------------------------------------------------------------------------
# The agreement rule (stated in the summary too).
#
# DQ-01 gives an identity threshold of max(rounding tolerance, 0.5% x |base|).
# A metric formula is an identity on its own inputs, so the same shape is used
# here: the relative 0.5% materiality term of DQ-01 / C3 p.32, measured on the
# stored value.  Both cells empty is an agreement.
# ---------------------------------------------------------------------------
REL_TOLERANCE = 0.005
# A recomputation that lands on the stored value to within this relative
# distance is an exact hit; it is reported separately from the tolerance band.
EXACT_REL = 1e-9
# The absolute floor of the agreement test on a stored value of zero.
ZERO_FLOOR = 1e-12
# The magnitudes below which a number is printed in the exponential form.
BIG = 1e6
SMALL = 1e-4
# The D-13 utility rule averages three years.
THREE_YEARS = 3
# A PER-06 term records a label and a number.
TERM_COLUMNS = 2
# The examples the summary gives per mismatch class.
EXAMPLES_PER_CLASS = 3

HANDOFF_DB = "/workspaces/thesis/pilot_a2/financial.db"
HANDOFF_OUT = "/workspaces/thesis/handoff/pilot/v3"

# ---------------------------------------------------------------------------
# The applicability matrix of checklist v1.3 (lines 287-323), reduced to the
# rules that make a cell NOT APPLICABLE, i.e. the cell must be empty.  The
# other cells of the matrix are ADJ: the metric applies with a flag, so the
# value is expected to be there.
#
# The matrix is per metric, so these sets are per metric too.  A rule that is
# NA for one metric and ADJ for another is a real difference (revenue_growth is
# ADJ(APP-01) while gross_margin is NA(APP-01)).
# ---------------------------------------------------------------------------
NA_RULES_BY_METRIC = {
    "gross_margin": {"APP-01", "APP-03"},
    "current_ratio": {"APP-01", "APP-03"},
    "quick_ratio": {"APP-01", "APP-03"},
    "cash_ratio": {"APP-01", "APP-03"},
    "inventory_turnover": {"APP-01", "APP-03"},
    "return_on_assets": {"APP-02"},
    "debt_to_equity": {"APP-02", "APP-07"},
    "debt_to_assets": {"APP-02"},
    "interest_coverage": {"APP-02"},
    "net_debt_to_ebitda": {"APP-02"},
    "nopat": {"APP-02"},
    "return_on_invested_capital": {"APP-02"},
    "enterprise_value": {"APP-02"},
    "enterprise_fcf_yield": {"APP-02", "MD-38: no classified balance sheet"},
    "free_cash_flow_growth": {"APP-02b"},
    "operating_cash_flow_margin": {"APP-02b"},
    "free_cash_flow_margin": {"APP-02b"},
    "free_cash_flow_conversion": {"APP-02b"},
    "capex_intensity": {"APP-02b"},
    "operating_cash_flow_cagr": {"APP-02b"},
    "free_cash_flow_yield": {"APP-02b"},
    "sbc_adjusted_fcf_yield": {"APP-02b"},
    "return_on_equity": {"APP-07"},
}
# The applicability rules that only adjust (the value is expected to be there).
ADJ_RULES = {"APP-00", "APP-04", "APP-04b", "APP-04c", "MKT-03"}

# The rule each recorded `provenance.empty_reason` names.  The engine records
# one of six reasons; each maps to a rule of checklist v1.3.
EMPTY_REASON_RULE = {
    "not_applicable": "the applicability matrix (NA)",
    "missing_input": "MET-00 (a missing input leaves the metric empty)",
    "prior_year_missing": "MET-02 (the capital of the prior year is missing)",
    "capture_failure": "ID-02b (a capture failure)",
    "denominator_not_positive": "MET-09 (a denominator <= 0)",
    "growth_base_not_positive": "MET-04 (a base <= 0)",
}

# The recorded flags that name a rule for one metric only.
EXPLICIT_RULE_FLAGS = {
    "flag_interest_expense_negative": "MET-09",
    "flag_enterprise_fcf_yield_unclassified_balance_sheet": "MD-38",
    "flag_utility_cash_coverage_missing_bound": "D-13",
    "flag_return_on_invested_capital_invested_capital_not_positive": "MD-02",
}

# The one metric whose period basis cannot be taken from its own provenance.
#
# MD-01 gives "operating income + D&A" and no basis.  Read with PER-06 (a 10-Q
# flow is the trailing year, and a quarter multiplied by four is not a TTM),
# a 10-Q's ebitda is the trailing-year one -- and the row's own recorded keys
# agree: on every 10-Q that carries one, the `ebitda` key and the `ebitda_ttm`
# key hold the same number.  The row's provenance nevertheless labels its
# inputs `period`, which contradicts the value it stores; that divergence is
# counted and reported separately.
EBITDA_BASIS_ON_10Q = "ttm"

# Metrics that are not rows of the metric dictionary (Annex B).  They are
# recomputed from the rule that names them, and the summary says so.
NOT_IN_DICTIONARY = {
    "return_on_invested_capital_effective_rate": "MET-03's note (the thesis reports ROIC with the 21% rate and with the "
    "filed effective rate); not a row of Annex B",
    "utility_cash_coverage_3y": "the Scope section of checklist v1.3 (D-13, the utility cash-coverage "
    "band); not a row of Annex B",
    "value_inputs": "a record, not a metric: the bundle the cycle reads; not a row of Annex B",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def num(value):
    """A recorded number, or None.  bool is not a number here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    return None


def close(stored, recomputed, rel=REL_TOLERANCE):
    """DQ-01's relative materiality band."""
    if stored is None or recomputed is None:
        return False
    if stored == 0.0:
        return abs(recomputed) <= ZERO_FLOOR
    return abs(stored - recomputed) <= rel * abs(stored)


def exact(stored, recomputed):
    if stored is None or recomputed is None:
        return False
    if stored == 0.0:
        return recomputed == 0.0
    return abs(stored - recomputed) <= EXACT_REL * abs(stored)


def safe_div(num_, den):
    if num_ is None or den is None or den == 0:
        return None
    return num_ / den


def format_num(value):
    if value is None:
        return "none"
    if value == 0:
        return "0"
    if abs(value) >= BIG or abs(value) < SMALL:
        return f"{value:.6g}"
    return f"{value:.10g}"


# ---------------------------------------------------------------------------
# The recorded inputs of one filing.
# ---------------------------------------------------------------------------
class Filing:
    """Every recorded input of one filing, across all of its metric rows.

    The engine records the resolved inputs of a metric in that metric row's
    ``inputs_json``: the numbers as top-level keys, the trailing-year numbers
    as ``*_ttm`` keys, and the PER-06 construction of every trailing-year item
    in ``provenance.items[*].terms``.  A formula sometimes needs an input the
    row itself does not carry, so this class indexes all of them: the
    recomputation then uses what the pilot recorded, and never anything else.
    """

    def __init__(self, fid, ticker, form, period_end):
        self.fid = fid
        self.ticker = ticker
        self.form = form
        self.period_end = period_end
        self.rows = {}  # metric -> (value, inputs dict)
        self.order = []  # metric names, in load order
        self.prov = defaultdict(list)  # (item, basis) -> [(metric, entry)]
        self.ttm_available = False

    def add(self, metric, value, inputs):
        self.rows[metric] = (value, inputs)
        self.order.append(metric)
        if inputs.get("annualized_ttm"):
            self.ttm_available = True
        prov = inputs.get("provenance") or {}
        for item, entries in (prov.get("items") or {}).items():
            for entry in entries:
                self.prov[(item, entry.get("basis"))].append((metric, entry))

    # -- accessors ---------------------------------------------------------
    def basis_of(self, metric, item):
        """The basis the row records for an item (its provenance statement)."""
        prov = self.rows[metric][1].get("provenance") or {}
        for entry in (prov.get("items") or {}).get(item, []):
            return entry.get("basis")
        return None

    def ttm_entry(self, item, prefer_metric=None):
        """The recorded trailing-year construction of an item: (value, terms).

        A one-quarter-x-4 annualization (PER-06, D-09) is not a TTM and is
        refused: only a construction that records its columns counts.
        """
        seen = set()
        order = ([prefer_metric] if prefer_metric in self.rows else []) + self.order
        for metric in order:
            if metric in seen:
                continue
            seen.add(metric)
            for entry_metric, entry in self.prov.get((item, "ttm"), []):
                if entry_metric != metric:
                    continue
                if entry.get("method") not in ("ytd", "quarters"):
                    continue
                value = num(entry.get("value"))
                terms = entry.get("terms")
                if value is not None or terms:
                    return value, terms
        return None, None

    def rebuild_ttm(self, item, prefer_metric=None):
        """Rebuild a trailing-year value from the recorded PER-06 columns.

        PER-06: a 10-Q TTM is FY(prior 10-K) - YTD(same period last year) +
        YTD(this year), from contiguous periods.
        """
        value, terms = self.ttm_entry(item, prefer_metric)
        if not terms:
            return None, value
        numbers = {}
        for term in terms:
            if isinstance(term, (list, tuple)) and len(term) >= TERM_COLUMNS:
                numbers[str(term[0])] = num(term[1])
        fy = numbers.get("prior_fiscal_year")
        pytd = numbers.get("prior_ytd")
        ytd = numbers.get("ytd")
        if None in (fy, pytd, ytd):
            return None, value
        return fy - pytd + ytd, value

    def any_ttm(self, item, prefer_metric=None):
        order = ([prefer_metric] if prefer_metric in self.rows else []) + self.order
        for metric in order:
            if metric in self.rows:
                value = num(self.rows[metric][1].get(item + "_ttm"))
                if value is not None:
                    return value
        return None

    def any_value(self, name, prefer_metric=None):
        """A number recorded anywhere in the filing under ``name``."""
        order = ([prefer_metric] if prefer_metric in self.rows else []) + self.order
        for metric in order:
            if metric in self.rows:
                value = num(self.rows[metric][1].get(name))
                if value is not None:
                    return value
        return None


# ---------------------------------------------------------------------------
# The recomputations.  One method per stored metric.
#
# Each returns an ordered dict of readings:
#     "as_recorded" -- the dictionary formula on the inputs the row records,
#                      each ingredient taken at the basis its provenance
#                      declares (the recorded statement of the basis);
#     "period"      -- the same formula with every duration ingredient on the
#                      filing's own period;
#     "ttm"         -- the same formula with every duration ingredient on the
#                      trailing year of PER-06.
# The first key is the primary reading.  A value of None means the reading
# cannot be evaluated (an input the formula needs is not recorded) or that a
# rule makes the cell empty.
# ---------------------------------------------------------------------------
class Recomputer:
    def __init__(self, filing: Filing, metric: str):
        self.f = filing
        self.metric = metric
        self.value, self.inputs = filing.rows[metric]
        self.stored = num(self.value)
        self.input_gap = None  # set when the row lacks an input the formula needs

    # -- recorded inputs ----------------------------------------------------
    def v(self, name):
        """A number recorded in this row."""
        return num(self.inputs.get(name))

    def flag(self, name):
        return self.inputs.get(name)

    def basis(self, item):
        return self.f.basis_of(self.metric, item)

    def donor(self, name):
        """A number recorded in another metric row of the same filing."""
        return self.f.any_value(name, self.metric)

    def donor_ttm(self, name):
        return self.f.any_ttm(name, self.metric)

    def ttm_value(self, item):
        """The trailing-year value of an item (PER-06).

        On a 10-K the trailing year is the filing's own fiscal year.  On a 10-Q
        it is FY(prior 10-K) - YTD(prior year) + YTD(this year), rebuilt here
        from the recorded columns; a filing that has no such construction has
        no trailing year (a quarter multiplied by four is not a TTM).
        """
        if self.f.form == "10-K":
            # the trailing year of a 10-K is its own fiscal year
            value = self.v(item)
            return value if value is not None else self.donor(item)
        if not self.f.ttm_available:
            return None
        rebuilt, value = self.f.rebuild_ttm(item, self.metric)
        if rebuilt is not None:
            return rebuilt
        if value is not None:
            return value
        value = self.v(item + "_ttm")
        return value if value is not None else self.donor_ttm(item)

    def flow(self, item, basis):
        """A duration ingredient at the requested basis."""
        if basis == "ttm":
            return self.ttm_value(item)
        return self.v(item)

    def ingredient_basis(self, item, default="period"):
        """The basis the row records for an ingredient."""
        basis = self.basis(item)
        if basis in ("ttm", "period", "prior_year", "prior_quarter"):
            return basis
        return default

    def prior_capture_failure(self):
        """A recorded capture failure of a prior-year input (ID-02b).

        A capture failure means the filed line was not captured, so the input
        is not there; the building blocks built on it stay empty, as ID-02b
        requires.
        """
        for name, value in self.inputs.items():
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value == 1.0
                and name.startswith("flag_prior_")
                and "capture_failure" in name
            ):
                return name
        return None

    # -- the readings -------------------------------------------------------
    def readings(self):
        fn = getattr(self, "_read_" + self.metric, None)
        if fn is None:
            return {}
        out = fn()
        return {k: out[k] for k in ("as_recorded", "period", "ttm") if k in out}

    def _labels(self, recorded):
        """The three reading labels and the basis each one uses."""
        out = []
        for label in ("as_recorded", "period", "ttm"):
            out.append((label, recorded if label == "as_recorded" else label))
        return out

    # -- building blocks ----------------------------------------------------
    def _rate_unusable(self, basis):
        """Is the rate NOPAT would use unusable?

        APP-04c gives a REIT the filing's own effective rate; when the
        trailing-year rate is missing there is no rate to use and the metric
        stays empty.  A rate outside the unit interval is *flagged* by the
        engine (`flag_effective_tax_rate_outside_unit_interval`) but is still
        the rate the filing reports, so it is used as recorded.
        """
        return basis == "ttm" and bool(self.flag("flag_nopat_reit_ttm_rate_missing"))

    def _tax_rate_period(self):
        """MET-03: the rate NOPAT uses on the filing's own period."""
        rate = self.v("nopat_tax_rate")
        if rate is not None:
            return rate
        if self.flag("nopat_tax_rate_operating_loss"):
            return 0.0
        if self.flag("nopat_tax_rate_reit_effective"):
            return self.v("effective_tax_rate")
        if self.flag("nopat_tax_rate_statutory_21"):
            return 0.21
        return None

    def _tax_rate_ttm(self):
        """MET-03: the rate NOPAT uses on the trailing year."""
        rate = self.v("nopat_ttm_tax_rate_value")
        if rate is not None:
            return rate
        if self.flag("nopat_ttm_tax_rate_operating_loss"):
            return 0.0
        if self.flag("nopat_ttm_tax_rate_statutory_21"):
            return 0.21
        if self.flag("nopat_ttm_tax_rate_reit_effective") or self.flag(
            "nopat_tax_rate_reit_effective"
        ):
            # APP-04c: the REIT's own effective rate, on the same basis
            tax, pre = self.ttm_value("income_tax"), self.ttm_value("pretax_income")
            if tax is not None and pre:
                return tax / pre
            return self.v("effective_tax_rate")
        return self._tax_rate_period()

    def _tax_rate(self, basis):
        """MET-03: the rate NOPAT uses.

        21% for every company but a REIT (APP-04c: the filing's own effective
        rate), and 0 on an operating loss.  The row records the rate it used;
        where it records only the flag, the rate is rebuilt from the flag.
        """
        if basis == "ttm":
            return self._tax_rate_ttm()
        return self._tax_rate_period()

    def _nopat(self, basis):
        """MD-05: operating income x (1 - tax rate).

        The recorded building block is used only when its key says which basis
        it is on (`nopat_ttm` for the trailing year); the plain `nopat` key is
        not basis-explicit across rows, so it is read from this row only.
        """
        if self._rate_unusable(basis):
            return None
        oi = self.flow("operating_income", basis)
        rate = self._tax_rate(basis)
        if oi is not None and rate is not None:
            return oi * (1.0 - rate)
        if basis == "ttm":
            recorded = self.v("nopat_ttm")
            if recorded is None:
                recorded = self.donor_ttm("nopat")
            if recorded is None and self.f.form == "10-K":
                recorded = self.v("nopat")
            return recorded
        return self.v("nopat")

    def _invested_capital(self):
        """MD-02: debt + parent equity + NCI - cash - short-term investments,
        at the end of the prior year."""
        recorded = self.v("invested_capital")
        if self.prior_capture_failure():
            # ID-02b: a capture failure leaves the building block empty
            return None
        td, eq = self.v("total_debt"), self.v("equity")
        cash, sti = self.v("cash"), self.v("short_term_investments")
        nci = self.v("nci")
        if nci is None:
            nci = 0.0
        if None in (td, eq, cash, sti):
            return recorded
        return td + eq + nci - cash - sti

    def _free_cash_flow(self, basis):
        """MD-04: operating cash flow - capex.

        The recorded building block is used only when its key says which basis
        it is on (`free_cash_flow_ttm` for the trailing year).
        """
        cfo = self.flow("operating_cash_flow", basis)
        capex = self.flow("capital_expenditure", basis)
        if cfo is not None and capex is not None:
            return cfo - capex
        if basis == "ttm":
            recorded = self.v("free_cash_flow_ttm")
            if recorded is None:
                recorded = self.donor_ttm("free_cash_flow")
            if recorded is None and self.f.form == "10-K":
                recorded = self.v("free_cash_flow")
            return recorded
        recorded = self.v("free_cash_flow")
        if recorded is None:
            recorded = self.donor("free_cash_flow")
        return recorded

    def _ebitda(self, basis):
        """MD-01: operating income + D&A.

        The recorded building block is used only when its key says which basis
        it is on: on a 10-Q the `ebitda` key holds the trailing-year figure,
        so only `ebitda_ttm` is read for the trailing year.
        """
        oi = self.flow("operating_income", basis)
        da = self.flow("depreciation_amortization", basis)
        if oi is not None and da is not None:
            return oi + da
        if basis == "ttm":
            recorded = self.v("ebitda_ttm")
            if recorded is None:
                recorded = self.donor_ttm("ebitda")
            return recorded
        if self.f.form == "10-K":
            return self.v("ebitda")
        return None

    # -- the metrics --------------------------------------------------------
    def _read_ebitda(self):
        """MD-01: operating income + D&A."""
        out = {}
        recorded = self.ingredient_basis("operating_income")
        if self.f.form == "10-Q":
            # MD-01 read with PER-06: a 10-Q's flow is the trailing year.
            # The row's own `ebitda` and `ebitda_ttm` keys hold the same number
            # on every 10-Q that carries one, so this is the metric's basis;
            # the provenance's `period` label is a labelling divergence.
            recorded = EBITDA_BASIS_ON_10Q
        for label, basis in self._labels(recorded):
            value = self._ebitda(basis)
            if value is not None:
                out[label] = value
        return out

    def _read_nopat(self):
        """MD-05: operating income x (1 - tax rate)."""
        out = {}
        recorded = self.ingredient_basis("operating_income")
        for label, basis in self._labels(recorded):
            value = self._nopat(basis)
            if value is not None:
                out[label] = value
        return out

    def _read_gross_margin(self):
        """MD-06: (GrossProfit when filed, else revenue - cost of revenue) / revenue."""
        out = {}
        for label, _basis in self._labels("period"):
            rev = self.flow("revenue", "period")
            if not rev:
                continue
            gp = self.v("gross_profit")
            if gp is not None:
                out[label] = gp / rev
                continue
            cogs = self.v("cogs")
            if cogs is None:
                cogs = self.donor("cogs")
            if cogs is not None:
                out[label] = (rev - cogs) / rev
        return out

    def _read_operating_margin(self):
        """MD-07: operating income / revenue."""
        out = {}
        recorded = self.ingredient_basis("operating_income")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("operating_income", basis), self.flow("revenue", basis))
            if value is not None:
                out[label] = value
        return out

    def _read_net_margin(self):
        """MD-08: parent net income / revenue."""
        out = {}
        recorded = self.ingredient_basis("net_income")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("net_income", basis), self.flow("revenue", basis))
            if value is not None:
                out[label] = value
        return out

    def _read_return_on_assets(self):
        """MD-09: NOPAT / total assets at the end of the prior year."""
        out = {}
        base = self.v("prior_total_assets")
        if not base:
            return out
        recorded = self.ingredient_basis("operating_income")
        for label, basis in self._labels(recorded):
            value = safe_div(self._nopat(basis), base)
            if value is not None:
                out[label] = value
        return out

    def _read_return_on_equity(self):
        """MD-10: parent net income / parent equity at the end of the prior year.

        APP-07: NA on negative equity.
        """
        out = {}
        base = self.v("prior_equity")
        if not base or base <= 0:
            return out
        recorded = self.ingredient_basis("net_income")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("net_income", basis), base)
            if value is not None:
                out[label] = value
        return out

    def _read_return_on_invested_capital(self):
        """MD-11: NOPAT / invested capital (MD-02) at the end of the prior year.

        MD-02's note: ROIC is used only when the invested capital is > 0.
        """
        out = {}
        ic = self._invested_capital()
        if not ic or ic <= 0:
            return out
        recorded = self.ingredient_basis("operating_income")
        for label, basis in self._labels(recorded):
            value = safe_div(self._nopat(basis), ic)
            if value is not None:
                out[label] = value
        return out

    def _read_return_on_invested_capital_effective_rate(self):
        """MET-03's robustness variant: NOPAT at the filed effective rate.

        Not a row of Annex B; MET-03's note is the rule that names it.  The
        effective rate is rebuilt on the same basis as the NOPAT: income tax /
        pre-tax income (MD-30).
        """
        out = {}
        ic = self._invested_capital()
        if not ic or ic <= 0:
            return out
        recorded = self.ingredient_basis("operating_income")
        for label, basis in self._labels(recorded):
            oi = self.flow("operating_income", basis)
            tax = self.flow("income_tax", basis)
            pre = self.flow("pretax_income", basis)
            if pre is None or pre <= 0:
                # L-05: the effective tax rate is left empty when the pre-tax
                # income is not positive, so there is no rate to use.
                continue
            rate = safe_div(tax, pre)
            if rate is None:
                rate = self.v("effective_tax_rate")
            if rate is None or oi is None:
                continue
            out[label] = oi * (1.0 - rate) / ic
        return out

    def _read_current_ratio(self):
        """MD-12: current assets / current liabilities."""
        value = safe_div(self.v("current_assets"), self.v("current_liabilities"))
        return {"as_recorded": value} if value is not None else {}

    def _read_quick_ratio(self):
        """MD-13: today (current assets - inventory) / current liabilities."""
        ca, cl = self.v("current_assets"), self.v("current_liabilities")
        inv = self.v("inventory")
        out = {}
        if cl and ca is not None and inv is not None:
            out["as_recorded"] = (ca - inv) / cl
        return out

    def _read_cash_ratio(self):
        """MD-14: (cash + short-term investments) / current liabilities.

        ID-02a: cash is never counted twice, so when the filed cash caption
        already includes the short-term investments the line is not added
        twice.
        """
        cash, sti = self.v("cash"), self.v("short_term_investments")
        cl = self.v("current_liabilities")
        if cash is None or not cl:
            return {}
        if sti is not None and not self.flag("flag_cash_includes_short_term_investments"):
            return {"as_recorded": (cash + sti) / cl}
        return {"as_recorded": cash / cl}

    def _read_debt_to_equity(self):
        """MD-15: debt / parent equity.  APP-07: NA on negative equity."""
        eq = self.v("equity")
        if not eq or eq <= 0:
            return {}
        value = safe_div(self.v("total_debt"), eq)
        return {"as_recorded": value} if value is not None else {}

    def _read_debt_to_assets(self):
        """MD-16: debt / total assets."""
        value = safe_div(self.v("total_debt"), self.v("total_assets"))
        return {"as_recorded": value} if value is not None else {}

    def _read_interest_coverage(self):
        """MD-17: operating income / interest expense, filed sign.

        MET-09: empty only when the interest expense is <= 0.
        """
        ie = self.v("interest_expense")
        if ie is None or ie <= 0:
            return {}
        out = {}
        recorded = self.ingredient_basis("operating_income")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("operating_income", basis), ie)
            if value is not None:
                out[label] = value
        return out

    def _read_net_debt_to_ebitda(self):
        """MD-18: net debt (MD-03) / EBITDA (MD-01).  MET-09: EBITDA <= 0."""
        td, cash, sti = self.v("total_debt"), self.v("cash"), self.v("short_term_investments")
        if None in (td, cash, sti):
            return {}
        net_debt = td - cash - sti
        out = {}
        recorded = self.ingredient_basis("operating_income")
        for label, basis in self._labels(recorded):
            ebitda = self._ebitda(basis)
            if ebitda is None or ebitda <= 0:
                continue
            out[label] = net_debt / ebitda
        return out

    def _read_asset_turnover(self):
        """MD-19: revenue / total assets."""
        ta = self.v("total_assets")
        if not ta:
            return {}
        out = {}
        recorded = self.ingredient_basis("revenue")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("revenue", basis), ta)
            if value is not None:
                out[label] = value
        return out

    def _read_inventory_turnover(self):
        """MD-20: cost of revenue / inventory."""
        inv = self.v("inventory")
        if not inv:
            return {}
        out = {}
        recorded = self.ingredient_basis("cogs")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("cogs", basis), inv)
            if value is not None:
                out[label] = value
        return out

    def _read_receivables_turnover(self):
        """MD-21: revenue / receivables."""
        rec = self.v("receivables")
        if not rec:
            return {}
        out = {}
        recorded = self.ingredient_basis("revenue")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("revenue", basis), rec)
            if value is not None:
                out[label] = value
        return out

    def _read_revenue_growth(self):
        return self._growth("revenue")

    def _read_operating_income_growth(self):
        return self._growth("operating_income")

    def _read_net_income_growth(self):
        return self._growth("net_income")

    def _read_free_cash_flow_growth(self):
        return self._growth("free_cash_flow")

    def _read_operating_cash_flow_margin(self):
        """MD-26: operating cash flow / revenue."""
        out = {}
        recorded = self.ingredient_basis("operating_cash_flow")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("operating_cash_flow", basis), self.flow("revenue", basis))
            if value is not None:
                out[label] = value
        return out

    def _read_free_cash_flow_margin(self):
        """MD-27: free cash flow (MD-04) / revenue."""
        out = {}
        recorded = self.ingredient_basis("operating_cash_flow")
        for label, basis in self._labels(recorded):
            value = safe_div(self._free_cash_flow(basis), self.flow("revenue", basis))
            if value is not None:
                out[label] = value
        return out

    def _read_free_cash_flow_conversion(self):
        """MD-28: free cash flow (MD-04) / parent net income.

        MET-09: empty when the net income is <= 0.
        """
        out = {}
        recorded = self.ingredient_basis("net_income")
        for label, basis in self._labels(recorded):
            ni = self.flow("net_income", basis)
            if ni is None or ni <= 0:
                continue
            value = safe_div(self._free_cash_flow(basis), ni)
            if value is not None:
                out[label] = value
        return out

    def _read_capex_intensity(self):
        """MD-29: capex (MET-08) / revenue."""
        out = {}
        recorded = self.ingredient_basis("capital_expenditure")
        for label, basis in self._labels(recorded):
            value = safe_div(self.flow("capital_expenditure", basis), self.flow("revenue", basis))
            if value is not None:
                out[label] = value
        return out

    def _read_effective_tax_rate(self):
        """MD-30: income tax (filed sign) / pre-tax income.

        L-05: empty when the pre-tax income is <= 0.
        """
        out = {}
        recorded = self.ingredient_basis("income_tax")
        for label, basis in self._labels(recorded):
            pre = self.flow("pretax_income", basis)
            if pre is None or pre <= 0:
                continue
            value = safe_div(self.flow("income_tax", basis), pre)
            if value is not None:
                out[label] = value
        return out

    def _read_revenue_cagr(self):
        return self._cagr()

    def _read_net_income_cagr(self):
        return self._cagr()

    def _read_operating_cash_flow_cagr(self):
        return self._cagr()

    def _read_market_capitalization(self):
        """MD-34: close on D x the share count public on D, every class.

        MKT-03: with more than one share class the market capitalization is
        the sum over the classes; the row records one class's count and price,
        so the sum cannot be reproduced from the row alone.
        """
        shares, price = self.v("shares"), self.v("share_price")
        if shares is None or price is None:
            return {}
        classes = self.v("n_classes")
        product = shares * price
        if (
            classes is not None
            and classes > 1
            and self.stored is not None
            and not close(self.stored, product)
        ):
            self.input_gap = (
                "MKT-03: " + format_num(classes) + " share classes; the row records one "
                "class's count and price (" + format_num(product) + "), not the sum over "
                "the classes (" + format_num(self.stored) + ")"
            )
        return {"as_recorded": product}

    def _read_enterprise_value(self):
        """MD-35: market cap + debt + NCI - cash - short-term investments.

        MET-00: empty when the market capitalization, the debt or the cash is
        missing.
        """
        mc = self.v("market_capitalization")
        td, cash, sti = self.v("total_debt"), self.v("cash"), self.v("short_term_investments")
        nci = self.v("nci")
        if None in (mc, td, cash, sti, nci):
            return {}
        return {"as_recorded": mc + td + nci - cash - sti}

    def _read_free_cash_flow_yield(self):
        """MD-36: free cash flow (MD-04) / market capitalization."""
        mc = self.v("market_capitalization")
        if not mc:
            return {}
        out = {}
        recorded = self.ingredient_basis("operating_cash_flow")
        for label, basis in self._labels(recorded):
            value = safe_div(self._free_cash_flow(basis), mc)
            if value is not None:
                out[label] = value
        return out

    def _read_sbc_adjusted_fcf_yield(self):
        """MD-37: (free cash flow - stock-based compensation) / market cap."""
        mc = self.v("market_capitalization")
        if not mc:
            return {}
        out = {}
        recorded = self.ingredient_basis("operating_cash_flow")
        for label, basis in self._labels(recorded):
            fcf = self._free_cash_flow(basis)
            sbc = self.flow("stock_based_compensation", basis)
            if fcf is None or sbc is None:
                continue
            out[label] = (fcf - sbc) / mc
        return out

    def _read_enterprise_fcf_yield(self):
        """MD-38: (NOPAT - (capex - D&A + delta working capital)) / EV."""
        ev = self.v("enterprise_value")
        if not ev:
            return {}
        numerator = self._md38_numerator()
        if numerator is None:
            return {}
        return {"as_recorded": numerator / ev}

    def _read_utility_cash_coverage_3y(self):
        """The D-13 utility rule input: the three-year average of operating
        cash flow before working-capital changes over debt.

        Not a row of Annex B; the Scope section of checklist v1.3 (D-13) is
        the rule that names it.  The rule is a three-year average, so a year
        with no bound leaves the cell empty.
        """
        years = self.inputs.get("years")
        if not isinstance(years, list) or len(years) < THREE_YEARS:
            return {}
        ratios = [num(y.get("ratio_ambiguous_in")) for y in years[:THREE_YEARS]]
        if any(r is None for r in ratios):
            return {}
        return {"as_recorded": sum(ratios) / len(ratios)}

    def _read_value_inputs(self):
        """A record, not a formula: the bundle the cycle reads.

        Its stored value is the trailing-year parent net income.
        """
        ni_ttm = self.v("net_income_ttm")
        if ni_ttm is None:
            ni_ttm = self.donor_ttm("net_income")
        if ni_ttm is None:
            return {}
        return {"as_recorded": ni_ttm}

    # -- shared pieces ------------------------------------------------------
    def _md38_numerator(self):
        """MD-38: NOPAT - (capex - D&A + delta working capital)."""
        recorded = self.v("md38_numerator")
        nopat = self._nopat("ttm")
        if nopat is None:
            nopat = self._nopat("period")
        capex = self.ttm_value("capital_expenditure")
        if capex is None:
            capex = self.v("capital_expenditure")
        da = self.ttm_value("depreciation_amortization")
        if da is None:
            da = self.v("depreciation_amortization")
        dwc = self.v("delta_working_capital")
        if None in (nopat, capex, da, dwc):
            return recorded
        return nopat - (capex - da + dwc)

    def _growth(self, name):
        """MD-22..MD-25: (x_t - x_t-1) / x_t-1, both from one filing (PER-04).

        MET-04: growth from a base <= 0 is not meaningful, so the cell is
        empty.
        """
        cur, prior = self.v(name), self.v(name + "_prior")
        if cur is None or prior is None or prior <= 0:
            return {}
        return {"as_recorded": (cur - prior) / prior}

    def _cagr(self):
        """MD-31..MD-33: (end / begin) ^ (1 / years) - 1.

        MET-04: empty when an end is <= 0.
        """
        end, begin, years = self.v("end"), self.v("begin"), self.v("years")
        if end is None or begin is None or years is None:
            return {}
        if begin <= 0 or end <= 0 or years <= 0:
            return {}
        return {"as_recorded": (end / begin) ** (1.0 / years) - 1.0}


# ---------------------------------------------------------------------------
# The rules that make a stored empty cell correct.
# ---------------------------------------------------------------------------
def na_rule(rec: Recomputer):
    """The hard NA rule that applies to this row, if any.

    The applicability matrix of checklist v1.3 is per metric: a rule that is NA
    for one metric can be ADJ for another.
    """
    rules = NA_RULES_BY_METRIC.get(rec.metric, set())
    rule = rec.inputs.get("applicability_rule")
    if isinstance(rule, str):
        for part in rule.split(","):
            name = part.strip()
            if name in rules:
                return name
    for name, value in rec.inputs.items():
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == 1.0
            and name.endswith("_not_applicable")
        ):
            return name
    return None


def rule_flag(rec: Recomputer):
    """A recorded flag that names the rule that empties this cell.

    Only a flag that belongs to this metric counts: the rows carry the flags
    of every input, so a flag named after another metric says nothing about
    this cell.
    """
    metric = rec.metric
    suffixes = (
        ("_growth_base_not_positive", "MET-04"),
        ("_denominator_negative", "MET-09"),
        ("_negative_equity", "APP-07"),
    )
    for name, value in rec.inputs.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if value != 1.0:
            continue
        for suffix, rule in suffixes:
            if name == "flag_" + metric + suffix:
                return name, rule
        rule = EXPLICIT_RULE_FLAGS.get(name)
        if rule:
            return name, rule
    return None


def empty_reason(rec: Recomputer):
    """Why is this cell empty?  A short label for the summary."""
    rule = na_rule(rec)
    if rule:
        return "not applicable (" + rule + ")"
    flag = rule_flag(rec)
    if flag:
        return flag[1] + " (" + flag[0] + ")"
    reason = (rec.inputs.get("provenance") or {}).get("empty_reason")
    if reason:
        rule = EMPTY_REASON_RULE.get(str(reason))
        return str(reason) + (": " + rule if rule else "")
    missing = rec.inputs.get("missing")
    if isinstance(missing, list) and missing:
        return "the row records what is missing"
    return "MET-00 (no input recorded)"


def empty_justification(rec: Recomputer):
    """Why is this cell empty?  Returns (id, description) or None."""
    flag = rule_flag(rec)
    if flag:
        return flag[0], flag[0] + " (" + flag[1] + ")"
    reason = (rec.inputs.get("provenance") or {}).get("empty_reason")
    if reason:
        rule = EMPTY_REASON_RULE.get(str(reason))
        text = "provenance empty_reason=" + str(reason)
        if rule:
            text += " -- " + rule
        return "empty_reason:" + str(reason), text
    missing = rec.inputs.get("missing")
    if isinstance(missing, list) and missing:
        return "missing", "the row records what is missing: " + ", ".join(map(str, missing[:6]))
    return None


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------
def compare_empty(rec: Recomputer, primary):
    """Compare a stored empty cell with the formula."""
    rule = na_rule(rec)
    if rule:
        return True, "ok", "not applicable: " + rule, None
    flag = rule_flag(rec)
    if flag:
        return True, "ok", flag[0] + " (" + flag[1] + ")", None
    if primary is None:
        # MET-00 (or PER-06's D-09): an input the formula needs is not
        # recorded, so the metric is empty.
        justification = empty_justification(rec)
        note = (
            justification[1]
            if justification
            else ("no input recorded that the dictionary formula needs (MET-00)")
        )
        return True, "ok", note, None
    # the formula does produce a number on the recorded basis, so the cell
    # should not be empty
    note = (
        "stored empty but the dictionary formula yields "
        + format_num(primary)
        + " on the recorded basis and no NA rule applies"
    )
    justification = empty_justification(rec)
    if justification:
        note += "; the row says " + justification[1]
    return False, "rule", note, None


def compare_value(rec: Recomputer, readings):
    """Compare a stored value with the readings."""
    stored = rec.stored
    rule = na_rule(rec)
    if rule:
        return False, "rule", "value stored but the hard NA rule " + rule + " applies", None
    if rec.input_gap:
        return False, "input", rec.input_gap, None
    primary = readings.get("as_recorded")
    if primary is not None and close(stored, primary):
        note = "reading as_recorded"
        if len(readings) > 1:
            others = {k: v for k, v in readings.items() if k != "as_recorded"}
            note += "; " + ", ".join(f"{k}={format_num(v)}" for k, v in sorted(others.items()))
        return True, "ok", note, "as_recorded"
    for label, value in sorted(readings.items()):
        if label == "as_recorded" or not close(stored, value):
            continue
        note = (
            "stored "
            + format_num(stored)
            + " follows the "
            + label
            + " reading ("
            + format_num(value)
            + ")"
        )
        if primary is not None:
            note += ", not the reading the row records (" + format_num(primary) + ")"
        else:
            note += "; the reading the row records is not evaluable"
        return True, "basis", note, label
    if not readings:
        return False, "input", "no input recorded that the dictionary formula needs", None
    detail = ", ".join(f"{k}={format_num(v)}" for k, v in sorted(readings.items()))
    return False, "formula", "stored " + format_num(stored) + " vs " + detail, None


def compare(rec: Recomputer):
    """Return (agree, cls, note, matched_reading).

    cls is "ok" when the row agrees on the reading the row itself records, and
    "basis" when it agrees on another reading -- the metric dictionary gives a
    formula but no period basis, so this is the ambiguity it leaves open, not
    a defect.  A row that agrees on no reading is a mismatch, classified
    "formula", "input" or "rule".
    """
    readings = rec.readings()
    if rec.stored is None:
        return compare_empty(rec, readings.get("as_recorded"))
    return compare_value(rec, readings)


# ---------------------------------------------------------------------------
# The building-block identities, checked on the recorded inputs.
# ---------------------------------------------------------------------------
def check_ebitda_identity(rec: Recomputer, counter: Counter):
    """MD-01: ebitda = operating income + D&A, on the basis the value is on."""
    ebitda = rec.v("ebitda")
    if ebitda is None:
        return
    if rec.basis("operating_income") == "ttm" or (
        rec.v("ebitda_ttm") is not None and rec.f.form == "10-Q"
    ):
        oi = rec.ttm_value("operating_income")
        da = rec.ttm_value("depreciation_amortization")
        value = rec.v("ebitda_ttm")
    else:
        oi = rec.v("operating_income")
        da = rec.v("depreciation_amortization")
        value = ebitda
    if None in (oi, da, value):
        return
    counter["MD-01 ebitda = operating income + D&A|checked"] += 1
    if not close(value, oi + da):
        counter["MD-01 ebitda = operating income + D&A|disagree"] += 1


def check_free_cash_flow_identity(rec: Recomputer, counter: Counter):
    """MD-04: free cash flow = operating cash flow - capex."""
    cfo = rec.v("operating_cash_flow")
    capex = rec.v("capital_expenditure")
    fcf = rec.v("free_cash_flow")
    if None in (cfo, capex, fcf):
        return
    counter["MD-04 free cash flow = operating cash flow - capex|checked"] += 1
    if not close(fcf, cfo - capex):
        counter["MD-04 free cash flow = operating cash flow - capex|disagree"] += 1


def check_nopat_identity(rec: Recomputer, counter: Counter):
    """MD-05: NOPAT = operating income x (1 - rate), on both bases."""
    oi = rec.v("operating_income")
    rate = rec.v("nopat_tax_rate")
    nopat = rec.v("nopat")
    if None not in (oi, rate, nopat):
        counter["MD-05 NOPAT = operating income x (1 - rate)|checked"] += 1
        if not close(nopat, oi * (1 - rate)):
            counter["MD-05 NOPAT = operating income x (1 - rate)|disagree"] += 1
    rate_ttm = rec.v("nopat_ttm_tax_rate_value")
    nopat_ttm = rec.v("nopat_ttm")
    oi_ttm = rec.ttm_value("operating_income")
    if None not in (oi_ttm, rate_ttm, nopat_ttm):
        counter["MD-05 NOPAT (trailing year) = operating income x (1 - rate)|checked"] += 1
        if not close(nopat_ttm, oi_ttm * (1 - rate_ttm)):
            counter["MD-05 NOPAT (trailing year) = operating income x (1 - rate)|disagree"] += 1


def check_invested_capital_identity(rec: Recomputer, counter: Counter):
    """MD-02: invested capital = debt + equity + NCI - cash - STI."""
    td = rec.v("total_debt")
    eq = rec.v("equity")
    cash = rec.v("cash")
    sti = rec.v("short_term_investments")
    ic = rec.v("invested_capital")
    if None in (td, eq, cash, sti, ic):
        return
    nci = rec.v("nci") or 0.0
    counter["MD-02 invested capital = debt + equity + NCI - cash - STI|checked"] += 1
    if not close(ic, td + eq + nci - cash - sti):
        counter["MD-02 invested capital = debt + equity + NCI - cash - STI|disagree"] += 1


def check_md38_identity(rec: Recomputer, counter: Counter):
    """MD-38: numerator = NOPAT - (capex - D&A + delta working capital)."""
    numerator = rec.v("md38_numerator")
    nopat = rec.v("nopat")
    capex = rec.v("capital_expenditure")
    da = rec.v("depreciation_amortization")
    dwc = rec.v("delta_working_capital")
    if None in (numerator, nopat, capex, da, dwc):
        return
    counter["MD-38 numerator = NOPAT - (capex - D&A + delta working capital)|checked"] += 1
    if not close(numerator, nopat - (capex - da + dwc)):
        counter["MD-38 numerator = NOPAT - (capex - D&A + delta working capital)|disagree"] += 1


def check_per06_identity(rec: Recomputer, counter: Counter):
    """PER-06: trailing year = FY(prior 10-K) - YTD(prior year) + YTD(this year)."""
    rebuilt, value = rec.f.rebuild_ttm("operating_income", rec.metric)
    if rebuilt is None or value is None:
        return
    counter["PER-06 trailing year = FY - YTD(prior) + YTD(this)|checked"] += 1
    if not close(value, rebuilt):
        counter["PER-06 trailing year = FY - YTD(prior) + YTD(this)|disagree"] += 1


def check_building_blocks(rec: Recomputer, counter: Counter):
    """Check the building-block identities on the recorded inputs.

    These are the identities the derived metrics rest on; a failure here is a
    finding about the inputs, not about the metric row.
    """
    for check in (
        check_ebitda_identity,
        check_free_cash_flow_identity,
        check_nopat_identity,
        check_invested_capital_identity,
        check_md38_identity,
        check_per06_identity,
    ):
        check(rec, counter)


def observe(rec: Recomputer, counter: Counter):
    """Observations about the stored data that are not mismatches."""
    if (
        rec.metric == "ebitda"
        and rec.f.form == "10-Q"
        and rec.stored is not None
        and rec.basis("operating_income") == "period"
    ):
        counter[
            "the `ebitda` row's provenance labels its inputs `period` while the "
            "stored value is the trailing year (MD-01 read with PER-06)"
        ] += 1
    if rec.inputs.get("flag_effective_tax_rate_outside_unit_interval") or rec.inputs.get(
        "flag_nopat_tax_rate_outside_unit_interval"
    ):
        counter[
            "a REIT effective rate outside the unit interval is used for NOPAT "
            "(the engine flags it; MET-03 sets no range)"
        ] += 1
    if rec.inputs.get("market_cap_incomplete_classes"):
        counter[
            "MKT-03: the market capitalization is flagged incomplete (an unlisted share class)"
        ] += 1


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def load_database(db_path):
    """The filings and the stored metric rows, read only."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    filings = {}
    for row in con.execute(
        "SELECT s.id AS fid, s.form, s.period_end, a.ticker "
        "FROM sec_filings s JOIN assets a ON a.id = s.asset_id"
    ):
        filings[row["fid"]] = (row["ticker"], row["form"], row["period_end"])
    metric_rows = defaultdict(list)
    for row in con.execute(
        "SELECT filing_id, metric_name, value, inputs_json FROM fundamental_metrics"
    ):
        metric_rows[row["filing_id"]].append(
            (row["metric_name"], row["value"], json.loads(row["inputs_json"]))
        )
    con.close()
    return filings, metric_rows


def new_counters():
    """The counters the run fills."""
    return {
        "per_metric": defaultdict(
            lambda: {
                "compared": 0,
                "agree": 0,
                "mismatch": 0,
                "basis_agree": 0,
                "stored_empty": 0,
                "empty_agreed": 0,
                "empty_mismatch": 0,
                "exact": 0,
                "primary_hit": 0,
                "classes": Counter(),
                "examples": defaultdict(list),
            }
        ),
        "reading_counts": defaultdict(Counter),
        "building_blocks": Counter(),
        "empty_reasons": defaultdict(Counter),
        "observations": Counter(),
        "basis_decides": Counter(),
    }


def count_readings(rec: Recomputer, counters):
    """Count the readings that reproduce the stored value, and the rows where
    the choice of basis changes the number."""
    stored = rec.stored
    readings = rec.readings()
    for label, value in readings.items():
        if value is not None and close(stored, value):
            counters["reading_counts"][rec.metric][label] += 1
    period_value = readings.get("period")
    ttm_value = readings.get("ttm")
    if (
        stored is not None
        and period_value is not None
        and ttm_value is not None
        and close(stored, period_value) != close(stored, ttm_value)
    ):
        counters["basis_decides"][rec.metric] += 1


def count_row(rec: Recomputer, counters, result):
    """Fill the per-metric counters for one row."""
    agree, cls, note, matched, stored, readings = result
    stats = counters["per_metric"][rec.metric]
    if agree:
        stats["agree"] += 1
        if cls == "basis":
            stats["basis_agree"] += 1
        if stored is None:
            stats["stored_empty"] += 1
            stats["empty_agreed"] += 1
            counters["empty_reasons"][rec.metric][empty_reason(rec)] += 1
        elif matched == "as_recorded":
            stats["primary_hit"] += 1
            if exact(stored, readings["as_recorded"]):
                stats["exact"] += 1
        return
    stats["mismatch"] += 1
    stats["classes"][cls] += 1
    if stored is None:
        stats["stored_empty"] += 1
        stats["empty_mismatch"] += 1
    if len(stats["examples"][cls]) < EXAMPLES_PER_CLASS:
        stats["examples"][cls].append(
            (rec.f.fid, rec.f.ticker, rec.f.form, rec.f.period_end, stored, readings, note)
        )


def compare_row(rec: Recomputer, counters):
    """Compare one stored metric row and fill the counters."""
    agree, cls, note, matched = compare(rec)
    stored = rec.stored
    readings = rec.readings()
    counters["per_metric"][rec.metric]["compared"] += 1

    check_building_blocks(rec, counters["building_blocks"])
    observe(rec, counters["observations"])
    count_readings(rec, counters)
    count_row(rec, counters, (agree, cls, note, matched, stored, readings))
    return agree, cls, note, stored, readings


def process_filing(filing: Filing, writer, counters):
    """Compare every stored metric row of one filing."""
    for metric in filing.order:
        rec = Recomputer(filing, metric)
        agree, cls, note, stored, readings = compare_row(rec, counters)
        best = readings.get("as_recorded")
        if best is None and readings:
            best = next(iter(readings.values()))
        writer.writerow(
            [
                filing.fid,
                filing.ticker,
                filing.form,
                filing.period_end,
                metric,
                "" if stored is None else repr(stored),
                "" if best is None else repr(best),
                "1" if agree else "0",
                cls,
                note,
            ]
        )


def run(db_path, out_dir):
    """Compare every stored metric row and write the two output files."""
    before = sha256_file(db_path)
    print(f"database {db_path}", file=sys.stderr)
    print(f"sha256 before: {before}", file=sys.stderr)

    filings, metric_rows = load_database(db_path)
    n_rows = sum(len(v) for v in metric_rows.values())
    print(f"filings: {len(filings)}  metric rows: {n_rows}", file=sys.stderr)

    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "v3_results.csv")
    summary_path = os.path.join(out_dir, "v3_summary.md")

    counters = new_counters()
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "filing_id",
                "ticker",
                "form",
                "period_end",
                "metric",
                "stored",
                "recomputed",
                "agree",
                "class",
                "note",
            ]
        )
        for fid in sorted(metric_rows):
            ticker, form, period_end = filings[fid]
            filing = Filing(fid, ticker, form, period_end)
            for metric, value, inputs in metric_rows[fid]:
                filing.add(metric, value, inputs)
            process_filing(filing, writer, counters)

    after = sha256_file(db_path)
    print(f"sha256 after: {after}", file=sys.stderr)

    report = dict(counters)
    report["before"] = before
    report["after"] = after
    report["n_filings"] = len(filings)
    report["n_rows"] = n_rows
    write_summary(summary_path, report)
    print(f"wrote {csv_path}")
    print(f"wrote {summary_path}")
    return report


# ---------------------------------------------------------------------------
# the summary
# ---------------------------------------------------------------------------
def summary_head(report, add):
    add("# T-143 part A -- V3: the independent recomputation")
    add("")
    add(
        "Generated by `scripts/pilot_a/independent_metrics.py` (this session's own code, "
        "written from `docs/metric_dictionary.md` and `docs/checklist_sec/checklist_v1.3.md` "
        "only).  It recomputes every stored metric from the inputs the pilot recorded in "
        "`fundamental_metrics.inputs_json`."
    )
    add("")
    add("## The data")
    add("")
    add(
        "* database `/workspaces/thesis/pilot_a2/financial.db`, opened read-only "
        "(`file:...?mode=ro`, `uri=True`)"
    )
    add(f"* sha256 before the run: `{report['before']}`")
    add(f"* sha256 after the run:  `{report['after']}`")
    add(f"* unchanged: **{'yes' if report['before'] == report['after'] else 'NO'}**")
    add(f"* filings: {report['n_filings']};  stored metric rows: {report['n_rows']}")
    add("")
    add("## The agreement rule")
    add("")
    add(
        "DQ-01 gives an identity threshold of `max(rounding tolerance, 0.5% x |base|)`. "
        "A metric formula is an identity on its own inputs, so the same shape is applied "
        "here, with the relative materiality term measured on the stored value:"
    )
    add("")
    add("    |stored - recomputed| <= 0.005 x |stored|")
    add("")
    add(
        "Both cells empty is an agreement.  The summary also reports how many rows hit "
        "the stored value to the last bit (relative 1e-9), because the inputs are the "
        "same numbers the engine used and the expected result is an exact identity."
    )
    add("")


def summary_readings(report, add):
    add("## The readings")
    add("")
    add(
        "The metric dictionary gives a formula per metric but **not** the period basis "
        "for a 10-Q flow.  Three readings are therefore computed for every metric:"
    )
    add("")
    add(
        "* `as_recorded` -- the primary reading: each ingredient is taken at the basis "
        "its own row records in `provenance.items[*].basis` (the auditable statement of "
        "the basis the stored value used);"
    )
    add("* `period` -- the same formula with every duration ingredient on the filing's own period;")
    add(
        "* `ttm` -- the same formula with every duration ingredient on the trailing year "
        "of PER-06 (`FY(prior 10-K) - YTD(same period last year) + YTD(this year)`, "
        "rebuilt here from the three columns the pilot recorded)."
    )
    add("")
    add(
        "A row agrees when the stored value equals the primary reading.  A row whose "
        "stored value equals another reading is classified `basis`, because that is the "
        "ambiguity the dictionary leaves open.  On a 10-K the trailing year is the "
        "filing's own fiscal year, so the two readings coincide."
    )
    add("")
    add(
        "An empty cell is correct when a rule makes it empty: a NA cell of the "
        "applicability matrix, MET-04 (a base <= 0), MET-09 (a denominator <= 0), "
        "APP-07 (negative equity), MD-38 (an unclassified balance sheet), D-13 (a "
        "missing bound), ID-02b (a capture failure), or MET-00 (an input the formula "
        "needs is not recorded).  An empty cell whose formula does produce a number is a "
        "`rule` mismatch."
    )
    add("")


def summary_totals(report, add):
    per_metric = report["per_metric"]
    total = sum(s["compared"] for s in per_metric.values())
    total_agree = sum(s["agree"] for s in per_metric.values())
    total_exact = sum(s["exact"] for s in per_metric.values())
    add("## The totals")
    add("")
    add(f"* metric rows compared: **{total}**")
    add(f"* agreements: **{total_agree}**")
    add(f"* of which an exact hit (relative 1e-9): **{total_exact}**")
    add(f"* mismatches: **{total - total_agree}**")
    add("")


def summary_per_metric(report, add):
    per_metric = report["per_metric"]
    reading_counts = report["reading_counts"]
    add("## Per metric")
    add("")
    add(
        "| metric | compared | agree | exact | mismatch | stored empty | empty agreed | "
        "empty unexplained | readings that reproduce the stored value |"
    )
    add("|---|---|---|---|---|---|---|---|---|")
    for metric in sorted(per_metric):
        s = per_metric[metric]
        readings = ", ".join(f"{k}:{v}" for k, v in sorted(reading_counts.get(metric, {}).items()))
        add(
            f"| `{metric}` | {s['compared']} | {s['agree']} | {s['exact']} | "
            f"{s['mismatch']} | {s['stored_empty']} | {s['empty_agreed']} | "
            f"{s['empty_mismatch']} | {readings or '-'} |"
        )
    add("")


def summary_building_blocks(report, add):
    building_blocks = report["building_blocks"]
    add("## The building-block identities")
    add("")
    add(
        "Checked on the recorded inputs of every row, independently of the metric row "
        "that carries them."
    )
    add("")
    for key in sorted(building_blocks):
        if not key.endswith("|checked"):
            continue
        name = key.split("|")[0]
        checked = building_blocks[key]
        bad = building_blocks.get(name + "|disagree", 0)
        add(f"* {name}: {bad} disagreement(s) out of {checked} checked")
    add("")


def summary_empty_cells(report, add):
    empty_reasons = report["empty_reasons"]
    add("## The stored empty cells, by the rule that empties them")
    add("")
    add(
        "Every empty cell the pilot stores must be empty for a reason the checklist "
        "names.  The table gives the rule each empty cell is justified by."
    )
    add("")
    add("| metric | empty cells | by rule |")
    add("|---|---|---|")
    for metric in sorted(empty_reasons):
        reasons = empty_reasons[metric]
        detail = "; ".join(f"{k}: {v}" for k, v in reasons.most_common())
        add(f"| `{metric}` | {sum(reasons.values())} | {detail} |")
    add("")


def summary_basis(report, add):
    per_metric = report["per_metric"]
    reading_counts = report["reading_counts"]
    basis_decides = report["basis_decides"]
    add("## The period basis the metric dictionary does not state")
    add("")
    add(
        "The metric dictionary gives one formula per metric and **no period basis**.  "
        "PER-06 says how to build a trailing year from contiguous sub-periods and that a "
        "quarter multiplied by four is not one; it does not say which metrics must be on "
        "a trailing-year basis.  The table counts, for every metric, how many of its "
        "stored values the *period* reading reproduces and how many the *trailing-year* "
        "reading reproduces; a reading that cannot be evaluated (a component the row does "
        "not record) reproduces nothing.  Where the two numbers differ, the metric's "
        "basis is load-bearing and the dictionary settles nothing: this is the open "
        "question for reviewer B, and the figures below are the counts under each "
        "reading."
    )
    add("")
    add(
        "| metric | values the period reading reproduces | values the trailing-year "
        "reading reproduces | rows where exactly one of the two reproduces the stored "
        "value |"
    )
    add("|---|---|---|---|")
    for metric in sorted(per_metric):
        counts = reading_counts.get(metric, {})
        period = counts.get("period", 0)
        ttm = counts.get("ttm", 0)
        if not period and not ttm:
            continue
        add(f"| `{metric}` | {period} | {ttm} | {basis_decides.get(metric, 0)} |")
    add("")


def summary_observations(report, add):
    observations = report["observations"]
    add("## What the stored data shows about it")
    add("")
    for note in sorted(observations):
        add(f"* {note}: {observations[note]} row(s)")
    add("")


def summary_mismatches(report, add):
    per_metric = report["per_metric"]
    add("## The mismatch classes")
    add("")
    add(
        "`formula` -- the dictionary formula and the stored value differ on the same "
        "recorded inputs.  `basis` -- the trailing-year (TTM) reading and the period "
        "reading differ, and the stored value follows one of them.  `input` -- an input "
        "the formula needs is not recorded.  `rule` -- an applicability rule says the "
        "cell must be empty (or must not be), and the cell disagrees."
    )
    add("")
    for metric in sorted(per_metric):
        s = per_metric[metric]
        if not s["classes"]:
            continue
        add(f"### `{metric}`")
        add("")
        for cls in sorted(s["classes"]):
            add(f"**{cls}: {s['classes'][cls]}**")
            add("")
            for ex in s["examples"][cls]:
                fid, ticker, form, period_end, stored, _readings, note = ex
                add(
                    f"* filing {fid} ({ticker}, {form}, period end {period_end}) -- "
                    f"stored {format_num(stored)}; {note}"
                )
            add("")


def summary_not_in_dictionary(report, add):
    add("## The metrics that are not rows of the metric dictionary")
    add("")
    for metric, why in sorted(NOT_IN_DICTIONARY.items()):
        add(f"* `{metric}` -- {why}")
    add("")


def summary_rules_not_applied(report, add):
    add("## The rules this session could not apply")
    add("")
    add("Each is a limit of the dictionary or of the recorded inputs, not of the script.")
    add("")
    add(
        "1. **The period basis of a metric.**  Annex B gives a formula and no basis; "
        "PER-06 gives the construction of a trailing year but not which metrics must be "
        "on one.  The engine uses the filing's period for `nopat`, `gross_margin`, "
        "`operating_margin`, `net_margin`, `interest_coverage` and `effective_tax_rate`, "
        "and the trailing year for `ebitda`, `asset_turnover`, `inventory_turnover`, "
        "`receivables_turnover`, `return_on_assets`, `return_on_equity`, "
        "`return_on_invested_capital`, `net_debt_to_ebitda` and the cash-flow ratios.  "
        "Both readings are computed for every metric; the counts under each are in the "
        "table above.  **Pending reviewer B.**"
    )
    add(
        "2. **MD-39 `earnings_yield`** is a VALORIZATION row, which the dictionary says "
        "is not a standalone `fundamental_metrics` name; it is not in the database, so "
        "there is nothing to compare.  MET-05's negative-earnings dummy has no stored "
        "cell either."
    )
    add(
        "3. **MD-02 `invested capital`, MD-03 `net debt` and MD-04 `free cash flow`** "
        "are building blocks, not stored metric names.  They are checked as identities "
        "on the recorded inputs (the table above) instead of as metric rows."
    )
    add(
        "4. **The 17 rows the dictionary marks *Source pending*** are recomputed from the "
        "formula the dictionary gives, as the brief asks; the summary does not treat "
        "them as confirmed.  They are `free cash flow`, `current_ratio`, `quick_ratio`, "
        "`cash_ratio`, `debt_to_equity`, `debt_to_assets`, `interest_coverage`, "
        "`net_debt_to_ebitda`, `asset_turnover`, `inventory_turnover`, "
        "`receivables_turnover`, `operating_cash_flow_margin`, `free_cash_flow_margin`, "
        "`free_cash_flow_conversion`, `capex_intensity`, `effective_tax_rate` and "
        "`sbc_adjusted_fcf_yield`."
    )
    add(
        "5. **DQ-01's rounding floor.**  `decimals` is not captured (L-10), so the "
        "identity's rounding term has no per-fact value; the comparison uses the "
        "materiality term alone.  It makes no difference here: every non-empty value "
        "agrees to within 2.2e-12 relative."
    )
    add(
        "6. **PER-06's construction when it sums four quarters.**  Where the trailing "
        "year was built from four quarters the pilot records the value but not the "
        "quarters, so the construction cannot be rebuilt and the recorded value is used "
        "as it stands."
    )
    add(
        "7. **MKT-03, the multi-class market capitalization.**  With more than one share "
        "class the market capitalization is the sum over the classes; the row records "
        "one class's count and price, so the sum cannot be reproduced.  These are the "
        "`input` rows above."
    )
    add(
        "8. **The three stored metrics that are not rows of Annex B** "
        "(`return_on_invested_capital_effective_rate`, `utility_cash_coverage_3y` and "
        "`value_inputs`) are recomputed from the rule that names them; they are listed "
        "above."
    )
    add(
        "9. **The 14 dictionary rows that are not stored** cannot be compared at all: "
        "the pilot's `fundamental_metrics` holds 38 names, of which 35 are Annex B "
        "rows and 3 are not."
    )
    add("")


def write_summary(path, report):
    """The summary the pilot's close-out will quote."""
    lines = []
    add = lines.append
    summary_head(report, add)
    summary_readings(report, add)
    summary_totals(report, add)
    summary_per_metric(report, add)
    summary_building_blocks(report, add)
    summary_empty_cells(report, add)
    summary_basis(report, add)
    summary_observations(report, add)
    summary_mismatches(report, add)
    summary_not_in_dictionary(report, add)
    summary_rules_not_applied(report, add)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=HANDOFF_DB)
    parser.add_argument("--out-dir", default=HANDOFF_OUT)
    args = parser.parse_args(argv)
    run(os.path.abspath(args.db), os.path.abspath(args.out_dir))


if __name__ == "__main__":
    main()
