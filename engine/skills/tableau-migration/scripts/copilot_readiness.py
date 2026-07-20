"""Read-only Copilot / Power BI Q&A readiness scorecard for a migrated estate.

A semantic model can carry perfectly correct DAX and still give Copilot and Q&A *weak* answers when
its fields have no descriptions and no synonyms, or when it exposes inert stub measures that quietly
return 0. This module GRADES the model the migration just produced -- purely from the estate
``report`` dict, never re-parsing TMDL and never mutating its input -- so a user can see at a glance
whether the output is grounded enough for AI/Copilot and exactly what to fix.

``score_copilot_readiness(report)`` returns::

    {
      "enabled": bool,            # was Copilot-readiness enrichment on for every migrated datasource
      "overall": "ready" | "ready_with_warnings" | "not_ready",
      "totals": {"passed": int, "warned": int, "failed": int, "checks": int},
      "checks": [ {"id", "label", "status", "detail", "metric"}, ... ],
    }

Every check ``status`` is one of ``pass`` / ``warn`` / ``fail`` / ``na`` (``na`` = not applicable,
e.g. a workbook with zero measures). The scorecard is deterministic, stdlib-only, and defensive:
every field access tolerates a missing/partial report so it can never raise inside a migration run.
"""

_MIGRATED = ("migrated", "migrated_with_followups")


def _pct(part, whole):
    """part/whole as a rounded percentage, or 0.0 when whole is 0 (never divides by zero)."""
    try:
        whole = float(whole)
        if whole <= 0:
            return 0.0
        return round(100.0 * float(part) / whole, 1)
    except (TypeError, ValueError):
        return 0.0


def _check(cid, label, status, detail, metric=None):
    return {"id": cid, "label": label, "status": status, "detail": detail, "metric": metric}


def _migrated_datasources(report):
    return [d for d in (report.get("datasources") or [])
            if isinstance(d, dict) and d.get("status") in _MIGRATED]


