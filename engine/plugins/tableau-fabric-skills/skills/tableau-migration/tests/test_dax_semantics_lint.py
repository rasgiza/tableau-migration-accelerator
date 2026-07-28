"""Tests for the generated-DAX semantic lint (the "compiles clean, ships wrong" backstop)."""
import pytest

from dax_semantics_lint import lint_model_semantics


def _m(name, dax, table="_Measures"):
    return {"measure": name, "dax": dax, "status": "translated",
            "source": {"model_table": table}}


def _kinds(result):
    return {f["kind"] for f in result["findings"]}


# -- compact boolean filter with a measure (blocking) ------------------------------------------

def test_measure_in_calculate_compact_filter_is_blocking():
    report = [
        _m("Prior Year Sales", "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Year] = [Target Year])"),
        _m("Target Year", "MAX('Orders'[Year])"),
    ]
    result = lint_model_semantics(report)
    assert result["ok"] is False
    assert result["blocking"] == 1
    assert _kinds(result) == {"compact_filter_with_measure"}
    assert "[Target Year]" in result["findings"][0]["detail"]


def test_same_predicate_inside_filter_is_legal_and_not_flagged():
    # FILTER() takes a table and returns a table, so the comparison is legal there. The ONLY
    # illegal form is the compact predicate handed straight to CALCULATE.
    report = [
        _m("Prior Year Sales",
           "CALCULATE(SUM('Orders'[Sales]), FILTER('Orders', 'Orders'[Year] = [Target Year]))"),
        _m("Target Year", "MAX('Orders'[Year])"),
    ]
    assert lint_model_semantics(report)["findings"] == []


def test_compact_filter_against_a_literal_is_legal():
    report = [_m("Sales 2024", "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Year] = 2024)")]
    assert lint_model_semantics(report)["findings"] == []


def test_compact_filter_against_a_column_is_not_mistaken_for_a_measure():
    # 'Orders'[Budget] is a COLUMN reference (table-qualified), not the bare [Budget] measure form.
    report = [_m("Over Budget",
                 "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Sales] > 'Orders'[Budget])")]
    assert lint_model_semantics(report)["findings"] == []


def test_unknown_bracket_name_is_not_flagged():
    # Nothing in this build emitted a [Threshold] measure, so the reference is not ours to judge.
    report = [_m("Big Sales", "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Sales] > [Threshold])")]
    assert lint_model_semantics(report)["findings"] == []


def test_measure_reference_in_calculate_first_argument_is_legal():
    # Argument 1 is the expression, not a filter -- a measure there is ordinary DAX.
    report = [
        _m("Filtered Total", "CALCULATE([Base Total], 'Orders'[Region] = \"East\")"),
        _m("Base Total", "SUM('Orders'[Sales])"),
    ]
    assert lint_model_semantics(report)["findings"] == []


def test_bracket_inside_a_string_literal_is_not_a_reference():
    report = [
        _m("Labelled", "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Note] = \"see [Target Year]\")"),
        _m("Target Year", "MAX('Orders'[Year])"),
    ]
    assert lint_model_semantics(report)["findings"] == []


# -- name collisions Tabular only reports on commit --------------------------------------------

def test_duplicate_measure_name_is_blocking():
    result = lint_model_semantics([_m("Sales", "SUM('Orders'[Sales])"),
                                   _m("sales", "SUM('Returns'[Sales])")])
    assert result["ok"] is False
    assert _kinds(result) == {"duplicate_measure_name"}


def test_measure_shadowing_a_column_in_its_own_table_is_blocking():
    manifest = {"columns": [{"model_table": "Orders", "model_name": "Sales"}]}
    result = lint_model_semantics([_m("Sales", "SUM('Orders'[Sales])", table="Orders")],
                                  model_manifest=manifest)
    assert result["ok"] is False
    assert _kinds(result) == {"measure_shadows_column"}


