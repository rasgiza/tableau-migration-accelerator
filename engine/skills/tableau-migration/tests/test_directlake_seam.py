"""Extract-backed -> DirectLake-over-OneLake seam.

An extract-backed datasource (Excel/CSV direct, or a ``.twbx`` packaging only a ``.hyper`` cache)
whose bundled data can NOT be materialized used to emit empty Power Query stub partitions
(``Source = #table(type table [], {})``) -- a model that opens but loads nothing. It now rebinds
the base tables onto a completable DirectLake-over-OneLake seam (entity partitions + a shared
``AzureStorage.DataLake`` expression) the customer finishes by mirroring the source to OneLake as
Delta. The translated measures, calculated columns, hierarchies and the calendar Date table are
preserved -- only the base tables' DATA binding changes.
"""
import connection_to_m as C
from assemble_model import (
    migrate_datasource,
    convert_import_parts_to_directlake_seam,
    configure_directlake_seam,
    DIRECTLAKE_SEAM_URL_PLACEHOLDER,
)


EXCEL_DS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='Superstore' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='Superstore' name='excel.abc'>
        <connection class='excel-direct' filename='Sample - Superstore.xlsx' validate='no' />
      </named-connection>
    </named-connections>
    <relation connection='excel.abc' name='Orders' table='[Orders$]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Order Date</remote-name><local-name>[Order Date]</local-name>
        <parent-name>[Orders]</parent-name><local-type>datetime</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
      <metadata-record class='column'>
        <remote-name>Region</remote-name><local-name>[Region]</local-name>
        <parent-name>[Orders]</parent-name><local-type>string</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


# A LIVE SQL Server datasource -- NOT extract-backed, so it must stay a direct Import (never a seam).
LIVE_SQL_DS = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='LiveOrders' inline='true' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='LiveOrders' name='sql.abc'>
        <connection class='sqlserver' server='sql.example.com' dbname='Sales' />
      </named-connection>
    </named-connections>
    <relation connection='sql.abc' name='Orders' table='[dbo].[Orders]' type='table' />
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>Sales</remote-name><local-name>[Sales]</local-name>
        <parent-name>[Orders]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def _build(tds, **kw):
    return migrate_datasource(
        tds, model_name="Superstore",
        calcs=[{"name": "Total Sales", "formula": "SUM([Sales])", "role": "measure"}], **kw)


# =============================================================================
# is_extract_backed predicate
# =============================================================================
def test_is_extract_backed_true_for_excel_direct():
    d = C.parse_tds(EXCEL_DS)
    assert C.is_extract_backed(d) is True


def test_is_extract_backed_false_for_live_sql():
    d = C.parse_tds(LIVE_SQL_DS)
    assert C.is_extract_backed(d) is False


# =============================================================================
# End-to-end seam through migrate_datasource
# =============================================================================
def test_excel_no_data_rebinds_to_directlake_seam():
    res = _build(EXCEL_DS)
    parts = res["parts"]
    orders = parts["definition/tables/Orders.tmdl"]
    # base table is now a DirectLake entity, NOT an empty Import stub
    assert "partition Orders = entity" in orders
    assert "mode: directLake" in orders
    assert "sourceLineageTag: [dbo].[Orders]" in orders
    assert "#table(type table [], {})" not in orders
    assert "mode: import" not in orders
    # a spaced source header binds via a quoted sourceColumn (mirror preserves source names)
    assert 'sourceColumn: "Order Date"' in orders
    # shared DirectLake expression + on-OneLake model annotations
    assert "AzureStorage.DataLake" in parts["definition/expressions.tmdl"]
    assert "DirectLakeOnOneLakeInWeb" in parts["definition/model.tmdl"]
    # translated measures and the calendar Date table are preserved
    assert "definition/tables/_Measures.tmdl" in parts
    assert "definition/tables/Date.tmdl" in parts


def test_seam_records_landing_manifest_with_placeholder_url():
    res = _build(EXCEL_DS)
    seam = res["report"]["directlake_seam"]
    assert seam["needs_landing"] == [{"table": "Orders", "delta_name": "Orders"}]
    assert seam["url_is_placeholder"] is True
    assert seam["directlake_url"] == DIRECTLAKE_SEAM_URL_PLACEHOLDER
    assert seam["expression"] == "DirectLake - Superstore"


def test_seam_honors_explicit_directlake_url_and_schema():
    url = "https://onelake.dfs.fabric.microsoft.com/ws/lh/Tables"
    res = _build(EXCEL_DS, directlake_url=url, directlake_schema="")
    parts = res["parts"]
    orders = parts["definition/tables/Orders.tmdl"]
    # classic (non-schema) lakehouse: no schemaName line, unqualified lineage
    assert "schemaName:" not in orders
    assert "sourceLineageTag: [Orders]" in orders
    # the shared expression is normalised to the OneLake *item root* (no trailing /Tables) --
    # Direct-Lake-on-OneLake rejects a /Tables-suffixed source; entity partitions navigate down.
    assert 'AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/ws/lh"' in parts["definition/expressions.tmdl"]
    assert "/Tables" not in parts["definition/expressions.tmdl"]
    seam = res["report"]["directlake_seam"]
    assert seam["url_is_placeholder"] is False
    assert seam["directlake_url"] == url
    assert seam["schema"] is None


