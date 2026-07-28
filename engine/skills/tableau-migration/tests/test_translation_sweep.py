"""Tests for the deterministic-translation sweep (the oracle pointed at the ordinary build)."""
import pytest

import translation_sweep as ts


TABLES = {"Orders": {"columns": ["Region", "Sales", "Profit", "Qty"], "rows": [
    {"Region": "East", "Sales": "100", "Profit": "10", "Qty": "2"},
    {"Region": "East", "Sales": "200", "Profit": "50", "Qty": "4"},
    {"Region": "West", "Sales": "300", "Profit": "30", "Qty": "6"},
]}}


def _resolver(caption):
    return ("Orders", {"Sales": "Sales", "Profit": "Profit", "Qty": "Qty",
                       "Region": "Region"}.get(caption, caption))


def _m(name, formula, dax):
    return {"measure": name, "status": "translated", "tableau_formula": formula, "dax": dax,
            "source": {"model_table": "_Measures"}}


def test_a_faithful_translation_is_proven_over_landed_rows():
    r = ts.sweep_translations([_m("Total Sales", "SUM([Sales])", "SUM('Orders'[Sales])")],
                              tables=TABLES, resolver=_resolver)
    assert r["ok"] is True
    assert (r["checked"], r["verified"], r["unproven"]) == (1, 1, 0)
    assert r["verified_objects"] == ["Total Sales"]
    assert r["disagreements"] == []


def test_a_wrong_translation_is_caught_with_reproducible_evidence():
    # The DAX sums the wrong column. It is valid DAX over a model that loads -- only data catches it.
    r = ts.sweep_translations([_m("Total Sales", "SUM([Sales])", "SUM('Orders'[Profit])")],
                              tables=TABLES, resolver=_resolver)
    assert r["ok"] is False
    assert r["verified"] == 0
    assert len(r["disagreements"]) == 1
    d = r["disagreements"][0]
    assert d["object"] == "Total Sales"
    # a reviewer must be able to reproduce the finding, so both sides and the grain are kept
    assert d["tableau_value"] != d["candidate_value"]
    assert d["grain"]
    assert d["tableau_formula"] == "SUM([Sales])"
    assert d["dax"] == "SUM('Orders'[Profit])"


def test_an_unprovable_translation_is_reported_as_unproven_never_as_a_pass():
    r = ts.sweep_translations(
        [_m("Windowed", "WINDOW_SUM(SUM([Sales]))", "SUM('Orders'[Sales])")],
        tables=TABLES, resolver=_resolver)
    assert r["ok"] is True                     # unprovable is not failing
    assert (r["verified"], r["unproven"]) == (0, 1)
    assert sum(r["unproven_reasons"].values()) == 1


def test_unproven_reasons_are_bucketed_not_counted_verbatim():
    # Raw oracle reasons interpolate table names, so a verbatim histogram would have one entry per
    # expression. Two expressions blocked by the SAME limit must collapse to one bucket of 2.
    report = [_m("A", "WINDOW_SUM(SUM([Sales]))", "SUM('Orders'[Sales])"),
              _m("B", "RUNNING_SUM(SUM([Profit]))", "SUM('Orders'[Profit])")]
    r = ts.sweep_translations(report, tables=TABLES, resolver=_resolver)
    assert r["unproven"] == 2
    assert len(r["unproven_reasons"]) == 1
    assert list(r["unproven_reasons"].values()) == [2]


def test_calc_columns_are_swept_too():
    cols = [{"column": "Margin", "table": "Orders", "status": "translated",
             "tableau_formula": "SUM([Profit])", "dax": "SUM('Orders'[Profit])"}]
    r = ts.sweep_translations([], calc_column_report=cols, tables=TABLES, resolver=_resolver)
    assert r["verified"] == 1
    assert r["checked"] == 1


def test_stubs_are_not_swept():
    # No DAX -> nothing to reconcile, and the pipeline already disclosed it as needing review.
    report = [{"measure": "Unsupported", "status": "stub", "dax": None,
               "tableau_formula": "WINDOW_SUM(SUM([Sales]))"}]
    r = ts.sweep_translations(report, tables=TABLES, resolver=_resolver)
    assert r["checked"] == 0


