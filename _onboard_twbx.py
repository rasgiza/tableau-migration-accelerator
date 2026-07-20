"""Onboard a downloaded Tableau .twbx to the Azure SQL stand-in (Option 2).

For each .twbx this does the full "native-source rebind" motion automatically:

  1. Read the embedded .twb and parse the datasource schema straight from the
     workbook's own <metadata-record> entries (remote-name + local-type).
  2. CREATE the matching Azure SQL table (union of business columns across all
     supplied workbooks -- so several Superstore dashboards collapse to ONE
     shared `dbo.Orders`, exactly the "many workbooks -> few models" story).
  3. Seed deterministic synthetic rows that fit the real schema/types.
  4. Rewrite the workbook's federated connection to point at Azure SQL and
     collapse its relation to the single `[dbo].<Table>` table, then repackage
     the .twbx so the accelerator reads a live-SQL-backed workbook.

This is *demo-setup* tooling (uses pyodbc + an Entra token). It is deliberately
kept OUT of the stdlib-only engine -- a real customer already has their data in
Snowflake / SQL Server, so steps 1-3 collapse to "point at the real source".

Auth: Microsoft Entra access token (NO SQL password). Requires an `az login`
session, pyodbc, and ODBC Driver 18 for SQL Server.

Usage:
    py -3.11 _onboard_twbx.py --table Orders customer-estate/"Superstore KPIs.twbx" ...
    py -3.11 _onboard_twbx.py --table Orders --glob "customer-estate/Superstore*.twbx"
"""
import argparse
import glob as _glob
import io
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

import pyodbc

SQL_COPT_SS_ACCESS_TOKEN = 1256

DEFAULT_SERVER = os.environ.get("TABMIG_SERVER", "sql-tabmig-ysh95n.database.windows.net")
DEFAULT_DB = os.environ.get("TABMIG_DB", "tabmigdb")

# Tableau local-type -> SQL Server type.
TYPE_MAP = {
    "integer": "INT",
    "real": "FLOAT",
    "date": "DATE",
    "datetime": "DATETIME2",
    "boolean": "BIT",
    "string": "NVARCHAR(255)",
}
SCALAR_TYPES = set(TYPE_MAP)

# Columns that are not real physical business columns (calc/param/spatial noise).
DENYLIST = {
    "Number of Records", "Measure Names", "Measure Values", "Geometry",
    "campo", "Abbreviation", "Column", "Row",
}
_CLEAN_NAME = re.compile(r"^[A-Za-z0-9 /_.\-]+$")

# Deterministic domains for realistic synthetic seeding (Superstore grain).
DOMAINS = {
    "Category": ["Furniture", "Office Supplies", "Technology"],
    "Sub-Category": ["Chairs", "Tables", "Binders", "Paper", "Phones", "Storage", "Copiers"],
    "Region": ["East", "West", "Central", "South"],
    "Segment": ["Consumer", "Corporate", "Home Office"],
    "Ship Mode": ["Standard Class", "Second Class", "First Class", "Same Day"],
    "Country": ["United States"],
    "Country/Region": ["United States"],
    "State": ["California", "New York", "Texas", "Washington", "Florida", "Illinois"],
    "State/Province": ["California", "New York", "Texas", "Washington", "Florida"],
    "City": ["Seattle", "New York City", "Los Angeles", "Houston", "Chicago", "Miami"],
    "Returned": ["Yes", "No"],
    "Regional Manager": ["Sadie Pawthorne", "Chuck Magee", "Roxanne Rodriguez", "Fred Suzuki"],
}


def _token_conn(server: str, db: str) -> pyodbc.Connection:
    raw = subprocess.check_output(
        ["az", "account", "get-access-token", "--resource",
         "https://database.windows.net/", "--query", "accessToken", "-o", "tsv"],
        shell=True,
    ).decode().strip()
    tok = raw.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(tok)}s", len(tok), tok)
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={server},1433;Database={db};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;"
    )
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})


def _inner_twb_name(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.lower().endswith(".twb")]
    if not names:
        raise ValueError("no .twb inside .twbx")
    return names[0]


def parse_columns(twb_xml: str) -> "list[tuple[str, str]]":
    """Return ordered (remote_name, local_type) business columns from the workbook."""
    root = ET.fromstring(twb_xml)
    seen: "dict[str, str]" = {}
    for rec in root.iter("metadata-record"):
        if rec.get("class") != "column":
            continue
        rn = rec.findtext("remote-name")
        lt = rec.findtext("local-type")
        if not rn or not lt:
            continue
        rn = rn.strip()
        lt = lt.strip()
        if lt not in SCALAR_TYPES:
            continue
        if rn in DENYLIST or not _CLEAN_NAME.match(rn):
            continue
        if rn not in seen:
            seen[rn] = lt
    return list(seen.items())


def _seed_value(rng: random.Random, col: str, sqltype: str, i: int):
    if col in DOMAINS:
        return rng.choice(DOMAINS[col])
    if sqltype == "DATE" or sqltype == "DATETIME2":
        return f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    if sqltype == "INT":
        low = col.lower()
        if "postal" in low:
            return rng.randint(10001, 99999)
        if "row id" in low:
            return i + 1
        if "quantity" in low:
            return rng.randint(1, 14)
        return rng.randint(1, 1000)
    if sqltype == "FLOAT":
        low = col.lower()
        if "discount" in low:
            return round(rng.uniform(0, 0.8), 2)
        if "profit" in low:
            return round(rng.uniform(-200, 900), 2)
        return round(rng.uniform(10, 5000), 2)  # sales / generic amount
    if sqltype == "BIT":
        return rng.randint(0, 1)
    # generic string
    return f"{col} {i % 50 + 1}"


