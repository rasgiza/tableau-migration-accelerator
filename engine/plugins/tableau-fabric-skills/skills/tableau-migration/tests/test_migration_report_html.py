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


def test_kpi_separators_are_not_double_escaped():
    # KPI sub-text goes through _esc, so an HTML entity there would be double-escaped and render as
    # literal text (e.g. "&middot;"). The double-escaped form must never appear; the KPI separators
    # must be the real middot character. (Raw &middot; in direct section markup is fine -- it is not
    # passed through _esc and renders correctly.)
    h = mr.render_report_html(_minimal_report())
    assert "&amp;middot;" not in h
    assert "\u00b7" in h  # the real "·" separator is present in the KPI sub-text


def test_datasource_rollup_cards_render_when_universe_is_nonempty():
    # A run with standalone datasources reports the rollup cards with real denominators.
    h = mr.render_report_html(_minimal_report())
    assert "Datasources migrated" in h
    assert "Model measures translated" in h
    assert "Model calc columns" in h


def test_datasource_rollup_cards_suppressed_on_embedded_run():
    # Embedded-datasource (workbook-only) runs leave these universes at 0; a "0 / 0" card reads as a
    # false failure, so the three datasource-rollup cards must be suppressed -- while the meaningful
    # workbook/visual cards remain.
    rpt = _minimal_report(summary={
        "datasources_migrated": 0, "datasources_total": 0,
        "datasources_partial": 0, "datasources_fallback": 0,
        "measures_translated": 0, "measures_total": 0, "measures_stubbed": 0,
        "calc_columns_translated": 0, "calc_columns_total": 0, "calc_columns_stubbed": 0,
        "workbook_calcs_translated": 16, "workbook_calcs_total": 69,
        "workbook_calcs_needs_review": 53,
        "visuals_rebuilt": 3, "visuals_warned": 38,
        "workbooks_viz_built": 1, "workbooks_total": 1, "workbooks_viz_error": 0,
    })
    h = mr.render_report_html(rpt)
    assert "Datasources migrated" not in h
    assert "Model measures translated" not in h
    assert "Model calc columns" not in h
    # Meaningful cards still present.
    assert "Workbook calcs translated" in h
    assert "Visuals rebuilt" in h
    assert "Workbooks viz built" in h


def test_copilot_readiness_all_na_reads_not_evaluated():
    # Embedded-datasource runs grade nothing (every check is "na"). The banner must NOT claim
    # "ready" (nothing was actually evaluated) -- it renders an honest "not evaluated" verdict.
    rpt = _minimal_report(copilot_readiness={
        "overall": "ready",
        "totals": {"passed": 0, "warned": 0, "failed": 0},
        "checks": [
            {"label": "Datasources migrated", "status": "na",
             "detail": "No datasources were in scope."},
            {"label": "Measures translated", "status": "na",
             "detail": "No measures were migrated."},
        ],
    })
    h = mr.render_report_html(rpt)
    assert "not evaluated" in h
    assert "nothing to ground for Copilot" in h
    # The misleading green "ready" verdict must be gone.
    assert "This model is grounded for Power BI" not in h


def test_copilot_readiness_ready_when_checks_actually_pass():
    # A run that genuinely graded datasources still reports the real "ready" verdict.
    rpt = _minimal_report(copilot_readiness={
        "overall": "ready",
        "totals": {"passed": 3, "warned": 0, "failed": 0},
        "checks": [
            {"label": "Datasources migrated", "status": "pass",
             "detail": "2 of 2 migrated.", "metric": 100},
        ],
    })
    h = mr.render_report_html(rpt)
    assert "This model is grounded for Power BI" in h
    assert "not evaluated" not in h


def test_disclosure_marker_css_is_not_octal_mangled():
    # The category disclosure triangle uses a CSS unicode escape (\25B8 / \25BE). In a non-raw
    # Python string that must be written "\\25B8", else Python parses \25 as octal (0x15) and the
    # report shows a tofu box + "B8". Guard both: no control char, real CSS escape present.
    h = mr.render_report_html(_minimal_report())
    assert "\x15" not in h  # octal-mangled control char must never reach the HTML
    assert "\\25B8" in h    # literal CSS escape for the closed-state triangle
    assert "\\25BE" in h    # literal CSS escape for the open-state triangle


def test_dod_not_applicable_renders_muted_banner():
    rpt = _minimal_report(definition_of_done={"applicable": False})
    h = mr.render_report_html(rpt)
    assert "not applicable" in h
    assert "banner muted" in h


def test_empty_report_does_not_crash():
    h = mr.render_report_html({})
    assert h.startswith("<!doctype html>")
    assert "Coverage" in h


def _directlake_seam_report():
    rpt = _minimal_report()
    rpt["datasources"][0]["directlake_seam"] = {
        "expression": "DirectLake - Sample - Superstore",
        "directlake_url": "https://onelake.dfs.fabric.microsoft.com/ws/lh/Tables",
        "schema": None,
        "url_is_placeholder": False,
        "needs_landing": [
            {"table": "Orders", "delta_name": "Orders"},
            {"table": "People", "delta_name": "People"},
        ],
        "stripped_calc_columns": [
            {"table": "Orders", "columns": ["Day - Order Date", "Manufacturer"]},
        ],
        "calendar_adjustments": [
            {"table": "Date", "from": "CALENDARAUTO()",
             "to": "CALENDAR(DATE(2015,1,1),DATE(2025,12,31))"},
        ],
    }
    return rpt


