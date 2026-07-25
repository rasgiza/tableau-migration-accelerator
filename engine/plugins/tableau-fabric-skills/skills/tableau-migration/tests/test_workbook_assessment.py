"""Tests for :mod:`workbook_assessment` -- the pre-migration sizing scan.

Covers surface-area counting, orphaned-worksheet + unused-field detection, the documented
1-5 complexity rubric, the transparent 0-100 difficulty score, per-component impact rating,
the estate roll-up, and the report-HTML section that surfaces it. Fixtures are hand-authored
``.twb`` XML shaped like real Tableau output (worksheets, dashboards/zones, datasource
columns + calculations, a Parameters datasource, a custom-SQL relation).
"""

import workbook_assessment as A


# --- fixtures ------------------------------------------------------------------------------

_SIMPLE = """<?xml version='1.0'?>
<workbook>
  <datasources>
    <datasource caption='DS' name='federated.ds'>
      <connection class='federated'>
        <relation name='Orders' table='[Orders]' type='table' />
        <metadata-records>
          <metadata-record class='column'><remote-name>Sales</remote-name>
            <local-name>[Sales]</local-name></metadata-record>
          <metadata-record class='column'><remote-name>Region</remote-name>
            <local-name>[Region]</local-name></metadata-record>
        </metadata-records>
      </connection>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Used'>
      <table><view><datasource-dependencies datasource='federated.ds'>
        <column caption='Sales' name='[Sales]' role='measure' />
        <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' />
      </datasource-dependencies></view></table>
    </worksheet>
    <worksheet name='Lonely'>
      <table><view><datasource-dependencies datasource='federated.ds'>
        <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' />
      </datasource-dependencies></view></table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='DB'><zones><zone name='Used' x='0' y='0' w='1' h='1' /></zones></dashboard>
  </dashboards>
</workbook>"""


_RICH = """<?xml version='1.0'?>
<workbook>
  <datasources>
    <datasource caption='DS' name='federated.ds'>
      <connection class='federated'>
        <relation name='CustomSQL' type='text'>SELECT * FROM Orders</relation>
        <metadata-records>
          <metadata-record class='column'><remote-name>Sales</remote-name>
            <local-name>[Sales]</local-name></metadata-record>
          <metadata-record class='column'><remote-name>Region</remote-name>
            <local-name>[Region]</local-name></metadata-record>
          <metadata-record class='column'><remote-name>Ghost</remote-name>
            <local-name>[Ghost Col]</local-name></metadata-record>
        </metadata-records>
      </connection>
      <column caption='Fixed Sales' name='[Calc Fixed]' role='measure'>
        <calculation class='tableau' formula='{ FIXED [Region] : SUM([Sales]) }' /></column>
      <column caption='Include Profit' name='[Calc Inc]' role='measure'>
        <calculation class='tableau' formula='{ INCLUDE [Category] : AVG([Profit]) }' /></column>
      <column caption='Windowed' name='[Calc TC]' role='measure'>
        <calculation class='tableau' formula='WINDOW_SUM(SUM([Sales]))' /></column>
      <column caption='Gated' name='[Calc Param]' role='measure'>
        <calculation class='tableau' formula='IF [Parameter 1] &gt; 0 THEN SUM([Sales]) END' /></column>
    </datasource>
    <datasource name='Parameters'>
      <column caption='Threshold' name='[Parameter 1]' datatype='integer'>
        <calculation class='tableau' formula='10' /></column>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='WS1'>
      <table><view><datasource-dependencies datasource='federated.ds'>
        <column caption='Sales' name='[Sales]' role='measure' />
        <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' />
        <column-instance column='[Calc Fixed]' derivation='User' name='[usr:Calc Fixed:qk]' />
      </datasource-dependencies></view></table>
    </worksheet>
    <worksheet name='WS2'>
      <table><view><datasource-dependencies datasource='federated.ds'>
        <column-instance column='[Calc TC]' derivation='User' name='[usr:Calc TC:qk]' />
      </datasource-dependencies></view></table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='DB1'><zones><zone name='WS1' x='0' y='0' w='1' h='1' /></zones></dashboard>
  </dashboards>
</workbook>"""


# --- surface area & dead content -----------------------------------------------------------

def test_empty_input_returns_valid_empty_assessment():
    a = A.assess_workbook("")
    assert a["surface_area"]["worksheets"] == 0
    assert a["complexity"]["bucket"] == "Simple"
    assert a["difficulty"]["band"] == "Low"
    assert a["unused_fields"] == []
    assert a["orphaned_worksheets"] == []


def test_garbage_input_does_not_raise():
    a = A.assess_workbook("<not-xml <<<")
    assert a["surface_area"]["worksheets"] == 0


def test_simple_surface_area_and_orphaned_worksheet():
    a = A.assess_workbook(_SIMPLE)
    surf = a["surface_area"]
    assert surf["worksheets"] == 2
    assert surf["dashboards"] == 1
    # 'Used' is on dashboard DB via a zone; 'Lonely' is on no dashboard.
    assert a["orphaned_worksheets"] == ["Lonely"]
    assert a["complexity"]["bucket"] == "Simple"


