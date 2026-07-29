"""Reconcile the DAX the DETERMINISTIC pass emitted against its Tableau source, over landed rows.

The reconciliation oracle already knows how to prove two expressions agree over real data. Until now
it was only ever pointed at the *second compiler's* output -- the hard calcs that would otherwise stay
stubs. That is the small end of the distribution. The overwhelming majority of every model this tool
builds comes from the deterministic pass, and none of it was ever evaluated against a single row.

This module closes that gap. It is deliberately NOT wired into the second compiler: that pass excludes
deterministic calcs on purpose, because its job is to *land* DAX that does not exist yet, and a calc
the normal build already lands must keep its ``deterministic`` provenance. Re-landing it would launder
a syntactic translation into an empirical one. So this is a separate, read-only sweep: it changes no
DAX, lands nothing, and only reports.

Three outcomes per expression, and the difference between them is the entire point:

- **verified** -- the oracle parsed BOTH the Tableau formula and the generated DAX, evaluated both over
  the rows on disk, and they agreed at every grain it could compare. This is the only outcome that is
  evidence of correctness.
- **disagreement** -- both sides evaluated and produced DIFFERENT numbers. This is the most serious
  finding the tool can produce: a translation that looks right, opens right, and is wrong. It fails
  the definition-of-done.
- **unproven** -- the oracle could not decide (outside its supported subset, multi-table, no landed
  rows for that table). Translated, not checked. Reported with a reason, never as a pass.

Expect ``unproven`` to dominate on a first run. The oracle's subset is arithmetic and conditionals over
single-column aggregations on one table, so a realistic estate reports a minority verified. That is the
honest number; a small proven count is worth more than a large translated one, and this module exists to
stop those two being confused. The reason buckets are the point of the report as much as the counts are:
they say which limit an estate is actually hitting, so the next thing to widen is measured rather than
guessed.

Pure stdlib. Read-only. Fail-safe: any error on one expression is recorded as unproven, never raised.
"""

import re

try:
    from .reconciliation_oracle import reconcile, load_tables_from_csv, PASS, FAIL
except ImportError:  # flat-script execution
    from reconciliation_oracle import reconcile, load_tables_from_csv, PASS, FAIL


_EXTRACT_GUID = re.compile(r"_[0-9a-fA-F]{32}$")
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _norm_table(name):
    """Normalize a table name or CSV stem for comparison (``Extract_Orders_<guid>`` -> ``orders``)."""
    s = str(name or "").strip()
    s = _EXTRACT_GUID.sub("", s).lower()
    if s.startswith("extract_"):
        s = s[len("extract_"):]
    return _NON_ALNUM.sub("", s)


def align_table_names(table_csv_paths, table_names):
    """Alias landed CSVs onto the MODEL table names the generated DAX actually references.

    A ``.hyper`` extract lands as ``Extract_Orders_<32-hex>.csv`` while the model -- and therefore the
    DAX -- calls the table ``Orders``. Without this, every expression comes back "no rows landed for
    that table" and the sweep silently proves nothing while looking like it ran.

    An alias is added ONLY on an exact match of the normalized names, and ONLY when exactly one stem
    matches. An ambiguous match is skipped rather than guessed: feeding the oracle the wrong table's
    rows could manufacture a proven "disagreement", and a false accusation of wrong numbers is far
    worse than an honest unproven. Original keys are preserved; never raises.
    """
    out = dict(table_csv_paths or {})
    by_norm = {}
    for stem in list(out):
        by_norm.setdefault(_norm_table(stem), []).append(stem)
    for name in (table_names or []):
        if not name or name in out:
            continue
        matches = by_norm.get(_norm_table(name)) or []
        if len(matches) == 1:
            out[name] = out[matches[0]]
    return out


# Stable buckets for the oracle's free-text ``reason``. The raw strings interpolate table names and
# row counts, so counting them verbatim yields a histogram with one entry per expression -- useless.
# These buckets are what a reader actually needs: which LIMIT stopped the check, and can they lift it.
_REASON_BUCKETS = (
    ("outside oracle subset", "expression is outside the oracle's supported subset"),
    ("multiple tables", "expression spans multiple tables (the oracle is single-table)"),
    ("no table at all", "expression references no table, so there is nothing to evaluate it over"),
    ("no landed data", "no rows landed for that table"),
    ("more than max_rows", "table is too large to evaluate"),
    ("no comparable", "no non-null grain to compare at"),
    ("evaluation error", "the expression could not be evaluated over the landed rows"),
    ("references table", "the DAX and the Tableau formula resolve to different tables"),
    ("grain outside", "the requested grain is outside the oracle's supported subset"),
)


