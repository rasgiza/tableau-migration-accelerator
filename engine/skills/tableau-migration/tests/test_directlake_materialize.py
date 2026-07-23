"""Tests for the row-level DAX -> Spark SQL materialization translator (Option 3 -- materialize
stripped calc columns upstream so Direct Lake reads them as physical columns).

The translator is PURE and correct-or-abstain: it emits Spark SQL only for a whitelisted row-level
function set and rejects anything it cannot translate faithfully (aggregations, RELATED, TODAY,
unknown functions) rather than guessing. These tests pin the four genuinely-materializable Superstore
columns (Day - Order Date, Revenue, Manufacturer, Profit (bin)) and the honest rejections.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import directlake_materialize as M  # noqa: E402


def _sql(dax, **kw):
    r = M.dax_to_sql(dax, **kw)
    assert r["ok"], r["reason"]
    return r["sql"]


# --------------------------------------------------------------------------- scalar translation
def test_column_ref_uses_physical_name_from_map():
    assert _sql("'Orders'[Order_Date]", column_map={"Order_Date": "Order Date"}) == "`Order Date`"


def test_column_ref_falls_back_to_dax_name():
    assert _sql("'Orders'[Sales]") == "`Sales`"


def test_divide_becomes_nullif_guarded_division():
    assert _sql("DIVIDE('Orders'[Sales], (1 - 'Orders'[Discount]))") == \
        "(`Sales` / NULLIF((1 - `Discount`), 0))"


def test_arithmetic_precedence_and_int_floor():
    # Profit (bin): INT((Profit - 0) / 200) * 200 + 0
    sql = _sql("INT(('Orders'[Profit] - 0) / 200) * 200 + 0")
    assert sql == "((CAST(FLOOR(((`Profit` - 0) / 200)) AS BIGINT) * 200) + 0)"


def test_date_normalization_emits_make_date():
    sql = _sql("DATE(YEAR('Orders'[Order_Date]), MONTH('Orders'[Order_Date]), DAY('Orders'[Order_Date]))")
    assert sql == "MAKE_DATE(YEAR(`Order_Date`), MONTH(`Order_Date`), DAY(`Order_Date`))"


def test_switch_true_becomes_case_with_in_list():
    sql = _sql("SWITCH(TRUE(), 'Orders'[Product_Name] IN { \"A\", \"B\" }, \"Acme\", \"Other\")")
    assert sql == "CASE WHEN (`Product_Name` IN ('A', 'B')) THEN 'Acme' ELSE 'Other' END"


def test_switch_value_form_becomes_case_expr():
    sql = _sql("SWITCH('Orders'[Region], \"East\", 1, \"West\", 2, 0)")
    assert sql == "CASE WHEN `Region` = 'East' THEN 1 WHEN `Region` = 'West' THEN 2 ELSE 0 END"


def test_if_becomes_case():
    assert _sql('IF(\'Orders\'[Profit] > 0, "Win", "Loss")') == \
        "CASE WHEN (`Profit` > 0) THEN 'Win' ELSE 'Loss' END"


def test_string_literal_quote_escaping():
    assert _sql('IF(\'Orders\'[X] = "a""b", "y", "z")') in (
        'CASE WHEN (`X` = \'a"b\') THEN \'y\' ELSE \'z\' END',
    )


def test_and_or_and_concat_operators():
    assert _sql("'Orders'[A] && 'Orders'[B]") == "(`A` AND `B`)"
    assert _sql("'Orders'[A] || 'Orders'[B]") == "(`A` OR `B`)"
    assert _sql("'Orders'[A] & 'Orders'[B]") == "(`A` || `B`)"


# --------------------------------------------------------------------------- honest rejections
def test_related_is_rejected_needs_join():
    r = M.dax_to_sql("RELATED('Date'[Year])")
    assert r["ok"] is False and "RELATED" in r["reason"]


def test_volatile_and_unknown_functions_are_rejected():
    assert M.dax_to_sql("YEAR('Orders'[D]) = YEAR(TODAY())")["ok"] is False
    assert M.dax_to_sql("SPLIT('People'[Name], ' ')")["ok"] is False
    assert M.dax_to_sql("SOMENEWFUNC('Orders'[X])")["ok"] is False


def test_empty_and_bad_input_never_raises():
    # Empty / malformed input abstains (ok=False); a bare constant is valid SQL. Never raises.
    for empty in ("", "   ", None):
        assert M.dax_to_sql(empty)["ok"] is False
    for malformed in ("(((", "1 +"):
        assert M.dax_to_sql(malformed)["ok"] is False
    assert M.dax_to_sql(123)["ok"] is True  # the literal 123 -> "123"
    for anything in ("(((", 123, {}, []):
        assert isinstance(M.dax_to_sql(anything)["ok"], bool)


# --------------------------------------------------------------------------- table view assembly
def test_build_table_view_covers_translatable_and_flags_manual():
    cols = [
        {"name": "Revenue", "dax": "DIVIDE('Orders'[Sales], (1 - 'Orders'[Discount]))"},
        {"name": "Year", "dax": "RELATED('Date'[Year])"},
    ]
    out = M.build_table_view("Orders", cols, {"Sales": "Sales", "Discount": "Discount"}, schema="dbo")
    assert out["covered"] == 1 and out["needs_manual"] == 1
    assert "CREATE OR REPLACE TABLE `dbo`.`Orders_enriched` AS" in out["sql"]
    assert "AS `Revenue`" in out["sql"]
    assert "-- REVIEW [Year]:" in out["sql"]
    assert out["view"] == "Orders_enriched"


def test_build_table_view_all_manual_emits_no_create():
    out = M.build_table_view("Orders", [{"name": "Year", "dax": "RELATED('Date'[Year])"}], schema="dbo")
    assert out["covered"] == 0 and out["needs_manual"] == 1
    assert "CREATE OR REPLACE TABLE" not in out["sql"]
    assert out["view"] is None


def test_build_table_view_empty_is_safe():
    out = M.build_table_view("Orders", [], schema="dbo")
    assert out["covered"] == 0 and out["needs_manual"] == 0 and out["sql"] == ""


def test_build_table_view_without_schema():
    out = M.build_table_view("Orders", [{"name": "Rev", "dax": "'Orders'[Sales]"}], schema=None)
    assert "FROM `Orders`" in out["sql"]
    assert "CREATE OR REPLACE TABLE `Orders_enriched` AS" in out["sql"]


# --------------------------------------------------------------------- consolidated script builder
def _stripped(*tables):
    """Build a fake ``stripped_calc_columns`` list from (name, [(col, dax)]) tuples."""
    out = []
    for name, cols in tables:
        out.append({
            "table": name,
            "columns": [c for c, _ in cols],
            "materialization": M.build_table_view(name, [{"name": c, "dax": d} for c, d in cols]),
        })
    return out


def test_materialization_script_consolidates_tables():
    stripped = _stripped(
        ("Orders", [("Rev", "'Orders'[Sales]")]),
        ("People", [("Up", "UPPER('People'[Name])")]),
    )
    out = M.build_materialization_script(stripped, model_name="Superstore")
    assert out["tables"] == 2 and out["covered"] == 2 and out["needs_manual"] == 0
    assert "-- Model: Superstore" in out["sql"]
    assert "-- ===== Table: Orders =====" in out["sql"]
    assert "-- ===== Table: People =====" in out["sql"]
    assert out["sql"].count("CREATE OR REPLACE TABLE") == 2


def test_materialization_script_counts_manual_and_keeps_review_todos():
    stripped = _stripped(("Orders", [
        ("Rev", "'Orders'[Sales]"),
        ("Year", "RELATED('Date'[Year])"),
    ]))
    out = M.build_materialization_script(stripped)
    assert out["tables"] == 1 and out["covered"] == 1 and out["needs_manual"] == 1
    assert "-- REVIEW [Year]" in out["sql"]
    assert "-- Model:" not in out["sql"]  # omitted when model_name is None


def test_materialization_script_includes_review_only_table():
    # An all-manual table produces no CREATE block, only a REVIEW comment. It is STILL included
    # (honest -- the reviewer sees the untranslated work) with covered=0.
    stripped = _stripped(("Orders", [("Year", "RELATED('Date'[Year])")]))
    out = M.build_materialization_script(stripped)
    assert out is not None and out["covered"] == 0 and out["needs_manual"] == 1
    assert "-- REVIEW [Year]" in out["sql"]


def test_materialization_script_empty_input_is_none():
    assert M.build_materialization_script([]) is None
    assert M.build_materialization_script(None) is None


def test_materialization_script_skips_entries_without_materialization():
    stripped = [{"table": "Orders", "columns": ["X"]}]  # no materialization key
    assert M.build_materialization_script(stripped) is None