def create_and_seed(server: str, db: str, table: str,
                    cols: "list[tuple[str, str]]", nrows: int) -> None:
    full = f"dbo.{table}"
    cn = _token_conn(server, db)
    cur = cn.cursor()
    cur.execute(f"IF OBJECT_ID('{full}','U') IS NOT NULL DROP TABLE {full};")
    coldefs = ", ".join(f"[{c}] {TYPE_MAP[t]}" for c, t in cols)
    cur.execute(f"CREATE TABLE {full} ({coldefs});")
    cn.commit()
    print(f"  created {full} ({len(cols)} cols)")

    rng = random.Random(42)
    collist = ",".join(f"[{c}]" for c, _ in cols)
    placeholders = ",".join("?" for _ in cols)
    ins = f"INSERT INTO {full} ({collist}) VALUES ({placeholders})"
    rows = []
    for i in range(nrows):
        rows.append(tuple(_seed_value(rng, c, TYPE_MAP[t], i) for c, t in cols))
    cur.fast_executemany = True
    cur.executemany(ins, rows)
    cn.commit()
    cur.execute(f"SELECT COUNT(*) FROM {full};")
    print(f"  seeded {full}: {cur.fetchone()[0]} rows")
    cur.close()
    cn.close()


def repoint_twb(twb_xml: str, table: str, server: str, db: str) -> str:
    """Rewrite the primary federated datasource to a single Azure SQL table.

    Because every business column is unioned into one `dbo.<table>`, all
    references to the original source tables (`[Orders$]`, `[Returns]`,
    `[People]`, hashed extract stubs, ...) are collapsed to `[<table>]` so the
    denormalized columns still resolve against the single flat table.
    """
    root = ET.fromstring(twb_xml)
    conn_name = "sqlserver.tabmig"
    tref = f"[{table}]"
    changed = 0
    for ds in root.iter("datasource"):
        conn = ds.find("connection")
        if conn is None or conn.get("class") != "federated":
            continue
        # Replace named-connections with a single sqlserver connection.
        for nc in list(conn.findall("named-connections")):
            conn.remove(nc)
        ncs = ET.Element("named-connections")
        nc = ET.SubElement(ncs, "named-connection",
                           {"caption": db, "name": conn_name})
        ET.SubElement(nc, "connection", {
            "authentication": "sqlserver", "class": "sqlserver", "dbname": db,
            "server": server, "username": "svc_placeholder", "one-time-sql": "",
        })
        conn.insert(0, ncs)
        # Replace all relation elements with a single table relation.
        for rel in list(conn.findall("relation")):
            conn.remove(rel)
        table_rel = ET.Element("relation", {
            "connection": conn_name, "name": table,
            "table": f"[dbo].[{table}]", "type": "table",
        })
        conn.insert(1, table_rel)
        # Collapse every metadata-record parent-name to the single table.
        for rec in ds.iter("metadata-record"):
            pn = rec.find("parent-name")
            if pn is not None:
                pn.text = tref
        # Rewrite cols map values ([OldTable].[col] -> [table].[col]).
        for m in ds.iter("map"):
            val = m.get("value")
            if val and "]." in val:
                m.set("value", tref + val[val.index("].") + 1:])
        changed += 1
    if not changed:
        raise ValueError("no federated datasource found to repoint")
    return ET.tostring(root, encoding="unicode")


def write_repointed_twbx(src_twbx: str, new_twb_xml: str) -> None:
    """Rewrite the .twb inside the .twbx in place, preserving other entries."""
    tmp = src_twbx + ".tmp"
    with zipfile.ZipFile(src_twbx, "r") as zin:
        twb_name = _inner_twb_name(zin)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == twb_name:
                    data = new_twb_xml.encode("utf-8")
                zout.writestr(item, data)
    os.replace(tmp, src_twbx)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("twbx", nargs="*", help=".twbx files to onboard")
    ap.add_argument("--glob", help="glob pattern for .twbx files")
    ap.add_argument("--table", default="Orders", help="target SQL table name")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--rows", type=int, default=1000)
    ap.add_argument("--backup-dir", default="_estate_originals")
    args = ap.parse_args()

    files = list(args.twbx)
    if args.glob:
        files.extend(_glob.glob(args.glob))
    files = sorted(set(files))
    if not files:
        print("no .twbx supplied", file=sys.stderr)
        return 2

    # 1. Parse each workbook's schema, build the union.
    union: "dict[str, str]" = {}
    per_file = {}
    for f in files:
        with zipfile.ZipFile(f, "r") as zf:
            xml = zf.read(_inner_twb_name(zf)).decode("utf-8", "replace")
        cols = parse_columns(xml)
        per_file[f] = cols
        for c, t in cols:
            union.setdefault(c, t)
        print(f"parsed {os.path.basename(f)}: {len(cols)} business columns")
    ordered = list(union.items())
    print(f"\nunion schema for dbo.{args.table}: {len(ordered)} columns")

    # 2 + 3. Create + seed the shared SQL table.
    create_and_seed(args.server, args.db, args.table, ordered, args.rows)

    # 4. Repoint each workbook (backing up the original first).
    os.makedirs(args.backup_dir, exist_ok=True)
    for f in files:
        bak = os.path.join(args.backup_dir, os.path.basename(f))
        if not os.path.exists(bak):
            shutil.copy2(f, bak)
        with zipfile.ZipFile(f, "r") as zf:
            xml = zf.read(_inner_twb_name(zf)).decode("utf-8", "replace")
        new_xml = repoint_twb(xml, args.table, args.server, args.db)
        write_repointed_twbx(f, new_xml)
        print(f"  repointed {os.path.basename(f)} -> dbo.{args.table}")

    print("\nOnboard complete. Re-run the accelerator to bind these workbooks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
