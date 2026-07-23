"""DirectLake remediation ROUTER for stripped calculated columns (Option 3 -- materialize upstream).

A Direct Lake table physically reads Delta columns and cannot carry calculated columns (the AS
engine rejects them). When the DirectLake seam rebinds a base table it therefore strips that table's
calculated columns. Dropping them silently is a fidelity hole -- visuals that referenced them break.
This module decides, per stripped column, the *correct* production remediation instead of a blanket
"dropped", so the report hands the customer an actionable, honest plan aligned with documented Fabric
guidance ("perform data-preparation logic upstream in the architecture to maximize reusability" --
learn.microsoft.com/fabric/fundamentals/direct-lake-overview).

Four remediation buckets, each a distinct action:

  * :data:`MATERIALIZE_UPSTREAM` -- a genuine row-level derived column (deterministic, no aggregation,
    no addressing, no parameter) whose value can be computed once *upstream* in the Lakehouse as a
    real Delta/SQL column. DirectLake then reads it natively. This is the best-practice target and the
    only bucket that recovers the column as a physical DirectLake column.
  * :data:`FIELD_PARAMETER` -- the column is a Tableau *parameter / what-if* construct. Its Power BI
    home is a field parameter / what-if parameter (both are *supported* on Direct Lake because they
    implicitly create calculated tables that don't reference Direct Lake columns), NOT a data column.
  * :data:`MEASURE_WORKLIST` -- the expression is an aggregation / table calc / LOD: it is a MEASURE,
    never a physical column, so materializing upstream is wrong. It belongs in the DAX measure
    worklist a human finishes.
  * :data:`REVIEW` -- no safe deterministic remediation (DAX-language gap, unresolved reference, or an
    unclassifiable shape). Keep the honest stub and flag for human review.

Design contract (mirrors :mod:`translation_router`):
  * **Pure and dependency-free.** Reads only strings (a column name, its DAX, an optional role and an
    optional :func:`translation_router.classify_fallback` category). Emits NO SQL and NO model objects
    -- it merely *labels*. SQL / field-parameter emission is a separate, downstream step.
  * **Never raises.** An unrecognized shape falls through to :data:`REVIEW`.
  * **Additive.** Advisory metadata layered onto the existing strip manifest; it never changes which
    columns are stripped or the live/inert status of any calc.
"""
from __future__ import annotations

import re

try:  # pragma: no cover - import shim so the module works both as a package and a loose script
    from translation_router import MODEL_OBJECT_PARAMETER
except Exception:  # pragma: no cover
    MODEL_OBJECT_PARAMETER = "model_object_parameter"

# --------------------------------------------------------------------------- remediation buckets
MATERIALIZE_UPSTREAM = "materialize_upstream"
FIELD_PARAMETER = "field_parameter"
MEASURE_WORKLIST = "measure_worklist"
REVIEW = "review"

BUCKETS = (MATERIALIZE_UPSTREAM, FIELD_PARAMETER, MEASURE_WORKLIST, REVIEW)

# DAX functions that make an expression a MEASURE (aggregation / iterator / table calc / context
# transition) -- never a physical column, so they can't be materialized as a Delta column.
_AGGREGATION_FUNCS = frozenset({
    "SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "COUNTROWS", "DISTINCTCOUNT",
    "SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "PRODUCT", "PRODUCTX", "MEDIAN", "MEDIANX",
    "PERCENTILE.INC", "PERCENTILE.EXC", "PERCENTILEX.INC", "PERCENTILEX.EXC",
    "STDEV.S", "STDEV.P", "VAR.S", "VAR.P", "GEOMEAN", "GEOMEANX",
    "CALCULATE", "CALCULATETABLE",
    "RANKX", "RANK", "TOPN", "SAMPLE",
    "ALL", "ALLEXCEPT", "ALLSELECTED", "REMOVEFILTERS", "KEEPFILTERS", "FILTER",
    "SUMMARIZE", "SUMMARIZECOLUMNS", "ADDCOLUMNS", "GROUPBY", "SELECTCOLUMNS", "GENERATE",
    "OFFSET", "WINDOW", "INDEX", "MOVINGAVERAGE", "RUNNINGSUM", "PREVIOUS", "EARLIER", "EARLIEST",
})

