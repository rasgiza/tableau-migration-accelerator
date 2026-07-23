"""Row-level DAX -> Spark SQL translator for the DirectLake *materialize upstream* remediation.

When :mod:`directlake_remediation` routes a stripped calculated column to
:data:`~directlake_remediation.MATERIALIZE_UPSTREAM`, that column is a genuine row-level deterministic
expression over base columns. To let Direct Lake read it as a *physical* column, the value must be
computed once UPSTREAM in the Lakehouse (documented Fabric best practice: "perform data-preparation
logic upstream in the architecture to maximize reusability"). This module turns the column's
(already row-level) DAX expression into an equivalent Spark SQL scalar expression and assembles a
``CREATE OR REPLACE TABLE ... AS SELECT *`` materialization script the customer runs in a Lakehouse
notebook / pipeline to produce the enriched Delta table Direct Lake then binds to.

Design contract (mirrors the other Option-3 modules):
  * **Pure and dependency-free.** Text in, text out. No I/O, no model objects, no Fabric calls.
  * **Never raises at the public boundary.** :func:`dax_to_sql` and :func:`build_table_view` catch
    every translation failure and report it as ``ok=False`` with a human reason, so an untranslatable
    expression degrades to an explicit ``-- REVIEW`` TODO rather than a crash or -- worse -- wrong SQL.
  * **Conservative / faithful.** Spark SQL is emitted ONLY for a whitelisted row-level function set.
    Any unknown function, aggregation, cross-table lookup (``RELATED``), or volatile function
    (``TODAY``/``NOW``) is rejected, never guessed. Correct-or-abstain.

Only a scalar, row-level subset is supported (dates, arithmetic, text, IF/SWITCH, IN). Aggregations,
table calcs and parameters are handled by *other* buckets and never reach here.
"""
from __future__ import annotations

import re


class TranslateError(Exception):
    """Raised internally when a DAX expression cannot be faithfully translated to Spark SQL."""


# --------------------------------------------------------------------------- tokenizer
# Order matters: COLREF before IDENT so ``Table[Col]`` / ``'Table'[Col]`` / ``[Col]`` stay whole.
_TOKEN_RE = re.compile(
    r"""
      \s+
    | (?P<NUMBER>\d+\.\d+|\d+)
    | (?P<STRING>"(?:[^"]|"")*")
    | (?P<COLREF>(?:'[^']*'|[A-Za-z_]\w*)?\[[^\]]+\])
    | (?P<IDENT>[A-Za-z_][\w.]*)
    | (?P<OP><=|>=|<>|&&|\|\||[-+*/&=<>(){},])
    """,
    re.VERBOSE,
)