def _bucket(reason):
    low = (reason or "").lower()
    for needle, label in _REASON_BUCKETS:
        if needle in low:
            return label
    return "the oracle could not decide"


def _rows(measure_report, calc_column_report):
    """Yield ``(name, table, role, tableau_formula, dax)`` for every LIVE translated expression.

    A stub carries no DAX -- there is nothing to reconcile, and the pipeline has already disclosed it
    as needing review. An expression with no retained Tableau formula cannot be reconciled either:
    the oracle needs BOTH sides, and checking DAX against itself would prove nothing while looking
    like a pass.
    """
    for row in (measure_report or []):
        if not isinstance(row, dict):
            continue
        dax, formula = row.get("dax"), row.get("tableau_formula")
        if not dax or not formula:
            continue
        yield (row.get("measure"), ((row.get("source") or {}).get("model_table")), "measure",
               formula, dax)
    for row in (calc_column_report or []):
        if not isinstance(row, dict):
            continue
        dax, formula = row.get("dax"), row.get("tableau_formula")
        if not dax or not formula:
            continue
        yield (row.get("column"), row.get("table"), "column", formula, dax)


def sweep_translations(measure_report, calc_column_report=None, *, tables=None,
                       table_csv_paths=None, resolver=None, max_rows=500000):
    """Reconcile every deterministically-translated expression against its Tableau source.

    ``tables`` is the loaded oracle table shape; pass ``table_csv_paths`` instead to load landed CSVs
    here. ``resolver(caption) -> (table, column, ...)`` maps a Tableau field caption onto its model
    column and is what lets the Tableau side of each pair be evaluated at all -- without it almost
    everything comes back unproven.

    Returns::

        {"ok": bool,                  # False ONLY on a proven disagreement
         "checked": int,              # expressions the oracle was asked about
         "verified": int,             # proven equal over landed rows
         "unproven": int,             # could not be decided (see unproven_reasons)
         "disagreements": [ {...} ],  # proven UNEQUAL -- the loud case, full evidence per entry
         "verified_objects": [names],
         "unproven_reasons": {bucket: count}}

    ``ok`` is False only for a disagreement. An estate that proves nothing is not a failing estate --
    it is an unchecked one, and that distinction is reported rather than graded.
    """
    if tables is None and table_csv_paths:
        try:
            tables = load_tables_from_csv(table_csv_paths, max_rows=max_rows)
        except Exception:
            tables = None
    if not tables:
        return {"ok": True, "checked": 0, "verified": 0, "unproven": 0, "disagreements": [],
                "verified_objects": [], "unproven_reasons": {}}

    try:
        rows = list(_rows(measure_report, calc_column_report))
    except Exception:
        rows = []

    verified_objects = []
    disagreements = []
    unproven_reasons = {}
    checked = 0

    for name, table, role, formula, dax in rows:
        checked += 1
        try:
            verdict = reconcile(formula, dax, tables, resolver=resolver, max_rows=max_rows)
        except Exception as exc:
            unproven_reasons["the oracle could not decide"] = (
                unproven_reasons.get("the oracle could not decide", 0) + 1)
            del exc
            continue
        status = (verdict or {}).get("status")
        if status == PASS:
            verified_objects.append(name)
        elif status == FAIL:
            # Both sides evaluated and produced different numbers. Keep the full evidence -- the
            # grain, and both values -- because "measure X is wrong" without the number a reviewer
            # can reproduce is an accusation, not a finding.
            disagreements.append({
                "object": name, "table": table, "role": role,
                "grain": verdict.get("grain"),
                "tableau_value": verdict.get("tableau_value"),
                "candidate_value": verdict.get("candidate_value"),
                "rows": verdict.get("rows"),
                "detail": verdict.get("reason"),
                "tableau_formula": formula,
                "dax": dax,
            })
        else:
            bucket = _bucket((verdict or {}).get("reason"))
            unproven_reasons[bucket] = unproven_reasons.get(bucket, 0) + 1

    return {
        "ok": not disagreements,
        "checked": checked,
        "verified": len(verified_objects),
        "unproven": checked - len(verified_objects) - len(disagreements),
        "disagreements": disagreements,
        "verified_objects": sorted(n for n in verified_objects if n),
        "unproven_reasons": unproven_reasons,
    }
