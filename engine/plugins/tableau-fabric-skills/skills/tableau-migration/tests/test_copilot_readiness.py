"""Copilot-readiness gate: honest field descriptions, Q&A synonyms on the customer path, and the
read-only readiness scorecard. These lock in that enrichment is (a) OFF by default at the function
level -- so every pre-existing golden stays byte-identical -- and (b) ON, honest, and openable when
the estate/customer path requests it."""
import re

import pytest

from tmdl_generate import (
    generate_measure_tmdl,
    generate_column_tmdl,
    generate_calc_column_tmdl,
)
from assemble_model import assemble_import_model
from connection_to_m import parse_tds
from openability_gate import check_model_openability
from copilot_readiness import score_copilot_readiness
from test_connection_to_m import LIVE_SQLSERVER


_LINEAGE = re.compile(r"lineageTag: [0-9a-f-]{36}")


def _norm(tmdl):
    """Normalize the random lineageTag GUIDs so two emissions can be compared byte-for-byte."""
    return _LINEAGE.sub("lineageTag: <GUID>", tmdl)


# --------------------------------------------------------------------------- emitters

def test_measure_description_absent_by_default_present_when_given():
    off = generate_measure_tmdl("Sales", "SUM([S])", "SUM(x)")
    on = generate_measure_tmdl("Sales", "SUM([S])", "SUM(x)",
                               description="Migrated from a Tableau calculation.")
    assert "///" not in off
    assert "\t/// Migrated from a Tableau calculation.\n\tmeasure Sales = SUM(x)" in on
    # The description is the ONLY difference -- strip it and the two are byte-identical.
    assert _norm(on).replace("\t/// Migrated from a Tableau calculation.\n", "", 1) == _norm(off)


def test_measure_description_none_is_byte_identical():
    assert _norm(generate_measure_tmdl("M", "F", "SUM(x)", description=None)) == \
        _norm(generate_measure_tmdl("M", "F", "SUM(x)"))


def test_column_description_absent_by_default_present_when_given():
    off = generate_column_tmdl("Region", "string", "none", False)
    on = generate_column_tmdl("Region", "string", "none", False, description="Geographic region.")
    assert "///" not in off
    assert on.startswith("\n\t/// Geographic region.\n\tcolumn Region")
    assert _norm(on).replace("\t/// Geographic region.\n", "", 1) == _norm(off)


def test_calc_column_description_absent_by_default_present_when_given():
    off = generate_calc_column_tmdl("Flag", "IF...", "IF(TRUE,1,0)")
    on = generate_calc_column_tmdl("Flag", "IF...", "IF(TRUE,1,0)",
                                   description="Row-level flag migrated from Tableau.")
    assert "///" not in off
    assert "\t/// Row-level flag migrated from Tableau.\n\tcolumn Flag" in on


def test_description_collapses_to_a_single_physical_line():
    # A multi-line / whitespace-heavy description must never inject a newline that breaks the object.
    on = generate_measure_tmdl("M", "F", "SUM(x)",
                               description="line one\n\t line two   with   gaps")
    desc_lines = [l for l in on.splitlines() if "///" in l]
    assert desc_lines == ["\t/// line one line two with gaps"]


# --------------------------------------------------------------------------- customer path wiring

def _calc_measures_tmdl(**kw):
    calcs = [{"name": "Weird Calc", "formula": "RANK_UNSUPPORTED([x])"}]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, **kw)
    return out


def test_copilot_ready_off_is_the_default_and_emits_no_enrichment():
    out = _calc_measures_tmdl()
    parts = out["parts"]
    assert "definition/cultures/en-US.tmdl" not in parts
    assert "ref cultureInfo" not in parts["definition/model.tmdl"]
    assert "///" not in parts["definition/tables/_Measures.tmdl"]
    assert "linguistic" not in out["report"]


def test_copilot_ready_on_emits_description_synonyms_and_stays_openable():
    out = _calc_measures_tmdl(copilot_ready=True)
    parts = out["parts"]
    # honest stub description (this calc cannot translate -> flagged for review, never claimed done)
    mt = parts["definition/tables/_Measures.tmdl"]
    assert "/// Untranslated Tableau calculation -- needs manual review" in mt
    # Q&A synonyms culture + model ref
    assert "definition/cultures/en-US.tmdl" in parts
    assert "ref cultureInfo en-US" in parts["definition/model.tmdl"]
    assert out["report"].get("linguistic", {}).get("language") == "en-US"
    # the enriched model must still be openable (/// is a comment; cultureInfo is a top-level part)
    check = check_model_openability(parts)
    assert check["ok"], check