def test_live_sql_source_stays_import_not_seam():
    res = _build(LIVE_SQL_DS)
    parts = res["parts"]
    orders = parts["definition/tables/Orders.tmdl"]
    assert "partition Orders = m" in orders
    assert "mode: directLake" not in orders
    assert "= entity" not in orders
    assert "directlake_seam" not in res["report"]


# =============================================================================
# Process-wide estate seam target (configure_directlake_seam)
# =============================================================================
def test_configured_estate_target_applies_without_explicit_arg():
    url = "https://onelake.dfs.fabric.microsoft.com/ws2/mirror/Tables"
    prev = configure_directlake_seam(url, schema="dbo")
    try:
        res = _build(EXCEL_DS)  # no explicit directlake_url
        orders = res["parts"]["definition/tables/Orders.tmdl"]
        assert "sourceLineageTag: [dbo].[Orders]" in orders
        # expression normalised to the item root (trailing /Tables stripped)
        assert "https://onelake.dfs.fabric.microsoft.com/ws2/mirror" in res["parts"]["definition/expressions.tmdl"]
        assert "/Tables" not in res["parts"]["definition/expressions.tmdl"]
        seam = res["report"]["directlake_seam"]
        assert seam["directlake_url"] == url
        assert seam["url_is_placeholder"] is False
    finally:
        configure_directlake_seam(*prev)


def test_explicit_arg_overrides_configured_target():
    configured = "https://onelake.dfs.fabric.microsoft.com/ws2/mirror/Tables"
    explicit = "https://onelake.dfs.fabric.microsoft.com/ws3/lh3/Tables"
    prev = configure_directlake_seam(configured)
    try:
        res = _build(EXCEL_DS, directlake_url=explicit)
        # item-root normalised: the explicit target's root wins, the configured one is absent
        assert "https://onelake.dfs.fabric.microsoft.com/ws3/lh3" in res["parts"]["definition/expressions.tmdl"]
        assert "ws2/mirror" not in res["parts"]["definition/expressions.tmdl"]
        assert "/Tables" not in res["parts"]["definition/expressions.tmdl"]
    finally:
        configure_directlake_seam(*prev)


def test_placeholder_when_nothing_configured():
    # ensure a clean slate even if a prior test leaked state
    prev = configure_directlake_seam(None)
    try:
        res = _build(EXCEL_DS)
        assert res["report"]["directlake_seam"]["url_is_placeholder"] is True
    finally:
        configure_directlake_seam(*prev)


# =============================================================================
# Pure converter: calculated / entity tables are never rebound
# =============================================================================
def test_converter_only_rebinds_m_partitions():
    imp_orders = (
        "table Orders\n"
        "\tcolumn Sales\n"
        "\t\tdataType: double\n"
        "\t\tsourceColumn: Sales\n\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet Source = #table(type table [], {}) in Source\n\n"
    )
    measures = (
        "table _Measures\n"
        "\tpartition _Measures = calculated\n"
        "\t\tmode: import\n"
        "\t\tsource = {0}\n"
    )
    parts = {
        "definition/tables/Orders.tmdl": imp_orders,
        "definition/tables/_Measures.tmdl": measures,
        "definition/model.tmdl": "model Model\nref table Orders\nref table _Measures\n",
        "definition/expressions.tmdl": "expression X = 1\n",
    }
    new_parts, landed, stripped, calendar = convert_import_parts_to_directlake_seam(
        parts, expression_name="DL", directlake_url="https://onelake/Tables")
    assert landed == [{"table": "Orders", "delta_name": "Orders"}]
    assert stripped == []  # Orders had only a physical column, nothing to strip
    assert calendar == []  # no CALENDARAUTO in this model
    # Orders rebound
    assert "partition Orders = entity" in new_parts["definition/tables/Orders.tmdl"]
    assert "#table(type table [], {})" not in new_parts["definition/tables/Orders.tmdl"]
    # _Measures (a calculated partition) is carried through byte-for-byte
    assert new_parts["definition/tables/_Measures.tmdl"] == measures
    # model re-emitted with DirectLake annotations, table order preserved
    model = new_parts["definition/model.tmdl"]
    assert "DirectLakeOnOneLakeInWeb" in model
    assert model.index("ref table Orders") < model.index("ref table _Measures")