# Row-level scalar functions we can emit faithfully. IF / SWITCH are handled specially.
_UNARY_MATH = {"ABS", "SQRT", "EXP", "LN", "SIGN"}
_DATE_PART = {"YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "QUARTER"}
_TEXT_PASSTHROUGH = {"UPPER", "LOWER", "TRIM", "LEFT", "RIGHT"}


def _tokenize(dax):
    toks = []
    pos, n = 0, len(dax)
    while pos < n:
        m = _TOKEN_RE.match(dax, pos)
        if not m or m.end() == pos:
            raise TranslateError(f"unexpected character at {pos!r}: {dax[pos:pos+12]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind is None:  # whitespace
            continue
        toks.append((kind, m.group()))
    return toks


# --------------------------------------------------------------------------- parser (recursive descent)
# Precedence low->high: OR, AND, comparison/IN, additive/concat, multiplicative, unary, primary.
class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _next(self):
        tok = self._peek()
        self.i += 1
        return tok

    def _expect_op(self, op):
        k, v = self._next()
        if not (k == "OP" and v == op):
            raise TranslateError(f"expected {op!r}, got {v!r}")

    def parse(self):
        node = self._or()
        if self.i != len(self.toks):
            raise TranslateError(f"trailing tokens: {self.toks[self.i:]!r}")
        return node

    def _or(self):
        node = self._and()
        while self._peek() == ("OP", "||"):
            self._next()
            node = ("bin", "OR", node, self._and())
        return node

    def _and(self):
        node = self._cmp()
        while self._peek() == ("OP", "&&"):
            self._next()
            node = ("bin", "AND", node, self._cmp())
        return node

    def _cmp(self):
        node = self._add()
        k, v = self._peek()
        if k == "OP" and v in ("=", "<>", "<", ">", "<=", ">="):
            self._next()
            return ("bin", v, node, self._add())
        if k == "IDENT" and v.upper() == "IN":
            self._next()
            self._expect_op("{")
            items = self._arglist("}")
            self._expect_op("}")
            return ("in", node, items)
        return node

    def _add(self):
        node = self._mul()
        while self._peek()[0] == "OP" and self._peek()[1] in ("+", "-", "&"):
            op = self._next()[1]
            node = ("bin", op, node, self._mul())
        return node

    def _mul(self):
        node = self._unary()
        while self._peek()[0] == "OP" and self._peek()[1] in ("*", "/"):
            op = self._next()[1]
            node = ("bin", op, node, self._unary())
        return node

    def _unary(self):
        if self._peek() == ("OP", "-"):
            self._next()
            return ("neg", self._unary())
        if self._peek() == ("OP", "+"):
            self._next()
            return self._unary()
        return self._primary()

    def _arglist(self, closer):
        args = []
        if self._peek() == ("OP", closer):
            return args
        args.append(self._or())
        while self._peek() == ("OP", ","):
            self._next()
            args.append(self._or())
        return args

    def _primary(self):
        k, v = self._next()
        if k == "NUMBER":
            return ("num", v)
        if k == "STRING":
            return ("str", v[1:-1].replace('""', '"'))
        if k == "COLREF":
            return ("col", _colname(v))
        if k == "OP" and v == "(":
            node = self._or()
            self._expect_op(")")
            return node
        if k == "IDENT":
            name = v.upper()
            if self._peek() == ("OP", "("):
                self._next()
                args = self._arglist(")")
                self._expect_op(")")
                if name in ("TRUE", "FALSE") and not args:
                    return ("bool", name)
                if name == "BLANK" and not args:
                    return ("null",)
                return ("func", name, args)
            if name in ("TRUE", "FALSE"):
                return ("bool", name)
            raise TranslateError(f"bare identifier {v!r} (not a column or known keyword)")
        raise TranslateError(f"unexpected token {v!r}")


def _colname(colref):
    """``'Orders'[Order_Date]`` / ``[Order_Date]`` -> the column name ``Order_Date``."""
    return colref[colref.rindex("[") + 1: colref.rindex("]")].strip()


# --------------------------------------------------------------------------- emitter (AST -> Spark SQL)
_OP_MAP = {"AND": "AND", "OR": "OR", "=": "=", "<>": "<>", "<": "<", ">": ">",
           "<=": "<=", ">=": ">=", "+": "+", "-": "-", "*": "*", "/": "/", "&": "||"}


def _emit(node, colmap):
    tag = node[0]
    if tag == "num":
        return node[1]
    if tag == "str":
        return "'" + node[1].replace("'", "''") + "'"
    if tag == "bool":
        return node[1]
    if tag == "null":
        return "NULL"
    if tag == "col":
        physical = colmap.get(node[1], node[1]) if colmap else node[1]
        return "`" + physical + "`"
    if tag == "neg":
        return "(-" + _emit(node[1], colmap) + ")"
    if tag == "bin":
        return "(" + _emit(node[2], colmap) + " " + _OP_MAP[node[1]] + " " + _emit(node[3], colmap) + ")"
    if tag == "in":
        items = ", ".join(_emit(a, colmap) for a in node[2])
        return "(" + _emit(node[1], colmap) + " IN (" + items + "))"
    if tag == "func":
        return _emit_func(node[1], node[2], colmap)
    raise TranslateError(f"cannot emit node {node!r}")


def _emit_func(name, args, colmap):
    e = [_emit(a, colmap) for a in args]

    if name == "IF":
        if len(args) == 2:
            return f"CASE WHEN {e[0]} THEN {e[1]} END"
        if len(args) == 3:
            return f"CASE WHEN {e[0]} THEN {e[1]} ELSE {e[2]} END"
        raise TranslateError("IF expects 2 or 3 arguments")

    if name == "SWITCH":
        if len(args) < 3:
            raise TranslateError("SWITCH expects at least 3 arguments")
        head, rest = args[0], args[1:]
        pairs, default = rest, None
        if len(rest) % 2 == 1:
            pairs, default = rest[:-1], rest[-1]
        is_true = head[0] == "bool" and head[1] == "TRUE"
        whens = []
        for i in range(0, len(pairs), 2):
            cond = _emit(pairs[i], colmap) if is_true else f"{_emit(head, colmap)} = {_emit(pairs[i], colmap)}"
            whens.append(f"WHEN {cond} THEN {_emit(pairs[i + 1], colmap)}")
        tail = f" ELSE {_emit(default, colmap)}" if default is not None else ""
        return "CASE " + " ".join(whens) + tail + " END"

    if name == "DIVIDE":
        if len(args) == 2:
            return f"({e[0]} / NULLIF({e[1]}, 0))"
        if len(args) == 3:
            return f"COALESCE({e[0]} / NULLIF({e[1]}, 0), {e[2]})"
        raise TranslateError("DIVIDE expects 2 or 3 arguments")

    if name == "DATE" and len(args) == 3:
        return f"MAKE_DATE({e[0]}, {e[1]}, {e[2]})"
    if name in _DATE_PART and len(args) == 1:
        return f"{name}({e[0]})"
    if name == "INT" and len(args) == 1:
        return f"CAST(FLOOR({e[0]}) AS BIGINT)"
    if name in ("FLOOR", "CEILING") and len(args) == 1:
        return ("CEIL" if name == "CEILING" else "FLOOR") + f"({e[0]})"
    if name == "ROUND" and len(args) == 2:
        return f"ROUND({e[0]}, {e[1]})"
    if name == "MOD" and len(args) == 2:
        return f"MOD({e[0]}, {e[1]})"
    if name == "POWER" and len(args) == 2:
        return f"POWER({e[0]}, {e[1]})"
    if name in _UNARY_MATH and len(args) == 1:
        return f"{name}({e[0]})"
    if name == "LEN" and len(args) == 1:
        return f"LENGTH({e[0]})"
    if name == "MID" and len(args) == 3:
        return f"SUBSTRING({e[0]}, {e[1]}, {e[2]})"
    if name in _TEXT_PASSTHROUGH:
        return f"{name}(" + ", ".join(e) + ")"
    if name in ("CONCATENATE",) and len(args) == 2:
        return f"CONCAT({e[0]}, {e[1]})"
    if name == "COALESCE" and args:
        return "COALESCE(" + ", ".join(e) + ")"

    raise TranslateError(f"unsupported function {name}()")


def dax_to_sql(dax, *, column_map=None):
    """Translate a row-level DAX scalar expression to a Spark SQL scalar expression.

    ``dax``        -- the calc column's translated DAX (text after ``=`` in the TMDL).
    ``column_map`` -- optional ``{dax_column_name: physical_delta_column}`` mapping so the emitted
                      SQL references the real Lakehouse column names (e.g. ``Order_Date`` ->
                      ``Order Date``). Missing names fall back to the DAX name verbatim.

    Returns ``{"ok": bool, "sql": str|None, "reason": str}``. Never raises: an untranslatable
    expression yields ``ok=False`` with a human-readable ``reason`` (so the caller emits an explicit
    review TODO instead of wrong SQL). Correct-or-abstain.
    """
    text = "" if dax is None else str(dax).strip()
    if not text:
        return {"ok": False, "sql": None, "reason": "empty expression"}
    try:
        toks = _tokenize(text)
        if not toks:
            return {"ok": False, "sql": None, "reason": "empty expression"}
        ast = _Parser(toks).parse()
        return {"ok": True, "sql": _emit(ast, column_map or {}), "reason": ""}
    except TranslateError as exc:
        return {"ok": False, "sql": None, "reason": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive: never raise at the boundary
        return {"ok": False, "sql": None, "reason": f"translation error: {exc}"}


# --------------------------------------------------------------------------- materialization script
def build_table_view(table, columns, column_map=None, *, schema="dbo", suffix="_enriched"):
    """Assemble the upstream materialization for ONE table's ``materialize_upstream`` columns.

    ``table``      -- the display/table name (also the Delta table name).
    ``columns``    -- ``[{"name", "dax"}]`` for the columns routed to materialize-upstream.
    ``column_map`` -- optional ``{dax_name: physical}`` passed through to :func:`dax_to_sql`.
    ``schema`` / ``suffix`` -- the enriched table is ``schema.<table><suffix>``.

    Returns ``{"table", "view", "sql", "columns", "covered", "needs_manual"}``. ``sql`` is a Spark SQL
    ``CREATE OR REPLACE TABLE ... AS SELECT *`` that adds every faithfully-translatable column as a
    real Delta column; columns that could NOT be translated are listed as ``-- REVIEW`` comments in
    the same script (with their reason and original DAX) so the artifact is complete and honest --
    never silently dropping work. Never raises.
    """
    view = f"{table}{suffix}"
    rows, select_exprs, todos = [], [], []
    for col in columns or []:
        name, dax = col.get("name"), col.get("dax")
        res = dax_to_sql(dax, column_map=column_map)
        rows.append({"name": name, "sql": res["sql"], "ok": res["ok"], "reason": res["reason"]})
        if res["ok"]:
            select_exprs.append(f"    {res['sql']} AS `{name}`")
        else:
            todos.append(f"-- REVIEW [{name}]: {res['reason']}\n--   DAX: {str(dax).strip()}")

    src = f"`{schema}`.`{table}`" if schema else f"`{table}`"
    dst = f"`{schema}`.`{view}`" if schema else f"`{view}`"
    parts = []
    if select_exprs:
        body = ",\n".join(["    *"] + select_exprs)
        parts.append(
            f"-- Materialize row-level calculated columns upstream so Direct Lake reads them\n"
            f"-- natively as physical columns. Run in a Fabric Lakehouse notebook (Spark SQL),\n"
            f"-- then rebind the Direct Lake table to `{view}`.\n"
            f"CREATE OR REPLACE TABLE {dst} AS\nSELECT\n{body}\nFROM {src};"
        )
    if todos:
        parts.append("\n".join(todos))
    return {
        "table": table,
        "view": view if select_exprs else None,
        "sql": "\n\n".join(parts),
        "columns": rows,
        "covered": sum(1 for r in rows if r["ok"]),
        "needs_manual": sum(1 for r in rows if not r["ok"]),
    }


def build_materialization_script(stripped, *, model_name=None):
    """Consolidate every table's materialization SQL into ONE runnable Spark SQL script.

    ``stripped`` -- the seam's ``stripped_calc_columns`` list; each entry may carry a
    ``materialization`` dict (produced by :func:`build_table_view`). Entries that carried no
    materializable SQL are skipped.

    Returns ``{"sql", "tables", "covered", "needs_manual"}`` -- a single script that concatenates
    every table's ``CREATE OR REPLACE TABLE ... _enriched`` block (plus its ``-- REVIEW`` TODOs) under
    a run-order header -- or ``None`` when nothing across the estate is materializable (so the caller
    writes no empty artifact). Never raises.
    """
    blocks, tables, covered, needs_manual = [], 0, 0, 0
    for entry in stripped or []:
        if not isinstance(entry, dict):
            continue
        mat = entry.get("materialization")
        if not isinstance(mat, dict):
            continue
        sql = str(mat.get("sql") or "").strip()
        if not sql:
            continue
        tables += 1
        covered += int(mat.get("covered") or 0)
        needs_manual += int(mat.get("needs_manual") or 0)
        blocks.append(f"-- ===== Table: {mat.get('table') or entry.get('table')} =====\n{sql}")
    if not blocks:
        return None
    head = [
        "-- Direct Lake upstream materialization script",
        "-- Generated by the Tableau -> Fabric migration accelerator.",
    ]
    if model_name:
        head.append(f"-- Model: {model_name}")
    head += [
        "--",
        "-- Run this in a Fabric Lakehouse notebook (Spark SQL) BEFORE opening the semantic model,",
        "-- then rebind each Direct Lake table to its `<table>_enriched` counterpart. Columns that",
        "-- could not be translated faithfully are listed as -- REVIEW comments with their original",
        "-- DAX, so nothing is silently dropped.",
        "",
    ]
    return {
        "sql": "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n",
        "tables": tables,
        "covered": covered,
        "needs_manual": needs_manual,
    }