def test_translated_measure_gets_provenance_not_needs_review():
    # A calc that DOES translate must carry the provenance description, not a needs-review flag.
    calcs = [{"name": "Doubled", "formula": "2 * 3"}]
    out = assemble_import_model(parse_tds(LIVE_SQLSERVER), model_name="Superstore",
                               calcs=calcs, copilot_ready=True)
    mt = out["parts"]["definition/tables/_Measures.tmdl"]
    assert "/// Migrated from a Tableau calculation." in mt
    assert "needs manual review" not in mt


# --------------------------------------------------------------------------- scorecard

def _report(**summary_over):
    summary = {"datasources_total": 1, "datasources_migrated": 1, "datasources_fallback": 0,
               "datasources_error": 0, "measures_total": 4, "measures_translated": 4,
               "measures_stubbed": 0, "calc_columns_total": 0, "calc_columns_translated": 0,
               "calc_columns_stubbed": 0}
    summary.update(summary_over)
    return {"summary": summary,
            "datasources": [{"status": "migrated", "copilot_ready": True,
                             "linguistic": {"terms": 3, "entities": 2}}]}


def test_scorecard_all_clean_is_ready():
    sc = score_copilot_readiness(_report())
    assert sc["overall"] == "ready"
    assert sc["totals"]["failed"] == 0
    assert {c["id"] for c in sc["checks"]} == {
        "datasources", "measures", "calc_columns", "descriptions", "synonyms"}


def test_scorecard_stub_measures_warn_then_fail():
    warn = score_copilot_readiness(_report(measures_translated=4, measures_stubbed=1, measures_total=5))
    m = next(c for c in warn["checks"] if c["id"] == "measures")
    assert m["status"] == "warn" and warn["overall"] == "ready_with_warnings"
    fail = score_copilot_readiness(_report(measures_translated=1, measures_stubbed=4, measures_total=5))
    m2 = next(c for c in fail["checks"] if c["id"] == "measures")
    assert m2["status"] == "fail" and fail["overall"] == "not_ready"


def test_scorecard_errored_datasource_fails():
    rpt = _report(datasources_total=2, datasources_migrated=1, datasources_error=1)
    sc = score_copilot_readiness(rpt)
    ds = next(c for c in sc["checks"] if c["id"] == "datasources")
    assert ds["status"] == "fail" and sc["overall"] == "not_ready"


def test_scorecard_descriptions_off_warns_not_fails():
    rpt = _report()
    rpt["datasources"][0]["copilot_ready"] = False
    sc = score_copilot_readiness(rpt)
    desc = next(c for c in sc["checks"] if c["id"] == "descriptions")
    assert desc["status"] == "warn"
    assert sc["enabled"] is False


def test_scorecard_is_defensive_against_empty_and_partial_reports():
    for bad in ({}, {"summary": {}}, {"datasources": []}, None):
        sc = score_copilot_readiness(bad)  # must never raise
        assert set(sc) == {"enabled", "overall", "totals", "checks", "guidance"}


def test_scorecard_never_mutates_input():
    rpt = _report()
    before = str(rpt)
    score_copilot_readiness(rpt)
    assert str(rpt) == before


def test_scorecard_guidance_is_honest_about_scaffold_vs_business_meaning():
    sc = score_copilot_readiness(_report())
    guidance = sc["guidance"]
    assert guidance, "a migrated model must always carry human next-steps"
    blob = " ".join(guidance).lower()
    # names the scaffold-vs-meaning gap and the data-prep step, not just 'you're done'
    assert "provenance" in blob and "business description" in blob
    assert "prep the" in blob
    # prioritization: must say you do NOT have to describe everything, and to hide the plumbing
    assert "do not have to" in blob and "hide" in blob
    # the descriptions check must NOT overclaim full grounding
    desc = next(c for c in sc["checks"] if c["id"] == "descriptions")
    assert "scaffold" in desc["detail"].lower()


def test_scorecard_guidance_flags_stubs_and_disabled_enrichment():
    with_stubs = score_copilot_readiness(
        _report(measures_translated=4, measures_stubbed=1, measures_total=5))
    assert any("needs manual review" in g.lower() or "inert stub" in g.lower()
               for g in with_stubs["guidance"])
    off = _report()
    off["datasources"][0]["copilot_ready"] = False
    assert any("--no-copilot-ready" in g for g in score_copilot_readiness(off)["guidance"])


def test_guidance_renders_in_html_report():
    from migration_report_html import render_report_html
    rpt = _report()
    rpt["copilot_readiness"] = score_copilot_readiness(rpt)
    html = render_report_html(rpt)
    assert "Make this fully AI-ready" in html
    assert "business description" in html.lower()
