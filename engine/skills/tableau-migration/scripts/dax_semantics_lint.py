"""Static semantic lint over the GENERATED DAX (the "compiles clean, ships wrong" backstop).

``tmdl_lint`` and ``openability_gate`` answer "will this model open?". They cannot answer "is this
model *right*?", and the gap between those two questions is where the expensive migration defects
live: a model that deserializes perfectly, opens without complaint, and reports the wrong number.

This module is the third check in that family -- pure-Python, dependency-free, offline, run on every
migration. It reads the per-measure report (which holds the original Tableau formula AND the
generated DAX side by side) plus the model manifest, and looks for defect classes that no
well-formedness check can see. Every detector here is grounded in a defect observed in a real
migration, not invented:

BLOCKING -- the model is genuinely invalid; it deserializes and opens, and then fails on use:

* ``compact_filter_with_measure``  A measure used in a CALCULATE compact boolean filter
                                   (``CALCULATE(<expr>, 'T'[C] = [Measure])``). DAX forbids this --
                                   the filter must be hoisted to a VAR -- but the TMDL is
                                   structurally perfect, so nothing catches it until Desktop
                                   evaluates the measure. One public Tableau->Power BI migration hit
                                   this ONE structural mistake repeated 58 times across a single
                                   91-worksheet workbook, which is exactly the shape of a systematic
                                   translator bug: invisible to every structural gate, everywhere at
                                   once. Only flagged when the bracket name matches a measure this
                                   build actually emitted, so a legal ``FILTER(...)`` predicate or an
                                   unqualified column reference can never be mistaken for it.
* ``duplicate_measure_name``       Two measures sharing a name. Tabular requires measure names to be
                                   unique model-wide (they are referenced unqualified), and the
                                   violation only surfaces on model commit.
* ``measure_shadows_column``       A measure whose name equals a column name in its own table.
                                   Measures and columns share one namespace within a table, so the
                                   object cannot be committed.

ADVISORY -- the DAX is legal and the model works; the NUMBER is likely wrong. These are heuristics
over aggregation semantics, so they are reported for a human to judge, never used to fail a build:

* ``countd_reaggregated``          A distinct count re-aggregated by SUM/AVERAGE (directly, or
                                   through a measure whose own expression is a DISTINCTCOUNT).
                                   A distinct count is not additive: summing it across groups
                                   double-counts every member that appears in more than one group.
* ``ratio_reaggregated``           A ratio measure (DIVIDE / division) re-aggregated by
                                   SUM(X)/AVERAGE(X). The average of averages is not the average --
                                   it silently drops the denominator weighting, and the result stays
                                   plausible enough to ship (the classic symptom is a percentage
                                   that creeps just past 100%).

Deliberately NOT a coverage claim. This lint proves the ABSENCE of specific known defects over the
measures it examined; it says nothing about whether a translation is faithful to its Tableau source.
That question belongs to ``reconciliation_oracle``, which evaluates both sides over landed rows.
A clean lint and a verified translation are different facts and are reported separately.

Fail-safe throughout: a malformed row, an unparseable expression, or a stub measure (no DAX) is
skipped, never flagged. A detector that cannot decide stays silent.
"""
from __future__ import annotations

import re

BLOCKING = "blocking"
ADVISORY = "advisory"