# Signals that the column is a parameter / what-if construct (a MODEL OBJECT, not a data column).
_PARAMETER_FUNCS = frozenset({"SELECTEDVALUE", "GENERATESERIES"})
_PARAMETER_REF_RE = re.compile(r"\[\s*Parameters?\s*\]", re.IGNORECASE)

# A base-column reference: ``'Table'[Col]`` or a bare ``[Col]``. A "row-level" expression that
# references NO column (e.g. BLANK(), "", a literal) is a stubbed placeholder -- there is nothing to
# materialize from source -- so it must NOT be routed to materialize-upstream.
_COLUMN_REF_RE = re.compile(r"'[^']*'\s*\[[^\]]+\]|\[[^\]]+\]")

# Row-level, deterministic functions that translate cleanly to a Lakehouse SQL / Delta computed
# column. Presence of ONLY these (plus operators / column refs / literals) => materialize upstream.
_ROW_LEVEL_FUNCS = frozenset({
    # date parts
    "YEAR", "MONTH", "DAY", "DATE", "HOUR", "MINUTE", "SECOND", "WEEKDAY", "WEEKNUM",
    "QUARTER", "EOMONTH", "EDATE", "DATEVALUE", "FORMAT",
    # text
    "LEFT", "RIGHT", "MID", "LEN", "UPPER", "LOWER", "TRIM", "SUBSTITUTE", "REPLACE",
    "CONCATENATE", "CONCATENATEX", "COMBINEVALUES", "UNICHAR", "REPT", "SEARCH", "FIND",
    "VALUE", "FIXED", "PROPER",
    # numeric / logical
    "ABS", "ROUND", "ROUNDUP", "ROUNDDOWN", "INT", "TRUNC", "MOD", "DIVIDE", "SIGN",
    "POWER", "SQRT", "EXP", "LN", "LOG", "CEILING", "FLOOR", "ISBLANK", "ISERROR",
    "IF", "IFERROR", "SWITCH", "AND", "OR", "NOT", "TRUE", "FALSE", "BLANK", "COALESCE",
    "CONVERT", "CURRENCY", "RELATED",
})

# Functions with no faithful native form (kept in sync with translation_router._DAX_GAP_FUNCS intent)
# -- if the expression leans on one of these it cannot be safely materialized upstream either.
_DAX_GAP_FUNCS = frozenset({
    "SPLIT", "REGEXP_MATCH", "REGEXP_EXTRACT", "REGEXP_REPLACE", "DATEPARSE", "FINDNTH",
    "RANK_UNIQUE", "HEXBINX", "HEXBINY", "MAKETIME", "MAKEDATETIME",
})