def test_a_measure_with_no_retained_tableau_formula_is_not_checked_against_itself():
    # Reconciling DAX against DAX would report a confident pass that proves nothing.
    report = [{"measure": "Orphan", "status": "translated", "dax": "SUM('Orders'[Sales])",
               "tableau_formula": None}]
    r = ts.sweep_translations(report, tables=TABLES, resolver=_resolver)
    assert r["checked"] == 0
    assert r["verified"] == 0


def test_no_landed_data_reports_nothing_checked_rather_than_everything_clean():
    r = ts.sweep_translations([_m("Total Sales", "SUM([Sales])", "SUM('Orders'[Sales])")],
                              tables=None, resolver=_resolver)
    assert r == {"ok": True, "checked": 0, "verified": 0, "unproven": 0, "disagreements": [],
                 "verified_objects": [], "unproven_reasons": {}}


def test_counts_always_partition_the_checked_set():
    report = [_m("Good", "SUM([Sales])", "SUM('Orders'[Sales])"),
              _m("Wrong", "SUM([Sales])", "SUM('Orders'[Profit])"),
              _m("Unknowable", "WINDOW_SUM(SUM([Sales]))", "SUM('Orders'[Sales])")]
    r = ts.sweep_translations(report, tables=TABLES, resolver=_resolver)
    assert r["checked"] == r["verified"] + r["unproven"] + len(r["disagreements"]) == 3


def test_tables_can_be_loaded_from_landed_csv_paths(tmp_path):
    csv = tmp_path / "Orders.csv"
    csv.write_text("Region,Sales\nEast,100\nWest,300\n", encoding="utf-8")
    r = ts.sweep_translations([_m("Total Sales", "SUM([Sales])", "SUM('Orders'[Sales])")],
                              table_csv_paths={"Orders": str(csv)}, resolver=_resolver)
    assert r["verified"] == 1


@pytest.mark.parametrize("report", [None, [None], [{"measure": "X"}], "not a list",
                                    [{"dax": "SUM('Orders'[Sales])", "tableau_formula": 5}]])
def test_malformed_input_never_raises(report):
    r = ts.sweep_translations(report, tables=TABLES, resolver=_resolver)
    assert r["ok"] is True
    assert r["disagreements"] == []


def test_extract_csv_stems_are_aliased_onto_model_table_names():
    # A .hyper extract lands as Extract_<Table>_<32 hex>.csv while the model -- and the DAX -- calls
    # the table Orders. Without the alias the oracle finds no rows for anything and the sweep proves
    # nothing while looking like it ran.
    paths = {"Extract_Orders_5043CEDD90404865BA448E7254C82A3D": r"C:\d\o.csv",
             "Extract_People_5C05E7B425AA40ABB664E53A75401308": r"C:\d\p.csv"}
    out = ts.align_table_names(paths, ["Orders", "People", "Date"])

    assert out["Orders"] == r"C:\d\o.csv"
    assert out["People"] == r"C:\d\p.csv"
    assert "Date" not in out              # no landed file -> no invented alias
    assert set(paths) <= set(out)         # original keys are never dropped


def test_an_ambiguous_stem_match_is_skipped_rather_than_guessed():
    # Two landed files normalize to the same table name. Picking one could feed the oracle the WRONG
    # table's rows and manufacture a proven "disagreement" -- a false accusation of wrong numbers is
    # worse than an honest unproven, so the alias is refused.
    paths = {"Extract_Orders_5043CEDD90404865BA448E7254C82A3D": "a.csv",
             "Orders_5C05E7B425AA40ABB664E53A75401308": "b.csv"}
    out = ts.align_table_names(paths, ["Orders"])
    assert out.get("Orders") is None


def test_alignment_never_overwrites_a_real_landed_table():
    out = ts.align_table_names({"Orders": "real.csv", "Extract_Orders_" + "a" * 32: "extract.csv"},
                               ["Orders"])
    assert out["Orders"] == "real.csv"


@pytest.mark.parametrize("paths,names", [(None, None), ({}, ["Orders"]), ({"X": "x.csv"}, None),
                                         ({None: "x.csv"}, [None, ""])])
def test_alignment_tolerates_malformed_input(paths, names):
    assert isinstance(ts.align_table_names(paths, names), dict)