def test_converter_strips_calc_columns_from_directlake_table():
    # Direct Lake tables cannot carry calculated columns -- the converter must drop them (recording
    # the names) while preserving physical columns and hierarchies, so the entity table saves.
    imp_orders = (
        "table Orders\n"
        "\tcolumn Sales\n"
        "\t\tdataType: double\n"
        "\t\tsourceColumn: Sales\n\n"
        "\tcolumn 'Product Name'\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: Product Name\n\n"
        "\tcolumn Manufacturer = SWITCH(TRUE(),\n"
        "\t\t\t'Orders'[Product Name] = \"X\", \"Acme\",\n"
        "\t\t\t\"Other\")\n"
        "\t\tdataType: string\n\n"
        "\tcolumn 'Day - Order Date' = DAY('Orders'[Order Date])\n"
        "\t\tdataType: int64\n\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet Source = #table(type table [], {}) in Source\n\n"
    )
    parts = {
        "definition/tables/Orders.tmdl": imp_orders,
        "definition/model.tmdl": "model Model\nref table Orders\n",
        "definition/expressions.tmdl": "expression X = 1\n",
    }
    new_parts, landed, stripped, calendar = convert_import_parts_to_directlake_seam(
        parts, expression_name="DL", directlake_url="https://onelake/Tables")
    orders = new_parts["definition/tables/Orders.tmdl"]
    assert landed == [{"table": "Orders", "delta_name": "Orders"}]
    # the two calc columns are recorded and removed; physical columns survive
    assert len(stripped) == 1 and stripped[0]["table"] == "Orders"
    assert stripped[0]["columns"] == ["Manufacturer", "Day - Order Date"]
    # each stripped column is routed to a remediation bucket (both are row-level deterministic
    # SWITCH / DAY expressions -> materialize upstream in the Lakehouse).
    rem = stripped[0]["remediation"]
    assert rem["counts"]["materialize_upstream"] == 2
    assert [r["name"] for r in rem["buckets"]["materialize_upstream"]] == ["Manufacturer", "Day - Order Date"]
    assert "= SWITCH" not in orders and "= DAY" not in orders
    assert "Manufacturer" not in orders and "Day - Order Date" not in orders
    assert "column Sales" in orders and "column 'Product Name'" in orders
    assert "partition Orders = entity" in orders and "mode: directLake" in orders


def test_converter_no_m_partition_is_noop():
    parts = {
        "definition/tables/_Measures.tmdl": "table _Measures\n\tpartition _Measures = calculated\n",
        "definition/model.tmdl": "model Model\nref table _Measures\n",
    }
    new_parts, landed, stripped, calendar = convert_import_parts_to_directlake_seam(
        parts, expression_name="DL", directlake_url="https://onelake/Tables")
    assert landed == []
    assert stripped == []
    assert calendar == []
    assert new_parts == parts


def test_converter_neutralizes_calendarauto_on_directlake_seam():
    # CALENDARAUTO() implicitly scans DirectLake date columns -- the engine rejects a calculated
    # table that binds to the DirectLake source. Once a base table is rebound onto DirectLake, the
    # converter must rewrite CALENDARAUTO() to a bounded CALENDAR() and record the change.
    imp_orders = (
        "table Orders\n"
        "\tcolumn Sales\n"
        "\t\tdataType: double\n"
        "\t\tsourceColumn: Sales\n\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet Source = #table(type table [], {}) in Source\n\n"
    )
    date_tbl = (
        "table Date\n"
        "\tcolumn Date\n"
        "\t\tdataType: dateTime\n\n"
        "\tpartition Date = calculated\n"
        "\t\tmode: import\n"
        "\t\tsource = CALENDARAUTO()\n"
    )
    parts = {
        "definition/tables/Orders.tmdl": imp_orders,
        "definition/tables/Date.tmdl": date_tbl,
        "definition/model.tmdl": "model Model\nref table Orders\nref table Date\n",
        "definition/expressions.tmdl": "expression X = 1\n",
    }
    new_parts, landed, stripped, calendar = convert_import_parts_to_directlake_seam(
        parts, expression_name="DL", directlake_url="https://onelake/Tables")
    date_out = new_parts["definition/tables/Date.tmdl"]
    assert "CALENDARAUTO" not in date_out
    assert "CALENDAR(DATE(2010, 1, 1), DATE(2035, 12, 31))" in date_out
    assert calendar == [{
        "table": "Date",
        "from": "CALENDARAUTO()",
        "to": "CALENDAR(DATE(2010, 1, 1), DATE(2035, 12, 31))",
    }]
    # the Date column and partition structure are otherwise preserved
    assert "column Date" in date_out and "partition Date = calculated" in date_out


def test_converter_leaves_calendarauto_when_nothing_rebound():
    # With no DirectLake rebind (no = m base table) there is no DirectLake source to bind to, so
    # CALENDARAUTO() is correct and must be left untouched.
    date_tbl = (
        "table Date\n"
        "\tpartition Date = calculated\n"
        "\t\tmode: import\n"
        "\t\tsource = CALENDARAUTO()\n"
    )
    parts = {
        "definition/tables/Date.tmdl": date_tbl,
        "definition/model.tmdl": "model Model\nref table Date\n",
    }
    new_parts, landed, stripped, calendar = convert_import_parts_to_directlake_seam(
        parts, expression_name="DL", directlake_url="https://onelake/Tables")
    assert landed == []
    assert calendar == []
    assert new_parts["definition/tables/Date.tmdl"] == date_tbl