def test_directlake_section_surfaces_manifest_stripped_and_calendar():
    h = mr.render_report_html(_directlake_seam_report())
    # section heading
    assert "OneLake deployment lineage" in h
    # landing manifest
    assert "Delta landing manifest" in h
    assert "Orders" in h and "People" in h
    # stripped calc columns (names surfaced)
    assert "Calculated columns removed" in h
    assert "Day - Order Date" in h
    assert "Manufacturer" in h
    # calendar rewrite surfaced
    assert "Calendar rewritten" in h
    assert "CALENDARAUTO()" in h
    # the OneLake URL is shown as escaped text, but NEVER as a fetchable asset reference
    assert "onelake.dfs.fabric.microsoft.com" in h
    assert 'src="http' not in h.lower()
    assert 'href="http' not in h.lower()
    assert "@import" not in h.lower()
    assert "<script" not in h.lower()


def test_directlake_remediation_plan_renders_buckets():
    # When the seam carries a per-column remediation plan, the section shows what to DO with each
    # dropped column (bucketed), not just that it was removed.
    rpt = _directlake_seam_report()
    rpt["datasources"][0]["directlake_seam"]["stripped_calc_columns"] = [
        {
            "table": "Orders",
            "columns": ["Day - Order Date", "Region Rank", "Product Type"],
            "remediation": {
                "buckets": {
                    "materialize_upstream": [
                        {"name": "Day - Order Date", "bucket": "materialize_upstream",
                         "rationale": "row-level deterministic"},
                    ],
                    "measure_worklist": [
                        {"name": "Region Rank", "bucket": "measure_worklist",
                         "rationale": "aggregation"},
                    ],
                    "field_parameter": [
                        {"name": "Product Type", "bucket": "field_parameter",
                         "rationale": "parameter"},
                    ],
                },
                "counts": {"materialize_upstream": 1, "measure_worklist": 1, "field_parameter": 1},
            },
        },
    ]
    h = mr.render_report_html(rpt)
    assert "Remediation plan" in h
    assert "Materialize upstream" in h
    assert "Author as a DAX measure" in h
    assert "Field / what-if parameter" in h
    # each column appears under its bucket
    assert "Day - Order Date" in h
    assert "Region Rank" in h
    assert "Product Type" in h


def test_directlake_remediation_plan_absent_without_block():
    # Older manifests without a remediation block render the stripped table but no plan.
    h = mr.render_report_html(_directlake_seam_report())
    assert "Calculated columns removed" in h
    assert "Remediation plan" not in h


def test_directlake_materialization_sql_renders():
    # When the seam carries generated materialization SQL, the report shows it (copy-friendly) so the
    # customer can add the row-level columns as physical Delta columns upstream.
    rpt = _directlake_seam_report()
    rpt["datasources"][0]["directlake_seam"]["stripped_calc_columns"] = [
        {
            "table": "Orders",
            "columns": ["Revenue", "Year"],
            "materialization": {
                "table": "Orders",
                "view": "Orders_enriched",
                "sql": "CREATE OR REPLACE TABLE `dbo`.`Orders_enriched` AS\nSELECT\n    *,\n"
                       "    (`Sales` / NULLIF((1 - `Discount`), 0)) AS `Revenue`\nFROM `dbo`.`Orders`;"
                       "\n\n-- REVIEW [Year]: unsupported function RELATED()",
                "columns": [
                    {"name": "Revenue", "sql": "(`Sales` / NULLIF((1 - `Discount`), 0))",
                     "ok": True, "reason": ""},
                    {"name": "Year", "sql": None, "ok": False, "reason": "unsupported function RELATED()"},
                ],
                "covered": 1,
                "needs_manual": 1,
            },
        },
    ]
    h = mr.render_report_html(rpt)
    assert "Generated materialization SQL" in h
    assert "CREATE OR REPLACE TABLE" in h
    assert "Orders_enriched" in h
    assert "1 materialized, 1 need manual SQL" in h
    # SQL is escaped, never a live asset reference
    assert 'src="http' not in h.lower()
    assert "<script" not in h.lower()


def test_directlake_section_absent_without_seam():
    # A plain migration (no DirectLake seam) omits the section entirely.
    h = mr.render_report_html(_minimal_report())
    assert "OneLake deployment lineage" not in h


def test_directlake_placeholder_url_is_flagged():
    rpt = _directlake_seam_report()
    rpt["datasources"][0]["directlake_seam"]["url_is_placeholder"] = True
    h = mr.render_report_html(rpt)
    assert "placeholder" in h


def test_directlake_seam_on_workbook_entry_is_rendered():
    # A consolidated workbook builds its model in the workbook path, so its seam audit lives on the
    # workbook entry (datasources rollup is empty). The renderer must read workbooks[] too.
    seam = _directlake_seam_report()["datasources"][0]["directlake_seam"]
    rpt = _minimal_report(datasources=[])
    rpt["workbooks"] = [{"name": "Superstore Sales Dashboard", "directlake_seam": seam}]
    h = mr.render_report_html(rpt)
    assert "OneLake deployment lineage" in h
    assert "Superstore Sales Dashboard" in h
    assert "Delta landing manifest" in h
    assert "Day - Order Date" in h


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
