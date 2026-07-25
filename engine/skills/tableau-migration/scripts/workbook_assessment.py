"""Pre-migration assessment of a Tableau workbook: surface area, complexity, risk.

Stdlib-only and pure. Given a workbook's ``.twb`` XML text, :func:`assess_workbook`
returns a machine-readable, deterministic assessment the migration report surfaces so a
first-time customer can *size* the estate before committing:

* **surface_area** -- raw counts (worksheets, dashboards, calculated fields, LOD
  expressions, table calcs, parameters, defined fields). The rebuild/translation surface.
* **complexity** -- a 1-5 score + Simple/Moderate/Complex bucket, using the documented
  rubric in ``docs/assessment-methodology.md`` (calc profile + structural modifiers).
* **difficulty** -- a transparent 0-100 Power BI migration-difficulty score + band, a
  weighted count of the signals that actually drive manual DAX work (LOD, table calcs,
  nested logic, parameters driving calcs, custom SQL). The formula is published in the
  report, so the number is *defensible arithmetic*, never an opaque "AI" guess.
* **unused_fields** -- fields defined but not referenced by any worksheet/dashboard/calc.
  Deliberately **conservative** (a field counts as used the moment its id appears anywhere
  outside its own definition) so the list is a *"verify before removing"* hint, not a
  deletion instruction -- a false "unused" is worse than a missed one.
* **orphaned_worksheets** -- worksheets placed on no dashboard (candidate dead content).
* **components** -- per calculated field (and custom SQL) a Low/Medium/High migration
  *impact*, so the effort tail is visible line by line.

Faithful-or-silent: if the XML cannot be parsed the function returns an empty-but-valid
assessment rather than guessing, and every count is derived from the workbook's own
grammar -- nothing is inferred or inflated.
"""

import re
import xml.etree.ElementTree as ET

# --- calc-type signatures (case-insensitive, word-boundary where it matters) ---------------
# LOD expression: ``{ FIXED [Region] : SUM([Sales]) }`` / INCLUDE / EXCLUDE.
_LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)
_LOD_BROAD_RE = re.compile(r"\b(INCLUDE|EXCLUDE)\b", re.IGNORECASE)  # heavier LOD variants
# Table-calculation functions: they address the viz, not the row -- the manual-rebuild tail.
_TABLE_CALC_RE = re.compile(
    r"\b(WINDOW_\w+|RUNNING_\w+|INDEX|RANK|RANK_DENSE|RANK_MODIFIED|RANK_PERCENTILE|"
    r"RANK_UNIQUE|LOOKUP|TOTAL|FIRST|LAST|SIZE|PREVIOUS_VALUE)\s*\(",
    re.IGNORECASE,
)
# Conditional / nested row logic: raises effort above a plain aggregation.
_CONDITIONAL_RE = re.compile(r"\b(IF|IIF|CASE)\b", re.IGNORECASE)

# Published so the report can show the customer exactly how the difficulty number is built --
# transparent arithmetic over the workbook's own grammar, never an opaque score.
_DIFFICULTY_FORMULA = (
    "score = min(100, 3*LOD + 3*table_calcs + nested_calcs + parameter_driven_calcs "
    "+ 8*custom_sql + 0.2*calculations)"
)


def _text_of(elem):
    """Serialize an element subtree back to text for conservative substring reference checks."""
    try:
        return ET.tostring(elem, encoding="unicode")
    except Exception:  # pragma: no cover - tostring on a valid parsed tree does not fail
        return ""


def _field_ids(name):
    """The tokens by which a field is referenced elsewhere: its bracketed id and bare name.

    Tableau stores ids as ``[Sales]``; references appear as ``[Sales]`` (column-instance,
    formula) so the bracketed form is the reliable key. We also keep the debracketed form as
    a weaker signal. Empty/None yields no tokens (never matches -> never a false "used").
    """
    if not name:
        return ()
    bare = name.strip()
    inner = bare[1:-1] if bare.startswith("[") and bare.endswith("]") else bare
    toks = {bare}
    if inner:
        toks.add("[" + inner + "]")
    return tuple(t for t in toks if t)


def _bucket(score):
    if score <= 2:
        return "Simple"
    if score == 3:
        return "Moderate"
    return "Complex"


def _difficulty_band(score):
    if score < 20:
        return "Low"
    if score < 50:
        return "Moderate"
    if score < 75:
        return "High"
    return "Very High"


