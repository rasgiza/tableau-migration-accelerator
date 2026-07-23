"""Tests for the DirectLake remediation router (Option 3 -- materialize stripped calc columns upstream).

The router is PURE: given a stripped calculated column's name / DAX / role / fallback-category it
returns one of four remediation buckets. These tests pin the routing for representative real columns
from the Superstore pilot (Year / Month / Revenue -> materialize; Selected metric / Year Filter ->
field parameter; MoM Sales % -> measure worklist; Customer Name Initial (SPLIT) -> review).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import directlake_remediation as R  # noqa: E402


def _bucket(name, dax=None, **kw):
    return R.classify_directlake_remediation(name, dax, **kw)["bucket"]


# --------------------------------------------------------------------------- materialize upstream
def test_date_part_column_materializes_upstream():
    assert _bucket("Year", "YEAR('Orders'[Order Date])") == R.MATERIALIZE_UPSTREAM
    assert _bucket("Month", "MONTH('Orders'[Order Date])") == R.MATERIALIZE_UPSTREAM


def test_bare_rename_and_arithmetic_materialize_upstream():
    # a pure column reference (rename) and simple arithmetic have NO functions -> row-level.
    assert _bucket("Revenue", "'Orders'[Sales]") == R.MATERIALIZE_UPSTREAM
    assert _bucket("Net", "'Orders'[Sales] - 'Orders'[Discount]") == R.MATERIALIZE_UPSTREAM


def test_row_level_if_and_text_materialize_upstream():
    assert _bucket("Order Status", 'IF(\'Orders\'[Profit] > 0, "Win", "Loss")') == R.MATERIALIZE_UPSTREAM
    assert _bucket("Initial", "UPPER(LEFT('People'[Name], 1))") == R.MATERIALIZE_UPSTREAM


def test_constant_stub_without_column_ref_routes_to_review():
    # A row-level expression that references NO base column is a stubbed placeholder (BLANK()/literal)
    # -- there is nothing to materialize, so it must NOT be called materialize-upstream.
    assert _bucket("Selected Shape", "BLANK()") == R.REVIEW
    assert _bucket("Deselect", '""') == R.REVIEW
    assert _bucket("Const", "42") == R.REVIEW


def test_row_level_with_column_ref_still_materializes():
    assert _bucket("Day", "DATE(YEAR('Orders'[Order_Date]), 1, 1)") == R.MATERIALIZE_UPSTREAM
    assert _bucket("Revenue", "DIVIDE('Orders'[Sales], 1 - 'Orders'[Discount])") == R.MATERIALIZE_UPSTREAM


def test_string_literals_do_not_create_phantom_functions():
    # Product-name text inside a string literal must NOT be read as a function call (e.g. the
    # "(2nd Generation)" here would otherwise look like a GENERATION() call and force review).
    dax = ('SWITCH(TRUE(), \'Orders\'[Product_Name] IN { "Cube Printer (2nd Generation)" }, '
           '"Acme", "Other")')
    assert _bucket("Manufacturer", dax) == R.MATERIALIZE_UPSTREAM


# --------------------------------------------------------------------------- field parameter
def test_selectedvalue_routes_to_field_parameter():
    assert _bucket("Selected metric (Sales)", "SELECTEDVALUE('Parameter'[Value])") == R.FIELD_PARAMETER


def test_parameter_reference_routes_to_field_parameter():
    assert _bucket("Year Filter", "IF('Orders'[Year] = [Parameters].[Year Parameter], 1, 0)") == R.FIELD_PARAMETER


def test_model_object_parameter_category_wins_even_without_dax():
    assert _bucket("Selected Shape", None, category=R.MODEL_OBJECT_PARAMETER) == R.FIELD_PARAMETER


# --------------------------------------------------------------------------- measure worklist
def test_aggregation_routes_to_measure_worklist():
    assert _bucket("MoM Sales %", "DIVIDE(SUM('Orders'[Sales]), 100)") == R.MEASURE_WORKLIST


def test_measure_role_never_materializes():
    # even a row-level-looking expression, if it is a measure by role, is not a physical column.
    assert _bucket("CM Sales", "'Orders'[Sales]", role="measure") == R.MEASURE_WORKLIST


def test_rankx_routes_to_measure_worklist():
    assert _bucket("Rank customer", "RANKX(ALL('People'), [Sales])") == R.MEASURE_WORKLIST


# --------------------------------------------------------------------------- review (keep stub)
def test_split_is_dax_gap_and_routes_to_review():
    assert _bucket("Customer Name Initial", "SPLIT('People'[Name], ' ')") == R.REVIEW


def test_empty_dax_routes_to_review():
    assert _bucket("Mystery", "") == R.REVIEW
    assert _bucket("Mystery", None) == R.REVIEW


def test_unknown_function_mix_routes_to_review():
    assert _bucket("Weird", "SOMENEWFUNC('Orders'[X])") == R.REVIEW


# --------------------------------------------------------------------------- never raises / contract
def test_never_raises_and_returns_known_bucket():
    for bad in (None, 123, {}, [], "  "):
        out = R.classify_directlake_remediation("n", bad)
        assert out["bucket"] in R.BUCKETS
        assert out["name"] == "n"
        assert isinstance(out["rationale"], str) and out["rationale"]


# --------------------------------------------------------------------------- list grouping helper
def test_classify_stripped_columns_groups_and_counts():
    cols = [
        {"name": "Year", "dax": "YEAR('Orders'[Order Date])"},
        {"name": "Month", "dax": "MONTH('Orders'[Order Date])"},
        {"name": "Selected metric", "dax": "SELECTEDVALUE('P'[V])"},
        {"name": "MoM Sales %", "dax": "SUM('Orders'[Sales])", "role": "measure"},
        {"name": "Customer Name Initial", "dax": "SPLIT('People'[Name], ' ')"},
        "Bare Name Only",  # bare string -> name, no DAX -> review
    ]
    out = R.classify_stripped_columns(cols)
    assert out["counts"][R.MATERIALIZE_UPSTREAM] == 2
    assert out["counts"][R.FIELD_PARAMETER] == 1
    assert out["counts"][R.MEASURE_WORKLIST] == 1
    assert out["counts"][R.REVIEW] == 2
    names = [r["name"] for r in out["buckets"][R.MATERIALIZE_UPSTREAM]]
    assert names == ["Year", "Month"]


def test_classify_stripped_columns_empty_is_safe():
    out = R.classify_stripped_columns([])
    assert out["counts"] == {b: 0 for b in R.BUCKETS}
    out2 = R.classify_stripped_columns(None)
    assert out2["counts"] == {b: 0 for b in R.BUCKETS}