_FUNC_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_\.]*)\s*\(")
# String literals may contain text like ``"Printer (2nd Generation)"`` whose ``word (`` would
# otherwise be misread as a function call. Blank them out before scanning for function names.
_STRING_LITERAL_RE = re.compile(r'"(?:[^"]|"")*"')


def _function_names(dax):
    """Return the set of UPPERCASE function names called in *dax* (a token immediately followed by
    ``(``). String-literal contents are ignored, and column/measure references in ``[...]`` are not
    function calls, so both are excluded."""
    if not dax:
        return set()
    cleaned = _STRING_LITERAL_RE.sub('""', str(dax))
    return {m.group(1).upper() for m in _FUNC_CALL_RE.finditer(cleaned)}


def classify_directlake_remediation(name, dax=None, *, role=None, category=None):
    """Route ONE stripped calculated column to a remediation :data:`BUCKETS` value + rationale.

    ``name``     -- the column's display name (used only for the rationale text).
    ``dax``      -- its translated DAX expression (the text after ``=`` in the TMDL calc column), or
                    ``None``/empty for a column that never translated to a faithful expression.
    ``role``     -- ``"measure"`` / ``"dimension"`` when known; a measure-role column is never a
                    physical column.
    ``category`` -- an optional :func:`translation_router.classify_fallback` category. A
                    :data:`~translation_router.MODEL_OBJECT_PARAMETER` category routes straight to
                    :data:`FIELD_PARAMETER` regardless of the (stub) DAX.

    Returns ``{"name", "bucket", "rationale"}``. Never raises; an unclassifiable column -> ``REVIEW``.
    """
    dax = "" if dax is None else str(dax)
    funcs = _function_names(dax)

    # 1) Parameter / what-if construct -> field parameter (model object, not a data column).
    if category == MODEL_OBJECT_PARAMETER or (funcs & _PARAMETER_FUNCS) or _PARAMETER_REF_RE.search(dax):
        return {"name": name, "bucket": FIELD_PARAMETER,
                "rationale": "Tableau parameter / what-if construct -- model as a field parameter "
                             "(or what-if parameter), which Direct Lake supports; not a data column."}

    # 2) No faithful expression to work with, or a known DAX-language gap -> human review, keep stub.
    if funcs & _DAX_GAP_FUNCS:
        return {"name": name, "bucket": REVIEW,
                "rationale": "Relies on a construct with no faithful native form "
                             f"({', '.join(sorted(funcs & _DAX_GAP_FUNCS))}) -- keep the stub and "
                             "review; it cannot be safely materialized upstream."}
    if not dax.strip():
        return {"name": name, "bucket": REVIEW,
                "rationale": "No faithful translated expression to materialize -- review and author "
                             "the intended DAX/SQL by hand."}

    # 3) Aggregation / table-calc / context transition -> it is a MEASURE, never a physical column.
    if role == "measure" or (funcs & _AGGREGATION_FUNCS):
        return {"name": name, "bucket": MEASURE_WORKLIST,
                "rationale": "Aggregation / table calculation -- this is a measure, not a physical "
                             "column; add it to the DAX measure worklist rather than materializing."}

    # 3.5) A constant / stubbed expression with NO base-column reference cannot be materialized --
    #      there is nothing to compute from source (BLANK(), "", a bare literal). In practice these
    #      are parameter / visual-control placeholders that never translated to a real column.
    if not _COLUMN_REF_RE.search(dax):
        return {"name": name, "bucket": REVIEW,
                "rationale": "Stubbed constant with no source-column reference (e.g. BLANK() / empty "
                             "string) -- nothing to materialize; the original Tableau calc (often a "
                             "parameter or visual control) did not translate. Model as a field "
                             "parameter or author by hand."}

    # 4) Purely row-level & deterministic (only row-level funcs, or no functions at all -- a rename /
    #    arithmetic on base columns) -> materialize upstream as a Lakehouse SQL / Delta column.
    unknown = funcs - _ROW_LEVEL_FUNCS
    if not unknown:
        return {"name": name, "bucket": MATERIALIZE_UPSTREAM,
                "rationale": "Row-level deterministic expression -- materialize once upstream in the "
                             "Lakehouse (SQL view / computed column) so Direct Lake reads it natively."}

    # 5) Anything else (unrecognized function mix) -> review; don't guess a physical column.
    return {"name": name, "bucket": REVIEW,
            "rationale": "Uses functions that aren't confirmed row-level-deterministic "
                         f"({', '.join(sorted(unknown))}) -- review before materializing upstream."}


def classify_stripped_columns(columns):
    """Route a LIST of stripped columns, returning ``{"buckets": {bucket: [rows]}, "counts": {...}}``.

    Each item in *columns* is a dict ``{"name", "dax"?, "role"?, "category"?}`` (a bare string is
    also accepted and treated as a name with no DAX). The result groups the routed rows by bucket and
    provides a per-bucket count, so the caller (report / pipeline) can render an actionable plan.
    """
    grouped = {b: [] for b in BUCKETS}
    for col in columns or []:
        if isinstance(col, str):
            col = {"name": col}
        routed = classify_directlake_remediation(
            col.get("name"), col.get("dax"), role=col.get("role"), category=col.get("category"))
        grouped[routed["bucket"]].append(routed)
    return {"buckets": grouped, "counts": {b: len(grouped[b]) for b in BUCKETS}}