def assess_workbook(xml_text):
    """Return the deterministic assessment dict for one workbook's ``.twb`` XML text.

    Never raises: unparseable/empty input yields the empty-but-valid assessment so a report
    run is never gated on a scan hiccup.
    """
    if not xml_text or not str(xml_text).strip():
        return _clone_empty()
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return _clone_empty()

    # --- worksheets & dashboards ----------------------------------------------------------
    ws_parent = root.find("worksheets")
    worksheets = []
    if ws_parent is not None:
        worksheets = [w.get("name") for w in ws_parent.findall("worksheet") if w.get("name")]

    db_parent = root.find("dashboards")
    dashboards = []
    zone_names = set()
    if db_parent is not None:
        for d in db_parent.findall("dashboard"):
            if d.get("name"):
                dashboards.append(d.get("name"))
            # Zones nest arbitrarily (containers within containers); a worksheet is placed via
            # a zone whose @name is the worksheet name. Walk every descendant zone.
            for z in d.iter("zone"):
                zn = z.get("name")
                if zn:
                    zone_names.add(zn)

    orphaned = [w for w in worksheets if w not in zone_names]

    # --- reference surface (worksheets + dashboards subtree text + calc formulas) ---------
    # A field counts as "used" if its id appears anywhere in the viz grammar or in any calc
    # formula. This is intentionally generous so the "unused" list stays conservative.
    ref_text_parts = []
    if ws_parent is not None:
        ref_text_parts.append(_text_of(ws_parent))
    if db_parent is not None:
        ref_text_parts.append(_text_of(db_parent))

    # --- datasources: defined fields, calcs, parameters, custom SQL -----------------------
    calc_formulas = []          # (caption_or_name, formula)
    defined_fields = []         # (display_name, id_name, kind)  kind in {column, calculation}
    parameter_ids = set()
    custom_sql = False
    seen_field_keys = set()

    ds_parent = root.find("datasources")
    datasources = ds_parent.findall("datasource") if ds_parent is not None else []
    for ds in datasources:
        is_param_ds = (ds.get("name") == "Parameters")
        # Custom SQL: an inline text relation instead of a plain table.
        for rel in ds.iter("relation"):
            if (rel.get("type") or "").lower() == "text":
                custom_sql = True
        # Physical columns (metadata-records) -- the base schema fields.
        for mr in ds.iter("metadata-record"):
            if (mr.get("class") or "") != "column":
                continue
            ln = mr.findtext("local-name")
            if ln:
                key = ("column", ln)
                if key not in seen_field_keys:
                    seen_field_keys.add(key)
                    defined_fields.append((ln.strip("[]"), ln, "column"))
        # Author-defined columns and calculated fields.
        for col in ds.findall("column"):
            name = col.get("name")
            caption = col.get("caption") or (name or "").strip("[]")
            if is_param_ds:
                # A parameter carries a <calculation> for its current value; it is a control, not a
                # model calc -- count it as a parameter, never as a calculated field.
                if name:
                    parameter_ids.add(name)
                continue
            calc = col.find("calculation")
            if calc is not None and calc.get("formula") is not None:
                calc_formulas.append((caption, calc.get("formula")))
                kind = "calculation"
            else:
                kind = "column"
            if name:
                key = (kind, name)
                if key not in seen_field_keys:
                    seen_field_keys.add(key)
                    defined_fields.append((caption, name, kind))

    # Some workbooks define calculated fields only inside a worksheet's
    # ``<datasource-dependencies>`` (not at the datasource level). Fold those in too, deduped by
    # id so a calc used on three worksheets is still counted once.
    if ws_parent is not None:
        for col in ws_parent.iter("column"):
            calc = col.find("calculation")
            if calc is None or calc.get("formula") is None:
                continue
            name = col.get("name")
            if not name:
                continue
            key = ("calculation", name)
            if key in seen_field_keys:
                continue
            seen_field_keys.add(key)
            caption = col.get("caption") or name.strip("[]")
            calc_formulas.append((caption, calc.get("formula")))
            defined_fields.append((caption, name, "calculation"))

    ref_text = "\n".join(ref_text_parts + [f for _, f in calc_formulas])

    # --- calc-type profile ----------------------------------------------------------------
    lod_count = 0
    heavy_lod_count = 0
    table_calc_count = 0
    nested_count = 0
    param_driven = 0
    components = []
    for caption, formula in calc_formulas:
        f = formula or ""
        is_lod = bool(_LOD_RE.search(f))
        is_heavy_lod = is_lod and bool(_LOD_BROAD_RE.search(f))
        is_tc = bool(_TABLE_CALC_RE.search(f))
        is_cond = bool(_CONDITIONAL_RE.search(f))
        uses_param = any(pid in f for pid in parameter_ids) if parameter_ids else False
        if is_lod:
            lod_count += 1
            if is_heavy_lod:
                heavy_lod_count += 1
        if is_tc:
            table_calc_count += 1
        if is_cond and not is_lod and not is_tc:
            nested_count += 1
        if uses_param:
            param_driven += 1

        if is_tc or is_heavy_lod:
            impact = "High"
        elif is_lod or uses_param or is_cond:
            impact = "Medium"
        else:
            impact = "Low"
        kind = (
            "table calc" if is_tc
            else "LOD" if is_lod
            else "conditional" if is_cond
            else "aggregation"
        )
        components.append({"name": caption, "kind": kind, "impact": impact})

    if custom_sql:
        components.append({"name": "Custom SQL relation", "kind": "custom sql", "impact": "High"})

    # --- unused fields (conservative) -----------------------------------------------------
    unused = []
    for display, idname, kind in defined_fields:
        toks = _field_ids(idname)
        if not toks:
            continue
        # A field is used if any of its tokens appears in the viz grammar or any calc formula
        # (excluding its own definition, which lives in <datasources>, not in ref_text).
        if any(tok in ref_text for tok in toks):
            continue
        unused.append({"field": display, "id": idname, "kind": kind})

    # --- surface area ---------------------------------------------------------------------
    surface = {
        "worksheets": len(worksheets),
        "dashboards": len(dashboards),
        "calculations": len(calc_formulas),
        "lod_expressions": lod_count,
        "table_calcs": table_calc_count,
        "parameters": len(parameter_ids),
        "fields_defined": len(defined_fields),
    }

    # --- complexity 1-5 (documented rubric) -----------------------------------------------
    score = 1
    if lod_count or param_driven:
        score = max(score, 3)          # FIXED LOD or parameters
    if nested_count:
        score = max(score, 2)          # row-level IF/CASE
    if heavy_lod_count or table_calc_count >= 2:
        score = max(score, 4)          # INCLUDE/EXCLUDE LOD or multiple table calcs
    if heavy_lod_count and table_calc_count and custom_sql:
        score = 5                      # heavy nested LOD + table calcs + custom SQL
    if custom_sql:
        score += 1
    if param_driven:
        score += 1
    score = max(1, min(5, score))

    # --- difficulty 0-100 (transparent weighted count) ------------------------------------
    raw = (
        3 * lod_count
        + 3 * table_calc_count
        + 1 * nested_count
        + 1 * param_driven
        + 8 * (1 if custom_sql else 0)
        + 0.2 * len(calc_formulas)
    )
    diff_score = round(min(100.0, raw), 1)

    return {
        "surface_area": surface,
        "complexity": {"score": score, "bucket": _bucket(score)},
        "difficulty": {
            "score": diff_score,
            "band": _difficulty_band(diff_score),
            "drivers": {
                "lod_expressions": lod_count,
                "table_calcs": table_calc_count,
                "nested_calcs": nested_count,
                "parameter_driven_calcs": param_driven,
                "custom_sql": 1 if custom_sql else 0,
                "calculations": len(calc_formulas),
            },
            "formula": _DIFFICULTY_FORMULA,
        },
        "unused_fields": unused,
        "orphaned_worksheets": orphaned,
        "components": components,
    }