def test_measure_sharing_a_name_with_a_column_in_another_table_is_allowed():
    manifest = {"columns": [{"model_table": "Orders", "model_name": "Sales"}]}
    result = lint_model_semantics([_m("Sales", "SUM('Orders'[Sales])", table="_Measures")],
                                  model_manifest=manifest)
    assert result["findings"] == []


# -- aggregation semantics (advisory: legal DAX, wrong number) ---------------------------------

def test_summing_a_distinct_count_measure_is_advisory():
    report = [
        _m("Total Customers", "SUMX('Region', [Distinct Customers])"),
        _m("Distinct Customers", "DISTINCTCOUNT('Orders'[CustomerID])"),
    ]
    result = lint_model_semantics(report)
    assert result["ok"] is True          # legal DAX -- never fails a build
    assert result["blocking"] == 0
    assert _kinds(result) == {"countd_reaggregated"}


def test_distinct_count_nested_directly_is_advisory():
    result = lint_model_semantics(
        [_m("Total Customers", "SUMX('Region', DISTINCTCOUNT('Orders'[CustomerID]))")])
    assert _kinds(result) == {"countd_reaggregated"}


def test_averaging_a_ratio_measure_is_advisory():
    report = [
        _m("Avg Margin", "AVERAGEX('Region', [Margin Pct])"),
        _m("Margin Pct", "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))"),
    ]
    result = lint_model_semantics(report)
    assert result["ok"] is True
    assert _kinds(result) == {"ratio_reaggregated"}
    assert "denominator weighting" in result["findings"][0]["detail"]


def test_aggregating_a_plain_additive_measure_is_clean():
    report = [
        _m("Total Sales", "SUMX('Region', [Region Sales])"),
        _m("Region Sales", "SUM('Orders'[Sales])"),
    ]
    assert lint_model_semantics(report)["findings"] == []


def test_a_ratio_measure_on_its_own_is_clean():
    assert lint_model_semantics(
        [_m("Margin Pct", "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))")]
    )["findings"] == []


# -- shape, counting, and fail-safety ----------------------------------------------------------

def test_stub_measures_are_not_examined():
    # A stub is an inert `= 0` whose Tableau formula is preserved for a human. The pipeline has
    # already disclosed it; there is no expression to lint.
    report = [{"measure": "Unsupported", "dax": None, "status": "stub",
               "tableau_formula": "WINDOW_SUM(SUM([Sales]))"}]
    result = lint_model_semantics(report)
    assert result["checked"] == 0
    assert result["findings"] == []


def test_clean_counts_objects_not_findings():
    report = [
        _m("A", "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Year] = [Y])"),
        _m("Y", "MAX('Orders'[Year])"),
        _m("B", "SUM('Orders'[Sales])"),
    ]
    result = lint_model_semantics(report)
    assert result["checked"] == 3
    assert result["clean"] == 2          # only measure A is implicated
    assert result["counts"] == {"compact_filter_with_measure": 1}


def test_calc_columns_are_examined_too():
    columns = [{"column": "Bucket", "table": "Orders", "status": "translated",
                "dax": "CALCULATE(SUM('Orders'[Sales]), 'Orders'[Year] = [Y])"}]
    result = lint_model_semantics([_m("Y", "MAX('Orders'[Year])")], calc_column_report=columns)
    assert _kinds(result) == {"compact_filter_with_measure"}
    assert result["findings"][0]["role"] == "column"


def test_an_empty_estate_is_clean_not_broken():
    result = lint_model_semantics([])
    assert result == {"ok": True, "checked": 0, "clean": 0, "blocking": 0, "advisory": 0,
                      "counts": {}, "findings": []}


@pytest.mark.parametrize("report", [None, [None], [{"measure": "X"}], [{"dax": "SUM(x)"}],
                                    [_m("Unbalanced", "CALCULATE(SUM('Orders'[Sales]")]])
def test_malformed_input_never_raises(report):
    result = lint_model_semantics(report)
    assert result["ok"] is True
