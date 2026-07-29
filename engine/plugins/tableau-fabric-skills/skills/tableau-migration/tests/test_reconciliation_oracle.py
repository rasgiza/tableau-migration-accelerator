"""Tests for the local reconciliation oracle (scalar aggregate / ratio path).

The oracle empirically proves a candidate DAX is numerically faithful to the original Tableau
formula over the landed data -- the empirical half of the second-compiler gate that
``check_candidate_dax`` deliberately leaves out. The contract under test: PASS only on genuine
agreement over real rows; FAIL on divergence; INCONCLUSIVE (never a false PASS) for anything outside
the supported subset.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import reconciliation_oracle as ro  # noqa: E402
from reconciliation_oracle import reconcile, PASS, FAIL, INCONCLUSIVE  # noqa: E402


# -- fixtures ------------------------------------------------------------------------------------
_SCHEMA = {
    "Profit": ("Orders", "Profit", "decimal"),
    "Sales": ("Orders", "Sales", "decimal"),
    "Quantity": ("Orders", "Quantity", "integer"),
    "Price": ("Orders", "Price", "decimal"),
    "State": ("Orders", "State", "string"),
    "Category": ("Orders", "Category", "string"),
}


def _resolver(caption):
    return _SCHEMA.get(caption)


def _orders():
    rows = [
        {"State": "CA", "Category": "Tech", "Profit": 10, "Sales": 100, "Quantity": 2, "Price": 50},
        {"State": "CA", "Category": "Toys", "Profit": 5, "Sales": 40, "Quantity": 4, "Price": 10},
        {"State": "NY", "Category": "Tech", "Profit": 20, "Sales": 80, "Quantity": 1, "Price": 80},
        {"State": "NY", "Category": "Toys", "Profit": 8, "Sales": 60, "Quantity": 3, "Price": 20},
        {"State": "TX", "Category": "Tech", "Profit": 15, "Sales": 90, "Quantity": 2, "Price": 45},
    ]
    return {"Orders": {"columns": list(rows[0].keys()), "rows": rows}}


# -- PASS: equivalent expressions ----------------------------------------------------------------
def test_identical_ratio_passes():
    v = reconcile("SUM([Profit])/SUM([Sales])",
                  "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v
    assert v["groups_compared"] >= 1


def test_plain_slash_vs_divide_are_equivalent():
    v = reconcile("SUM([Profit]) / SUM([Sales])",
                  "SUM('Orders'[Profit]) / SUM('Orders'[Sales])",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_sum_of_product_via_sumx_passes():
    # Tableau SUM([Quantity]*[Price]) == DAX SUMX('Orders', 'Orders'[Quantity]*'Orders'[Price]).
    v = reconcile("SUM([Quantity] * [Price])",
                  "SUMX('Orders', 'Orders'[Quantity] * 'Orders'[Price])",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_countd_matches_distinctcount():
    v = reconcile("COUNTD([State])", "DISTINCTCOUNT('Orders'[State])",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_countrows_matches_count_of_nonnull_column():
    # Every row has a Sales value, so COUNT([Sales]) == COUNTROWS('Orders').
    v = reconcile("COUNT([Sales])", "COUNTROWS('Orders')", _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_zn_matches_coalesce():
    v = reconcile("ZN(SUM([Profit]))", "COALESCE(SUM('Orders'[Profit]), 0)",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_literal_scaling_passes():
    v = reconcile("SUM([Sales]) * 1.1", "SUM('Orders'[Sales]) * 1.1",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_average_matches_dax_average():
    v = reconcile("AVG([Sales])", "AVERAGE('Orders'[Sales])", _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


# -- FAIL: wrong candidate -----------------------------------------------------------------------
def test_swapped_ratio_fails():
    v = reconcile("SUM([Profit])/SUM([Sales])",
                  "DIVIDE(SUM('Orders'[Sales]), SUM('Orders'[Profit]))",
                  _orders(), resolver=_resolver)
    assert v["status"] == FAIL, v
    assert "differ" in v["reason"]


def test_wrong_literal_scaling_fails():
    v = reconcile("SUM([Sales]) * 1.1", "SUM('Orders'[Sales]) * 1.2",
                  _orders(), resolver=_resolver)
    assert v["status"] == FAIL, v


def test_wrong_column_fails():
    v = reconcile("SUM([Profit])", "SUM('Orders'[Sales])", _orders(), resolver=_resolver)
    assert v["status"] == FAIL, v


def test_coincidental_total_match_caught_by_auto_grain():
    # total(A) == total(B) but they differ per State -> auto-grain must FAIL it.
    schema = {"A": ("T", "A", "int"), "B": ("T", "B", "int")}
    tables = {"T": {"columns": ["State", "A", "B"], "rows": [
        {"State": "X", "A": 10, "B": 1},
        {"State": "Y", "A": 1, "B": 10},
    ]}}
    v = reconcile("SUM([A])", "SUM('T'[B])", tables, resolver=lambda c: schema.get(c))
    assert v["status"] == FAIL, v


# -- FAIL surfaced only at an explicit grain -----------------------------------------------------
def test_explicit_grain_catches_group_divergence():
    schema = {"A": ("T", "A", "int"), "B": ("T", "B", "int"), "G": ("T", "G", "str")}
    tables = {"T": {"columns": ["G", "A", "B"], "rows": [
        {"G": "X", "A": 10, "B": 1},
        {"G": "Y", "A": 1, "B": 10},
    ]}}
    v = reconcile("SUM([A])", "SUM('T'[B])", tables,
                  resolver=lambda c: schema.get(c), grain=["G"])
    assert v["status"] == FAIL, v


# -- INCONCLUSIVE: out of subset / no data -------------------------------------------------------
def test_out_of_subset_tableau_is_inconclusive():
    v = reconcile("DATEDIFF('day', [Order Date], [Ship Date])", "SUM('Orders'[Sales])",
                  _orders(), resolver=_resolver)
    assert v["status"] == INCONCLUSIVE, v
    assert "tableau" in v["reason"].lower()


def test_a_conditional_that_used_to_be_out_of_subset_is_now_decided():
    # This exact pair was the out-of-subset fixture until conditionals joined the grammar. It is not
    # merely parseable now -- the two sides really do disagree (1 vs the sum), and saying so is the
    # entire point of widening the subset.
    v = reconcile("IF SUM([Sales]) > 0 THEN 1 ELSE 0 END", "SUM('Orders'[Sales])",
                  _orders(), resolver=_resolver)
    assert v["status"] == FAIL, v
    assert v["tableau_value"] == 1.0, v


def test_out_of_subset_candidate_is_inconclusive():
    v = reconcile("SUM([Sales])", "CALCULATE(SUM('Orders'[Sales]), ALL('Orders'))",
                  _orders(), resolver=_resolver)
    assert v["status"] == INCONCLUSIVE, v
    assert "candidate" in v["reason"].lower()


def test_different_table_is_inconclusive():
    v = reconcile("SUM([Sales])", "SUM('Returns'[Sales])", _orders(), resolver=_resolver)
    assert v["status"] == INCONCLUSIVE, v


def test_unresolved_field_is_inconclusive():
    v = reconcile("SUM([Nonexistent])", "SUM('Orders'[Sales])", _orders(), resolver=_resolver)
    assert v["status"] == INCONCLUSIVE, v


def test_no_data_is_inconclusive():
    empty = {"Orders": {"columns": ["Profit", "Sales"], "rows": []}}
    v = reconcile("SUM([Profit])/SUM([Sales])",
                  "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
                  empty, resolver=_resolver)
    assert v["status"] == INCONCLUSIVE, v


def test_missing_table_is_inconclusive():
    v = reconcile("SUM([Profit])", "SUM('Orders'[Profit])", {}, resolver=_resolver)
    assert v["status"] == INCONCLUSIVE, v


def test_max_rows_guard_is_inconclusive():
    v = reconcile("SUM([Profit])", "SUM('Orders'[Profit])", _orders(),
                  resolver=_resolver, max_rows=2)
    assert v["status"] == INCONCLUSIVE, v


def test_empty_inputs_are_inconclusive():
    assert reconcile("", "SUM('Orders'[Sales])", _orders(), resolver=_resolver)["status"] == INCONCLUSIVE
    assert reconcile("SUM([Sales])", "", _orders(), resolver=_resolver)["status"] == INCONCLUSIVE


def test_no_resolver_is_inconclusive():
    v = reconcile("SUM([Sales])", "SUM('Orders'[Sales])", _orders())
    assert v["status"] == INCONCLUSIVE, v


# -- divide-by-zero: a zero-denominator group is skipped; nonzero groups still reconcile ----------
def test_divide_by_zero_group_skipped_but_candidate_passes():
    # NY's Sales sum is 0 -> that group is blank on BOTH sides (same divide-by-zero semantics) and
    # is skipped; the grand total and the CA group still carry numeric evidence -> PASS.
    tables = {"Orders": {"columns": ["Profit", "Sales", "State"], "rows": [
        {"Profit": 5, "Sales": 100, "State": "CA"},
        {"Profit": 7, "Sales": 0, "State": "NY"},
    ]}}
    schema = {"Profit": ("Orders", "Profit", "d"), "Sales": ("Orders", "Sales", "d"),
              "State": ("Orders", "State", "s")}
    v = reconcile("SUM([Profit])/SUM([Sales])",
                  "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
                  tables, resolver=lambda c: schema.get(c))
    assert v["status"] == PASS, v


def test_all_null_ratio_is_inconclusive():
    # Every denominator is zero -> both sides are blank at every grain -> zero discriminating
    # evidence -> the conservative oracle refuses to PASS (a wrong candidate could also be all-blank).
    tables = {"Orders": {"columns": ["Profit", "Zero", "State"], "rows": [
        {"Profit": 5, "Zero": 0, "State": "CA"},
        {"Profit": 7, "Zero": 0, "State": "NY"},
    ]}}
    schema = {"Profit": ("Orders", "Profit", "d"), "Zero": ("Orders", "Zero", "d"),
              "State": ("Orders", "State", "s")}
    v = reconcile("SUM([Profit])/SUM([Zero])",
                  "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Zero]))",
                  tables, resolver=lambda c: schema.get(c))
    assert v["status"] == INCONCLUSIVE, v


# -- bare-list table form + input robustness -----------------------------------------------------
def test_accepts_bare_list_of_rows():
    tables = {"Orders": [
        {"Profit": 10, "Sales": 100},
        {"Profit": 5, "Sales": 50},
    ]}
    v = reconcile("SUM([Profit])/SUM([Sales])",
                  "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
                  tables, resolver=_resolver)
    assert v["status"] == PASS, v


def test_string_numerics_are_coerced():
    tables = {"Orders": {"columns": ["Profit", "Sales"], "rows": [
        {"Profit": "10", "Sales": "100"},
        {"Profit": "5", "Sales": "50"},
    ]}}
    v = reconcile("SUM([Profit])", "SUM('Orders'[Profit])", tables, resolver=_resolver)
    assert v["status"] == PASS, v


def test_never_raises_on_garbage():
    for f, d in [("@@@", "SUM('Orders'[x])"), ("SUM([Sales])", ")("), (None, None)]:
        v = reconcile(f, d, _orders(), resolver=_resolver)
        assert v["status"] in (PASS, FAIL, INCONCLUSIVE)


# -- landed-CSV loader ---------------------------------------------------------------------------
def _write_csv(path, header, rows):
    import csv as _csv
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def test_load_tables_from_csv_basic(tmp_path):
    p = tmp_path / "orders.csv"
    _write_csv(p, ["Profit", "Sales", "State"], [[10, 100, "CA"], [5, 50, "NY"]])
    tables = ro.load_tables_from_csv({"Orders": str(p)})
    assert tables["Orders"]["columns"] == ["Profit", "Sales", "State"]
    assert len(tables["Orders"]["rows"]) == 2
    assert tables["Orders"]["rows"][0]["State"] == "CA"


def test_load_tables_from_csv_feeds_reconcile(tmp_path):
    p = tmp_path / "orders.csv"
    _write_csv(p, ["Profit", "Sales", "State"],
               [[10, 100, "CA"], [5, 40, "CA"], [20, 80, "NY"]])
    tables = ro.load_tables_from_csv({"Orders": str(p)})
    v = reconcile("SUM([Profit])/SUM([Sales])",
                  "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
                  tables, resolver=_resolver)
    assert v["status"] == PASS, v


def test_load_tables_from_csv_column_map_renames_to_model_names(tmp_path):
    # Landed CSV carries the extract's PHYSICAL headers; the map renames them to the model names
    # the resolver + candidate DAX speak, so both sides line up and reconcile.
    p = tmp_path / "orders.csv"
    _write_csv(p, ["profit_amt", "sales_amt"], [[10, 100], [5, 50]])
    tables = ro.load_tables_from_csv(
        {"Orders": str(p)},
        column_map={"Orders": {"profit_amt": "Profit", "sales_amt": "Sales"}})
    assert tables["Orders"]["columns"] == ["Profit", "Sales"]
    v = reconcile("SUM([Profit])/SUM([Sales])",
                  "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))",
                  tables, resolver=_resolver)
    assert v["status"] == PASS, v


def test_load_tables_from_csv_max_rows_caps(tmp_path):
    p = tmp_path / "orders.csv"
    _write_csv(p, ["Profit"], [[i] for i in range(100)])
    tables = ro.load_tables_from_csv({"Orders": str(p)}, max_rows=10)
    assert len(tables["Orders"]["rows"]) == 10


def test_load_tables_from_csv_tolerates_ragged_and_empty(tmp_path):
    ragged = tmp_path / "ragged.csv"
    _write_csv(ragged, ["A", "B", "C"], [[1, 2], [3, 4, 5, 6]])  # short + long rows
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    tables = ro.load_tables_from_csv({"R": str(ragged), "E": str(empty)})
    assert tables["R"]["rows"][0]["C"] is None          # short row padded
    assert tables["R"]["rows"][1]["A"] == "3"           # long row's overflow ignored
    assert tables["E"] == {"columns": [], "rows": []}   # empty file -> empty table



# -- conditionals: IF / CASE / IIF vs DAX IF / SWITCH ---------------------------------------------
# A census of a real 13-workbook estate put IF/CASE/IIF in 38% of all calculated fields -- the
# single biggest reason a translation went unproven. These tests pin the contract for that grammar:
# equivalent spellings across the two dialects must agree, a wrong branch must be CAUGHT, and
# anything the evaluator cannot decide must stay INCONCLUSIVE rather than drift into a false PASS.
def test_tableau_if_matches_the_equivalent_dax_if():
    v = reconcile("SUM(IF [Profit] > 10 THEN [Sales] ELSE 0 END)",
                  "SUMX('Orders', IF('Orders'[Profit] > 10, 'Orders'[Sales], 0))",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_a_flipped_comparison_in_the_candidate_is_caught():
    v = reconcile("SUM(IF [Profit] > 10 THEN [Sales] ELSE 0 END)",
                  "SUMX('Orders', IF('Orders'[Profit] < 10, 'Orders'[Sales], 0))",
                  _orders(), resolver=_resolver)
    assert v["status"] == FAIL, v
    assert v["tableau_value"] != v["candidate_value"], v


def test_case_when_desugars_onto_the_same_node_as_switch():
    v = reconcile('SUM(CASE [Category] WHEN "Tech" THEN [Sales] ELSE 0 END)',
                  'SUMX(\'Orders\', SWITCH(\'Orders\'[Category], "Tech", \'Orders\'[Sales], 0))',
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_elseif_chain_matches_switch_over_true():
    v = reconcile("SUM(IF [Profit] > 15 THEN 100 ELSEIF [Profit] > 7 THEN 10 ELSE 1 END)",
                  "SUMX('Orders', SWITCH(TRUE(), 'Orders'[Profit] > 15, 100, "
                  "'Orders'[Profit] > 7, 10, 1))",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_a_reordered_elseif_chain_is_caught():
    # Branch ORDER is semantics, not style: testing the looser condition first changes the answer.
    v = reconcile("SUM(IF [Profit] > 15 THEN 100 ELSEIF [Profit] > 7 THEN 10 ELSE 1 END)",
                  "SUMX('Orders', SWITCH(TRUE(), 'Orders'[Profit] > 7, 10, "
                  "'Orders'[Profit] > 15, 100, 1))",
                  _orders(), resolver=_resolver)
    assert v["status"] == FAIL, v


def test_iif_matches_if():
    v = reconcile('SUM(IIF([Category] = "Tech", [Sales], 0))',
                  'SUMX(\'Orders\', IF(\'Orders\'[Category] = "Tech", \'Orders\'[Sales], 0))',
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_and_or_not_agree_across_word_and_operator_spellings():
    tableau = 'SUM(IF [Profit] > 5 AND NOT [Category] = "Toys" THEN [Sales] ELSE 0 END)'
    for candidate in (
        "SUMX('Orders', IF(AND('Orders'[Profit] > 5, NOT('Orders'[Category] = \"Toys\")), "
        "'Orders'[Sales], 0))",
        "SUMX('Orders', IF('Orders'[Profit] > 5 && NOT('Orders'[Category] = \"Toys\"), "
        "'Orders'[Sales], 0))",
    ):
        v = reconcile(tableau, candidate, _orders(), resolver=_resolver)
        assert v["status"] == PASS, (candidate, v)


def test_or_is_not_silently_translated_as_and():
    v = reconcile('SUM(IF [Profit] > 15 OR [Category] = "Toys" THEN [Sales] ELSE 0 END)',
                  'SUMX(\'Orders\', IF(AND(\'Orders\'[Profit] > 15, '
                  '\'Orders\'[Category] = "Toys"), \'Orders\'[Sales], 0))',
                  _orders(), resolver=_resolver)
    assert v["status"] == FAIL, v


def test_missing_else_matches_an_explicit_blank():
    # Tableau IF with no ELSE yields NULL; DAX IF with no third argument yields BLANK. The two must
    # land on the same node, or every ELSE-less conditional would read as a disagreement.
    v = reconcile("SUM(IF [Profit] > 10 THEN [Sales] END)",
                  "SUMX('Orders', IF('Orders'[Profit] > 10, 'Orders'[Sales], BLANK()))",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_a_scalar_conditional_over_aggregations_is_decided():
    v = reconcile("IF SUM([Profit]) > 0 THEN SUM([Sales]) ELSE 0 END",
                  "IF(SUM('Orders'[Profit]) > 0, SUM('Orders'[Sales]), 0)",
                  _orders(), resolver=_resolver)
    assert v["status"] == PASS, v


def test_a_text_returning_conditional_is_inconclusive_not_passed():
    # Both sides are identical, but there is no NUMBER to reconcile. Claiming PASS here would be
    # claiming a proof the oracle never performed.
    dax = 'IF(SUM(\'Orders\'[Profit]) > 0, "up", "down")'
    v = reconcile('IF SUM([Profit]) > 0 THEN "up" ELSE "down" END', dax,
                  _orders(), resolver=_resolver)
    assert v["status"] == INCONCLUSIVE, v


def test_a_comparison_against_an_incomparable_type_never_manufactures_a_verdict():
    # [Category] is text; comparing it to a number is unknown, not false. Unknown -> ELSE on both
    # sides -> both are 0 -> equal, but only because the oracle refused to guess an ordering.
    v = reconcile("SUM(IF [Category] > 5 THEN [Sales] ELSE 0 END)",
                  "SUMX('Orders', IF('Orders'[Category] > 5, 'Orders'[Sales], 0))",
                  _orders(), resolver=_resolver)
    assert v["status"] in (PASS, INCONCLUSIVE), v


def test_a_conditional_spanning_two_tables_is_refused():
    # If the walker missed the branch bodies, the second table would go unnoticed and the oracle
    # would evaluate against one table's rows -- a manufactured disagreement.
    schema = dict(_SCHEMA, Other=("Returns", "Flag", "string"))
    v = reconcile("SUM(IF [Other] = \"y\" THEN [Sales] ELSE 0 END)",
                  "SUMX('Orders', IF('Returns'[Flag] = \"y\", 'Orders'[Sales], 0))",
                  _orders(), resolver=lambda c: schema.get(c))
    assert v["status"] == INCONCLUSIVE, v
    assert "table" in v["reason"].lower()


def test_conditional_columns_are_excluded_from_the_auto_grain():
    # A column used only inside a condition is still a measure input; grouping by it would compare
    # each branch against itself and weaken the check.
    ast = ro._TableauParser(
        ro._tokenize('SUM(IF [Category] = "Tech" THEN [Sales] ELSE 0 END)'), _resolver).parse()
    assert ro._columns_of(ast, set()) == {"Category", "Sales"}


def test_malformed_conditionals_stay_inconclusive_and_never_raise():
    for formula in ("IF [Profit] THEN", "CASE [Category] END", "IF THEN ELSE END",
                    "IIF([Profit] > 0)", "CASE WHEN THEN END", "IF [Profit] > 0 THEN 1"):
        v = reconcile(formula, "SUM('Orders'[Sales])", _orders(), resolver=_resolver)
        assert v["status"] == INCONCLUSIVE, (formula, v)
