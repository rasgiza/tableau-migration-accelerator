"""Unit tests for :mod:`migration_report_html` -- the offline, self-contained HTML migration
report rendered from a ``report.json`` dict.

The module is pure and stdlib-only, so these tests feed it small hand-built report dicts and assert
on the rendered HTML string: that it is self-contained (no ``<script>``, no external URL), that
untrusted report content is HTML-escaped (the security contract), and that the key sections and
faithful status rendering are present.
"""
import migration_report_html as mr


def _minimal_report(**over):
    report = {
        "tool": "migrate_estate",
        "generated_at": "2026-07-18T00:00:00Z",
        "source": {"kind": "local", "root": "/estate"},
        "summary": {
            "datasources_migrated": 2, "datasources_total": 2,
            "datasources_partial": 0, "datasources_fallback": 0,
            "measures_translated": 9, "measures_total": 17, "measures_stubbed": 8,
            "calc_columns_translated": 2, "calc_columns_total": 4, "calc_columns_stubbed": 2,
            "workbook_calcs_translated": 19, "workbook_calcs_total": 67,
            "workbook_calcs_needs_review": 48,
            "visuals_rebuilt": 43, "visuals_warned": 43,
            "workbooks_viz_built": 7, "workbooks_total": 7, "workbooks_viz_error": 0,
        },
        "definition_of_done": {
            "applicable": True, "status": "failed",
            "reports_bound": 2, "reports_warned": 1, "reports_failed": 4,
            "workbooks_total": 7,
            "workbooks": [
                {"workbook": "TableCalcShowcase", "status": "pass",
                 "bound_model": "Sales Flat", "pbip_folder": "pbip/x/x.pbip", "reason": ""},
            ],
        },
        "datasources": [
            {"name": "ClaimsFlat", "status": "migrated", "connector": "sqlserver",
             "storage_decision": {"mode": "DirectQuery", "rationale": "Live sqlserver -> DirectQuery.",
                                  "manual_followups": ["Configure connection credentials in Fabric."]},
             "lineage": [
                 {"calc": "Net Collection Rate", "role": "measure",
                  "formula": "SUM([Paid Amount]) / SUM([Allowed Amount])",
                  "references": ["Allowed Amount", "Paid Amount"],
                  "depends_on_calcs": [], "parameters": []},
             ],
             "manual_followups": ["Configure connection credentials in Fabric."]},
        ],
        "fallbacks": [],
    }
    report.update(over)
    return report


def test_document_is_self_contained_and_scriptless():
    h = mr.render_report_html(_minimal_report())
    assert h.startswith("<!doctype html>")
    assert "<script" not in h.lower()
    assert "http://" not in h and "https://" not in h  # no CDN / external asset


def test_sections_and_faithful_status_present():
    h = mr.render_report_html(_minimal_report())
    for token in ("Definition of done", "Coverage", "Workbook sign-off",
                  "Datasource lineage", "Net Collection Rate", "ClaimsFlat"):
        assert token in h
    # A failed definition-of-done is rendered faithfully, not softened.
    assert "banner bad" in h


def test_untrusted_content_is_html_escaped():
    # A datasource name / formula containing markup must never inject into the output.
    rpt = _minimal_report()
    rpt["datasources"][0]["name"] = "<img src=x onerror=alert(1)>"
    rpt["datasources"][0]["lineage"][0]["formula"] = "IF [a] < [b] THEN '<b>x</b>' END"
    h = mr.render_report_html(rpt)
    assert "<img src=x" not in h
    assert "&lt;img src=x onerror=alert(1)&gt;" in h
    assert "&lt;b&gt;x&lt;/b&gt;" in h


def test_pct_guards_divide_by_zero():
    assert mr._pct(0, 0) == "--"
    assert mr._pct(1, 4) == "25%"
    assert mr._pct(None, 10) == "--"


def test_dod_not_applicable_renders_muted_banner():
    rpt = _minimal_report(definition_of_done={"applicable": False})
    h = mr.render_report_html(rpt)
    assert "not applicable" in h
    assert "banner muted" in h


def test_empty_report_does_not_crash():
    h = mr.render_report_html({})
    assert h.startswith("<!doctype html>")
    assert "Coverage" in h


def _needs_review_workbook():
    return {
        "name": "Superstore Overview Dashboard",
        "model_translation_handoff": {
            "summary": {"translated": 34, "total": 69, "coverage_pct": 49.3, "needs_review": 35},
            "needs_review": [
                {
                    "category": "model_object_parameter",
                    "category_guidance": "This calc is driven by a Tableau parameter ...",
                    "fallback_reason": "parameter reference [Parameters].[Parameter 1] (unmodeled)",
                    "formula": "[Parameters].[Parameter 1]-1",
                    "name": "Prev Year (BANs)", "role": "dimension",
                },
                {
                    "category": "missing_addressing_intent",
                    "fallback_reason": "unsupported function INDEX",
                    "name": "Row Number", "role": "measure",
                },
            ],
        },
    }


def test_needs_review_section_lists_each_flagged_calc():
    rpt = _minimal_report(workbooks=[_needs_review_workbook()])
    h = mr.render_report_html(rpt)
    # section heading + per-workbook block + human category labels
    assert "Needs review" in h
    assert "Superstore Overview Dashboard" in h
    assert "Parameter-driven (Power BI model object)" in h
    assert "Table calc" in h  # missing_addressing_intent label
    # each calc name, its formula and its reason are surfaced
    assert "Prev Year (BANs)" in h
    assert "[Parameters].[Parameter 1]-1".replace("[", "&#x5B;") in h or "Parameter 1]-1" in h
    assert "unsupported function INDEX" in h
    # expandable, still script-free
    assert "<details" in h
    assert "<script" not in h.lower()


def test_needs_review_section_absent_when_no_workbooks():
    h = mr.render_report_html(_minimal_report())
    assert "calculation worklist" not in h


def test_needs_review_formula_is_html_escaped():
    wb = _needs_review_workbook()
    wb["model_translation_handoff"]["needs_review"][0]["formula"] = "IF [a] < '<b>x</b>' THEN 1 END"
    h = mr.render_report_html(_minimal_report(workbooks=[wb]))
    assert "<b>x</b>" not in h
    assert "&lt;b&gt;x&lt;/b&gt;" in h