def score_copilot_readiness(report):
    """Grade an estate ``report`` dict for Copilot / Q&A readiness. Pure and defensive."""
    report = report if isinstance(report, dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    migrated = _migrated_datasources(report)
    checks = []

    # 1. Migration completeness -- a datasource that fell back or errored has NO model for Copilot.
    total_ds = int(summary.get("datasources_total") or len(report.get("datasources") or []))
    n_migrated = int(summary.get("datasources_migrated") or len(migrated))
    n_fallback = int(summary.get("datasources_fallback") or 0)
    n_error = int(summary.get("datasources_error") or 0)
    if total_ds == 0:
        checks.append(_check("datasources", "Datasources migrated", "na",
                             "No datasources were in scope."))
    elif n_error:
        checks.append(_check("datasources", "Datasources migrated", "fail",
                             f"{n_error} of {total_ds} datasource(s) errored -- no model produced "
                             f"for Copilot until resolved.", _pct(n_migrated, total_ds)))
    elif n_fallback:
        checks.append(_check("datasources", "Datasources migrated", "warn",
                             f"{n_fallback} of {total_ds} datasource(s) hit a storage-decision "
                             f"fallback and produced no model.", _pct(n_migrated, total_ds)))
    else:
        checks.append(_check("datasources", "Datasources migrated", "pass",
                             f"All {total_ds} datasource(s) produced a semantic model.",
                             _pct(n_migrated, total_ds)))

    # 2. Measure coverage -- an untranslated stub measure returns an inert 0; Copilot will answer
    #    from it as if it were real. 100% translated is the only clean bar.
    m_total = int(summary.get("measures_total") or 0)
    m_trans = int(summary.get("measures_translated") or 0)
    m_stub = int(summary.get("measures_stubbed") or 0)
    if m_total == 0:
        checks.append(_check("measures", "Measures translated (no inert stubs)", "na",
                             "No measures were migrated."))
    elif m_stub == 0:
        checks.append(_check("measures", "Measures translated (no inert stubs)", "pass",
                             f"All {m_total} measure(s) carry real DAX.", _pct(m_trans, m_total)))
    elif _pct(m_trans, m_total) >= 80.0:
        checks.append(_check("measures", "Measures translated (no inert stubs)", "warn",
                             f"{m_stub} of {m_total} measure(s) are inert stubs needing review "
                             f"before Copilot can trust them.", _pct(m_trans, m_total)))
    else:
        checks.append(_check("measures", "Measures translated (no inert stubs)", "fail",
                             f"{m_stub} of {m_total} measure(s) are inert stubs -- Copilot would "
                             f"answer from placeholder 0s.", _pct(m_trans, m_total)))

    # 3. Calculated-column coverage -- same inert-stub risk on the column side.
    c_total = int(summary.get("calc_columns_total") or 0)
    c_trans = int(summary.get("calc_columns_translated") or 0)
    c_stub = int(summary.get("calc_columns_stubbed") or 0)
    if c_total == 0:
        checks.append(_check("calc_columns", "Calculated columns translated", "na",
                             "No calculated columns were migrated."))
    elif c_stub == 0:
        checks.append(_check("calc_columns", "Calculated columns translated", "pass",
                             f"All {c_total} calculated column(s) carry real DAX.",
                             _pct(c_trans, c_total)))
    else:
        status = "warn" if _pct(c_trans, c_total) >= 80.0 else "fail"
        checks.append(_check("calc_columns", "Calculated columns translated", status,
                             f"{c_stub} of {c_total} calculated column(s) are stubs needing review.",
                             _pct(c_trans, c_total)))

    # 4. Field descriptions -- Copilot grounds answers on an object's description. Enrichment writes
    #    an honest one-line description onto every emitted measure, BUT it is only a PROVENANCE
    #    scaffold (translated => "migrated from a Tableau calc"; stub => a needs-review flag), NOT a
    #    business definition of what the field means. Tableau workbooks rarely carry real field
    #    descriptions, so genuine business meaning is a human add -- surfaced under ``guidance`` below.
    enabled_flags = [bool(d.get("copilot_ready")) for d in migrated]
    all_enabled = bool(enabled_flags) and all(enabled_flags)
    any_enabled = any(enabled_flags)
    if not migrated:
        checks.append(_check("descriptions", "Field descriptions for Copilot grounding", "na",
                             "No migrated datasources to describe."))
    elif all_enabled:
        checks.append(_check("descriptions", "Field descriptions for Copilot grounding", "pass",
                             "Every measure carries a provenance description scaffold; add business "
                             "meaning (see guidance) for the strongest grounding."))
    elif any_enabled:
        checks.append(_check("descriptions", "Field descriptions for Copilot grounding", "warn",
                             "Description scaffolds were emitted for only some datasources."))
    else:
        checks.append(_check("descriptions", "Field descriptions for Copilot grounding", "warn",
                             "Descriptions are OFF (--no-copilot-ready) -- Copilot has less to "
                             "ground on."))

    # 5. Q&A synonyms -- a ``cultureInfo`` linguistic layer maps a user's words onto model fields.
    #    Harvested from Tableau captions that differ from their model column name; a model whose
    #    names already match its captions legitimately needs none.
    term_total = 0
    ds_with_syn = 0
    for d in migrated:
        ling = d.get("linguistic")
        if isinstance(ling, dict):
            t = int(ling.get("terms") or 0)
            if t:
                term_total += t
                ds_with_syn += 1
    if not migrated:
        checks.append(_check("synonyms", "Q&A synonyms (linguistic culture)", "na",
                             "No migrated datasources."))
    elif not any_enabled:
        checks.append(_check("synonyms", "Q&A synonyms (linguistic culture)", "warn",
                             "Synonyms are OFF (--no-copilot-ready)."))
    elif term_total:
        checks.append(_check("synonyms", "Q&A synonyms (linguistic culture)", "pass",
                             f"{term_total} synonym term(s) harvested across {ds_with_syn} "
                             f"datasource(s)."))
    else:
        checks.append(_check("synonyms", "Q&A synonyms (linguistic culture)", "pass",
                             "Enrichment on; no synonyms needed (model names already match "
                             "Tableau captions)."))

    passed = sum(1 for c in checks if c["status"] == "pass")
    warned = sum(1 for c in checks if c["status"] == "warn")
    failed = sum(1 for c in checks if c["status"] == "fail")
    if failed:
        overall = "not_ready"
    elif warned:
        overall = "ready_with_warnings"
    else:
        overall = "ready"
    return {
        "enabled": all_enabled,
        "overall": overall,
        "totals": {"passed": passed, "warned": warned, "failed": failed, "checks": len(checks)},
        "checks": checks,
        "guidance": _readiness_guidance(migrated, all_enabled, any_enabled, m_stub, c_stub),
    }


def _readiness_guidance(migrated, all_enabled, any_enabled, m_stub, c_stub):
    """Honest, human next-steps for turning a Copilot-READY scaffold into a Copilot-GROUNDED model.

    The migration produces the scaffold (provenance descriptions, harvested synonyms, typed model);
    it cannot invent business meaning the Tableau source never carried. These steps name exactly what
    a human must add -- so the guidance is grounded in what the tool did and did NOT do, never
    over-claiming that a migrated model is automatically AI-ready.
    """
    tips = []
    if migrated:
        tips.append(
            "You do NOT have to describe every field. The scaffold is safe and openable as-is; enrich "
            "only the fields users actually ask about -- the VISIBLE measures and the columns people "
            "slice by (region, product, service line). That is usually a few dozen fields, not thousands.")
        tips.append(
            "Add business descriptions where it counts: the auto-generated measure descriptions record "
            "PROVENANCE (where a field came from), not what it MEANS. Tableau workbooks rarely carry "
            "field descriptions, so replace them with a short plain-language definition on your key "
            "measures and slicer columns -- the single biggest lever on Copilot / Q&A answer quality.")
        tips.append(
            "HIDE, don't describe, the plumbing: surrogate keys, ID columns, and technical/staging "
            "fields need no description at all -- just hide them so Copilot ignores them. This removes "
            "most of the model from the 'to-do' list before you write a single description.")
        tips.append(
            "Curate Q&A synonyms: the harvested synonyms come only from Tableau captions that differ "
            "from the model column name. Add the words your users actually say -- abbreviations, "
            "business jargon, and alternate names -- so Copilot maps their vocabulary to the model.")
        tips.append(
            "Prep the data for AI: give the fields you kept friendly names, mark the date table, and "
            "verify each relationship's cardinality and cross-filter direction. An ambiguous model "
            "produces ambiguous answers, however good the DAX.")
        tips.append(
            "This is a one-time curation, not a per-migration tax: descriptions and synonyms you add "
            "live in the published semantic model as normal stewardship -- re-running the migration "
            "does not redo this work unless you overwrite the model file.")
    if m_stub or c_stub:
        tips.append(
            "Resolve the 'needs manual review' stubs before exposing them to Copilot -- an inert stub "
            "returns 0, and Copilot will answer from it as if it were real.")
    if migrated and not any_enabled:
        tips.append(
            "Copilot-readiness enrichment is OFF (--no-copilot-ready): no descriptions or synonyms were "
            "written. Re-run without that flag to emit the grounding scaffold.")
    return tips