def _clone_empty():
    """A fresh empty-but-valid assessment (callers may mutate their result)."""
    return {
        "surface_area": {
            "worksheets": 0,
            "dashboards": 0,
            "calculations": 0,
            "lod_expressions": 0,
            "table_calcs": 0,
            "parameters": 0,
            "fields_defined": 0,
        },
        "complexity": {"score": 1, "bucket": "Simple"},
        "difficulty": {
            "score": 0.0,
            "band": "Low",
            "drivers": {
                "lod_expressions": 0,
                "table_calcs": 0,
                "nested_calcs": 0,
                "parameter_driven_calcs": 0,
                "custom_sql": 0,
                "calculations": 0,
            },
            "formula": _DIFFICULTY_FORMULA,
        },
        "unused_fields": [],
        "orphaned_worksheets": [],
        "components": [],
    }


def aggregate_assessment(wb_details):
    """Roll per-workbook ``assessment`` blocks up into the estate-level assessment.

    Reads ``w["assessment"]`` (attached by the estate runner) off each workbook detail;
    workbooks without one contribute nothing. Sums the surface area, carries the *highest*
    difficulty as the estate headline (the estate is as hard as its hardest workbook), and
    lists a compact per-workbook row for the report table. Pure; never raises.
    """
    surface_keys = (
        "worksheets", "dashboards", "calculations",
        "lod_expressions", "table_calcs", "parameters", "fields_defined",
    )
    totals = {k: 0 for k in surface_keys}
    unused_total = 0
    orphaned_total = 0
    max_diff = 0.0
    max_complexity = 1
    by_workbook = []

    for w in wb_details:
        a = (w or {}).get("assessment")
        if not isinstance(a, dict) or not a:
            continue
        surf = a.get("surface_area") or {}
        for k in surface_keys:
            totals[k] += int(surf.get(k) or 0)
        unused_total += len(a.get("unused_fields") or [])
        orphaned_total += len(a.get("orphaned_worksheets") or [])
        diff = (a.get("difficulty") or {}).get("score") or 0.0
        cplx = (a.get("complexity") or {}).get("score") or 1
        max_diff = max(max_diff, float(diff))
        max_complexity = max(max_complexity, int(cplx))
        by_workbook.append({
            "name": w.get("name") or w.get("source_id") or "(workbook)",
            "complexity": a.get("complexity") or {},
            "difficulty": a.get("difficulty") or {},
            "unused_fields": len(a.get("unused_fields") or []),
            "orphaned_worksheets": len(a.get("orphaned_worksheets") or []),
        })

    return {
        "workbooks": len(by_workbook),
        "surface_area": totals,
        "difficulty": {
            "max_score": round(max_diff, 1),
            "band": _difficulty_band(max_diff),
            "formula": _DIFFICULTY_FORMULA,
        },
        "complexity": {"max_score": max_complexity, "bucket": _bucket(max_complexity)},
        "unused_fields_total": unused_total,
        "orphaned_worksheets_total": orphaned_total,
        "by_workbook": by_workbook,
    }