def test_dashboard_map_records_per_dashboard_worksheet_membership():
    a = A.assess_workbook(_SIMPLE)
    dm = a["dashboard_map"]
    assert dm == [{"name": "DB", "worksheets": ["Used"]}]
    # An empty assessment still exposes the key (never a KeyError downstream).
    assert A.assess_workbook("")["dashboard_map"] == []


def test_unused_field_detection_is_conservative():
    a = A.assess_workbook(_RICH)
    unused_ids = {u["id"] for u in a["unused_fields"]}
    # [Ghost Col] is defined but never referenced anywhere -> flagged.
    assert "[Ghost Col]" in unused_ids
    # [Sales] is placed on WS1 and referenced by the FIXED calc -> never flagged.
    assert "[Sales]" not in unused_ids
    # [Region] is referenced only inside a calc formula -> still counts as used.
    assert "[Region]" not in unused_ids


# --- calc profile / scoring ----------------------------------------------------------------

def test_rich_workbook_calc_profile():
    a = A.assess_workbook(_RICH)
    surf = a["surface_area"]
    assert surf["calculations"] == 4          # 4 author calcs; parameter default is NOT a calc
    assert surf["parameters"] == 1
    assert surf["lod_expressions"] == 2        # FIXED + INCLUDE
    assert surf["table_calcs"] == 1            # WINDOW_SUM
    drivers = a["difficulty"]["drivers"]
    assert drivers["parameter_driven_calcs"] == 1
    assert drivers["custom_sql"] == 1
    assert drivers["nested_calcs"] == 1        # IF-gated calc (not LOD/table calc)


def test_rich_workbook_is_complex_bucket():
    a = A.assess_workbook(_RICH)
    # Heavy LOD + table calc + custom SQL + params -> top of the 1-5 rubric.
    assert a["complexity"]["score"] == 5
    assert a["complexity"]["bucket"] == "Complex"


def test_difficulty_is_transparent_arithmetic():
    a = A.assess_workbook(_RICH)
    d = a["difficulty"]
    # 3*2(LOD) + 3*1(TC) + 1*1(nested) + 1*1(param) + 8*1(custom sql) + 0.2*4(calcs) = 19.8
    assert d["score"] == 19.8
    assert d["band"] == "Low"
    assert "min(100" in d["formula"]


def test_component_impact_rating():
    a = A.assess_workbook(_RICH)
    by_name = {c["name"]: c for c in a["components"]}
    assert by_name["Windowed"]["impact"] == "High"        # table calc
    assert by_name["Include Profit"]["impact"] == "High"  # INCLUDE LOD
    assert by_name["Fixed Sales"]["impact"] == "Medium"   # FIXED LOD
    assert by_name["Gated"]["impact"] == "Medium"         # parameter-driven / conditional
    # Custom SQL surfaces as its own High-impact component.
    assert any(c["kind"] == "custom sql" and c["impact"] == "High" for c in a["components"])


# --- estate roll-up ------------------------------------------------------------------------

def test_aggregate_carries_hardest_workbook_and_sums_surface():
    simple = {"name": "Simple", "assessment": A.assess_workbook(_SIMPLE)}
    rich = {"name": "Rich", "assessment": A.assess_workbook(_RICH)}
    agg = A.aggregate_assessment([simple, rich])
    assert agg["workbooks"] == 2
    assert agg["surface_area"]["worksheets"] == 4          # 2 + 2
    assert agg["surface_area"]["lod_expressions"] == 2     # only the rich one has LOD
    assert agg["complexity"]["max_score"] == 5             # hardest workbook wins
    assert agg["orphaned_worksheets_total"] >= 1
    assert agg["difficulty"]["max_score"] == 19.8
    assert {r["name"] for r in agg["by_workbook"]} == {"Simple", "Rich"}


def test_aggregate_ignores_workbooks_without_assessment():
    agg = A.aggregate_assessment([{"name": "None"}, {"name": "X", "assessment": {}}])
    assert agg["workbooks"] == 0


# --- report HTML integration ---------------------------------------------------------------

def test_report_html_renders_assessment_section():
    from migration_report_html import render_report_html
    wb = {"name": "Rich", "assessment": A.assess_workbook(_RICH)}
    report = {
        "tool": "migrate_estate",
        "summary": {},
        "workbooks": [wb],
        "assessment": A.aggregate_assessment([wb]),
    }
    html = render_report_html(report)
    assert 'id="assessment"' in html
    assert "Estate assessment" in html
    assert "Migration difficulty" in html
    # The difficulty formula is printed so the score is defensible, not opaque.
    assert "min(100" in html
    # A dead-content candidate (orphaned worksheet WS2) is surfaced.
    assert "WS2" in html


def test_report_html_omits_assessment_when_absent():
    from migration_report_html import render_report_html
    html = render_report_html({"tool": "migrate_estate", "summary": {}})
    assert 'id="assessment"' not in html
