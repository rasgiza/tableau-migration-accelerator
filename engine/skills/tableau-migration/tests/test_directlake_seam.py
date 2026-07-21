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
    res = _build(EXCEL_DS, directlake_url=url, directlake_schema=None)
    parts = res["parts"]
    orders = parts["definition/tables/Orders.tmdl"]
    # classic (non-schema) lakehouse: no schemaName line, unqualified lineage
    assert "schemaName:" not in orders
    assert "sourceLineageTag: [Orders]" in orders
    assert url in parts["definition/expressions.tmdl"]
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
    new_parts, landed = convert_import_parts_to_directlake_seam(
        parts, expression_name="DL", directlake_url="https://onelake/Tables")
    assert landed == [{"table": "Orders", "delta_name": "Orders"}]
    # Orders rebound
    assert "partition Orders = entity" in new_parts["definition/tables/Orders.tmdl"]
    assert "#table(type table [], {})" not in new_parts["definition/tables/Orders.tmdl"]
    # _Measures (a calculated partition) is carried through byte-for-byte
    assert new_parts["definition/tables/_Measures.tmdl"] == measures
    # model re-emitted with DirectLake annotations, table order preserved
    model = new_parts["definition/model.tmdl"]
    assert "DirectLakeOnOneLakeInWeb" in model
    assert model.index("ref table Orders") < model.index("ref table _Measures")


def test_converter_no_m_partition_is_noop():
    parts = {
        "definition/tables/_Measures.tmdl": "table _Measures\n\tpartition _Measures = calculated\n",
        "definition/model.tmdl": "model Model\nref table _Measures\n",
    }
    new_parts, landed = convert_import_parts_to_directlake_seam(
        parts, expression_name="DL", directlake_url="https://onelake/Tables")
    assert landed == []
    assert new_parts == parts