# A function call: an identifier followed by ``(``. The lookbehind keeps ``'Table'(`` and a longer
# identifier's tail from matching, so only real call heads are found.
_CALL_RE = re.compile(r"(?<![\w'])([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
# A BARE ``[Name]`` reference -- i.e. a MEASURE reference. A column is always written either
# ``'Table'[Col]`` (preceded by a quote) or ``Table[Col]`` (preceded by a word character), so
# excluding those two predecessors leaves exactly the unqualified measure form.
_BARE_REF_RE = re.compile(r"(?<!['\w\]])\[([^\[\]]+)\]")
# Longest-first so ``<=`` is never read as ``<`` followed by ``=``.
_COMPARISONS = ("<=", ">=", "<>", "=", "<", ">")
_CALCULATE_FNS = ("CALCULATE", "CALCULATETABLE")
_ADDITIVE_FNS = ("SUM", "SUMX", "AVERAGE", "AVERAGEX")


def _blank_strings(dax):
    """Blank out double-quoted string literals, preserving length so offsets stay valid.

    A bracket or comma inside a text literal is data, not syntax; leaving it in place would let
    ``"a, b"`` split an argument list or ``"[x]"`` register as a measure reference.
    """
    out = []
    i, n = 0, len(dax)
    while i < n:
        ch = dax[i]
        if ch != '"':
            out.append(ch)
            i += 1
            continue
        out.append(" ")
        i += 1
        while i < n:
            if dax[i] == '"':
                # A doubled "" is an escaped quote inside the literal, not the end of it.
                if i + 1 < n and dax[i + 1] == '"':
                    out.append("  ")
                    i += 2
                    continue
                out.append(" ")
                i += 1
                break
            out.append(" ")
            i += 1
    return "".join(out)


def _match_paren(text, start):
    """Index of the ``)`` matching the ``(`` at ``start``, or -1 when unbalanced."""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top_level(body):
    """Split an argument list on commas that sit at bracket depth 0."""
    args, depth, cur = [], 0, []
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    args.append("".join(cur))
    return args


def _calls(dax, names):
    """Yield ``(FN, body)`` for every call to a function in ``names`` (case-insensitive)."""
    wanted = {n.upper() for n in names}
    for m in _CALL_RE.finditer(dax):
        fn = m.group(1).upper()
        if fn not in wanted:
            continue
        open_i = m.end() - 1
        close = _match_paren(dax, open_i)
        if close == -1:  # unbalanced -- not our problem to diagnose, stay silent
            continue
        yield fn, dax[open_i + 1:close]


def _bare_refs(expr):
    """Set of unqualified ``[Name]`` (measure) references in ``expr``."""
    return {m.group(1).strip() for m in _BARE_REF_RE.finditer(expr)}


def _has_top_level_comparison(arg):
    """True when ``arg`` contains a comparison operator at bracket depth 0.

    This is what separates a COMPACT boolean filter (``'T'[C] = [M]``, the illegal form) from a
    table-valued filter argument (``FILTER('T', 'T'[C] = [M])``, which is legal) -- in the latter the
    comparison lives inside the FILTER call's parens, so it is never at depth 0 of the argument.
    """
    depth, i = 0, 0
    while i < len(arg):
        ch = arg[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            for op in _COMPARISONS:
                if arg.startswith(op, i):
                    return True
        i += 1
    return False


def _is_ratio(dax):
    """True when an expression's value is a quotient (so re-aggregating it loses the weighting)."""
    if not dax:
        return False
    body = _blank_strings(dax)
    return "DIVIDE(" in body.upper().replace(" ", "") or "/" in body


def _is_distinct_count(dax):
    if not dax:
        return False
    return "DISTINCTCOUNT" in _blank_strings(dax).upper()


def _rows(measure_report, calc_column_report):
    """Normalise the measure + calc-column reports into ``(name, table, role, dax)`` tuples.

    Only rows carrying a live DAX expression are returned. A stub measure is an inert ``= 0`` that
    preserves its Tableau formula for a human -- it is honest by construction and has no expression
    to lint, so flagging it would be noise about a decision the pipeline already disclosed.
    """
    out = []
    for row in measure_report or []:
        if not isinstance(row, dict):
            continue
        dax = row.get("dax")
        name = row.get("measure")
        if dax and name:
            out.append((str(name), (row.get("source") or {}).get("model_table") or "_Measures",
                        "measure", str(dax)))
    for row in calc_column_report or []:
        if not isinstance(row, dict):
            continue
        dax = row.get("dax")
        name = row.get("column")
        if dax and name:
            out.append((str(name), row.get("table") or "", "column", str(dax)))
    return out


def _manifest_columns(model_manifest):
    """``{(table_lower, name_lower)}`` for every column the manifest declares."""
    cols = set()
    for c in ((model_manifest or {}).get("columns") or []):
        if not isinstance(c, dict):
            continue
        t, n = c.get("model_table"), c.get("model_name")
        if t and n:
            cols.add((str(t).lower(), str(n).lower()))
    return cols


def lint_model_semantics(measure_report, calc_column_report=None, model_manifest=None):
    """Lint the generated DAX for defects no well-formedness check can see.

    Returns ``{"ok", "checked", "clean", "blocking", "advisory", "counts", "findings"}``.
    ``ok`` is False ONLY when a blocking finding exists -- an advisory finding is a number a human
    should look at, never a reason to fail a build. Read-only; never raises.
    """
    findings = []
    try:
        rows = _rows(measure_report, calc_column_report)
    except Exception:  # pragma: no cover - defensive; a malformed report must never break a build
        rows = []

    # Expression by measure name -- lets a detector follow a reference one hop, which is where the
    # aggregation-semantics defects actually live (nobody writes SUMX(T, DISTINCTCOUNT(...)) by hand;
    # they write SUMX(T, [Distinct Customers]) and the mistake hides behind the name).
    dax_by_measure = {n.lower(): d for n, _t, role, d in rows if role == "measure"}
    measure_names = set(dax_by_measure)
    columns = _manifest_columns(model_manifest)

    def _add(obj, table, role, kind, severity, detail):
        findings.append({"object": obj, "table": table, "role": role,
                         "kind": kind, "severity": severity, "detail": detail})

    seen_measures = {}
    for name, table, role, dax in rows:
        try:
            body = _blank_strings(dax)

            # -- BLOCKING: a measure inside a CALCULATE compact boolean filter ------------------
            for _fn, args_src in _calls(body, _CALCULATE_FNS):
                for arg in _split_top_level(args_src)[1:]:
                    if not arg.strip() or not _has_top_level_comparison(arg):
                        continue
                    hits = sorted(r for r in _bare_refs(arg) if r.lower() in measure_names)
                    if hits:
                        _add(name, table, role, "compact_filter_with_measure", BLOCKING,
                             "CALCULATE filter compares against measure "
                             + ", ".join("[%s]" % h for h in hits)
                             + " -- DAX rejects a measure in a compact boolean filter; hoist it to a VAR")

            # -- BLOCKING: name collisions Tabular only reports on commit -----------------------
            if role == "measure":
                key = name.lower()
                if key in seen_measures:
                    _add(name, table, role, "duplicate_measure_name", BLOCKING,
                         "a measure named '%s' was already emitted -- measure names must be unique "
                         "model-wide" % name)
                else:
                    seen_measures[key] = table
                if (str(table).lower(), key) in columns:
                    _add(name, table, role, "measure_shadows_column", BLOCKING,
                         "a column named '%s' already exists in table '%s' -- measures and columns "
                         "share one namespace" % (name, table))

            # -- ADVISORY: re-aggregating something that is not additive ------------------------
            for fn, agg_body in _calls(body, _ADDITIVE_FNS):
                if _is_distinct_count(agg_body):
                    _add(name, table, role, "countd_reaggregated", ADVISORY,
                         "%s(...) wraps a DISTINCTCOUNT -- a distinct count is not additive, so "
                         "aggregating it across groups double-counts shared members" % fn)
                    continue
                for ref in sorted(_bare_refs(agg_body)):
                    inner = dax_by_measure.get(ref.lower())
                    if inner is None or ref.lower() == name.lower():
                        continue
                    if _is_distinct_count(inner):
                        _add(name, table, role, "countd_reaggregated", ADVISORY,
                             "%s(...) aggregates [%s], whose expression is a distinct count -- a "
                             "distinct count is not additive" % (fn, ref))
                    elif _is_ratio(inner):
                        _add(name, table, role, "ratio_reaggregated", ADVISORY,
                             "%s(...) aggregates [%s], whose expression is a ratio -- the average of "
                             "averages drops the denominator weighting" % (fn, ref))
        except Exception:  # pragma: no cover - one bad expression must never break the lint
            continue

    counts = {}
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    blocking = sum(1 for f in findings if f["severity"] == BLOCKING)
    flagged = {f["object"] for f in findings}
    return {
        "ok": blocking == 0,
        "checked": len(rows),
        "clean": len(rows) - len(flagged),
        "blocking": blocking,
        "advisory": len(findings) - blocking,
        "counts": counts,
        "findings": findings,
    }
