"""Local reconciliation oracle -- the *empirical* half of the second-compiler validation gate.

``check_candidate_dax`` (in :mod:`translation_router`) is the SYNTACTIC gate: it proves a candidate
is well-formed DAX that is not the inert stub. It cannot prove the candidate is *numerically
faithful* to the original Tableau calc. That empirical proof is what this module provides, offline,
against the data a local run already lands as CSVs (the extract / flat-file tables).

The strategy is deliberately conservative -- a false PASS (landing wrong DAX with confidence) is the
worst possible outcome, strictly worse than leaving a stub. So the oracle:

  * parses BOTH the original Tableau formula AND the candidate DAX into ONE shared canonical AST
    (two independent front-ends -> a wrong candidate diverges; a shared evaluator means edge-case
    arithmetic semantics never make two *equivalent* expressions disagree),
  * evaluates both over the real landed rows at the grand total (plus any caller-supplied grain),
  * returns ``pass`` ONLY when both sides parse inside a tight supported subset, evaluate without
    error, and agree over the data; a mismatch is ``fail``; anything else (out of subset, no data,
    unresolved reference, multi-table) is ``inconclusive`` -- which, under faithful-or-stub, keeps
    the stub.

Supported subset: a measure that is an arithmetic combination (``+ - * /`` / ``DIVIDE``) of
single-column aggregations (``SUM/AVG/MIN/MAX/COUNT/COUNTD/MEDIAN`` and the ``*X`` row-iterator
forms) over ONE table, with ``ZN`` / ``IFNULL`` / ``COALESCE`` null handling and numeric literals.
The argmax/argmin-over-dimension idiom is handled separately by :func:`reconcile_argmax`. Everything
else -> ``inconclusive``.

Conditionals are in-subset too, because a census of a real 13-workbook estate found ``IF`` / ``CASE``
/ ``IIF`` in 38% of all calculated fields -- by a wide margin the single biggest reason a translation
went unproven. So the grammar also accepts Tableau ``IF/ELSEIF/ELSE/END``, ``IIF``, ``CASE/WHEN``,
the comparison operators, ``AND`` / ``OR`` / ``NOT``, and string/boolean literals, against DAX
``IF`` / ``SWITCH`` / ``BLANK`` / ``&&`` / ``||``. Both front-ends desugar onto ONE :class:`If` node,
so ``CASE x WHEN 1 THEN ...`` and ``SWITCH(x, 1, ...)`` -- or Tableau ``IF`` against DAX
``SWITCH(TRUE(), ...)`` -- produce identical ASTs and cannot disagree for spelling reasons alone.

Comparison is deliberately three-valued: when two operands cannot be put on a common type (say a
text column against a number) the comparison is *unknown*, not false, and an unknown condition takes
the ELSE branch on both sides. Unknown propagates to a null result, and :func:`reconcile` skips a
grain that is null on either side -- so an under-modelled comparison degrades to ``inconclusive``
rather than manufacturing agreement. That is the same faithful-or-stub bias as the rest of the file:
the cost of an unknown is a stub, the cost of a false PASS is wrong numbers in front of a customer.

Pure standard library (``csv`` + ``statistics``) so it runs everywhere and is fully unit-testable
offline; it never imports pandas/duckdb and never touches Tableau or Fabric.
"""
from __future__ import annotations

import csv
import math
import re
import statistics

PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"

# Aggregation function name (canonical) -> arity note. COUNTROWS is table-level (no column arg).
_AGG_FNS = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN", "COUNTROWS"}

# DAX function name -> canonical aggregation (single-column forms).
_DAX_AGG = {
    "SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
    "COUNT": "COUNT", "COUNTA": "COUNT", "DISTINCTCOUNT": "COUNTD", "MEDIAN": "MEDIAN",
}
# DAX row-iterator forms: FN(table, rowexpr) -> canonical aggregation over rowexpr.
_DAX_ITER = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT"}

# Tableau aggregation spellings -> canonical.
_TABLEAU_AGG = {
    "SUM": "SUM", "AVG": "AVG", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
    "COUNT": "COUNT", "COUNTD": "COUNTD", "MEDIAN": "MEDIAN",
}

# Comparison spellings -> canonical. Tableau writes ``=``/``<>``, DAX also accepts ``==``/``!=``.
_CMP_OPS = {"=": "=", "==": "=", "<>": "<>", "!=": "<>", ">": ">", "<": "<", ">=": ">=", "<=": "<="}


class _Unsupported(Exception):
    """Raised when a formula/candidate falls outside the oracle's supported subset."""


# --------------------------------------------------------------------------- canonical AST nodes
class Num:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = float(value)


class Col:
    """A row-level column reference ``(table, column)`` -- valid only inside an aggregation."""
    __slots__ = ("table", "column")

    def __init__(self, table, column):
        self.table = table
        self.column = column


class Agg:
    """A scalar aggregation over ``arg`` (a row-level expression); ``COUNTROWS`` has ``arg=None``."""
    __slots__ = ("fn", "arg", "table")

    def __init__(self, fn, arg, table=None):
        self.fn = fn
        self.arg = arg
        self.table = table


class Bin:
    __slots__ = ("op", "left", "right", "alt")

    def __init__(self, op, left, right, alt=None):
        self.op = op
        self.left = left
        self.right = right
        self.alt = alt  # DIVIDE's optional alternate result when the denominator is 0/blank


class Coalesce:
    """``ZN`` / ``IFNULL`` / ``COALESCE`` -- first non-null of its operands."""
    __slots__ = ("args",)

    def __init__(self, args):
        self.args = args


class Str:
    """A string literal -- only ever meaningful as a comparison operand or a branch result."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = str(value)


class Bool:
    """``TRUE`` / ``FALSE`` (Tableau) and ``TRUE()`` / ``FALSE()`` (DAX)."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = bool(value)


class Null:
    """DAX ``BLANK()`` -- an explicit null, so ``IF(c, x)`` and ``IF(c, x, BLANK())`` agree."""
    __slots__ = ()


class Cmp:
    """A comparison. Evaluates to True/False, or None when the operands are not comparable."""
    __slots__ = ("op", "left", "right")

    def __init__(self, op, left, right):
        self.op = op
        self.left = left
        self.right = right


class Logic:
    """``AND`` / ``OR`` / ``NOT`` under three-valued logic (None means unknown)."""
    __slots__ = ("op", "args")

    def __init__(self, op, args):
        self.op = op
        self.args = list(args)


class If:
    """The single conditional node both front-ends desugar onto.

    ``branches`` is an ordered list of ``(condition, value)``; ``alt`` is the ELSE value or None.
    Tableau ``IF``/``ELSEIF``, ``IIF`` and ``CASE``/``WHEN``, and DAX ``IF`` and ``SWITCH`` (both the
    value form and the ``SWITCH(TRUE(), ...)`` idiom) all land here, so an equivalent pair cannot
    diverge merely because the two dialects spell the same branch differently.
    """
    __slots__ = ("branches", "alt")

    def __init__(self, branches, alt=None):
        self.branches = list(branches)
        self.alt = alt


# --------------------------------------------------------------------------- tokenizer
_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<number>\d+\.\d+|\.\d+|\d+)
    | (?P<dquoted>"(?:[^"]|"")*")         # "text" -- a string literal in both dialects
    | (?P<quoted>'(?:[^']|'')*')          # 'Table Name' (DAX) / 'text' (Tableau) -- front-end decides
    | (?P<bracket>\[(?:[^\]]|\]\])*\])    # [Column]/[Field]/[Measure]
    | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)   # function name, keyword, or bare table name
    | (?P<op><=|>=|<>|!=|==|&&|\|\||[()+\-*/,<>=])
    """,
    re.VERBOSE,
)


def _tokenize(text):
    tokens = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise _Unsupported("unrecognized character %r" % text[pos])
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        val = m.group()
        if kind == "quoted":
            val = val[1:-1].replace("''", "'")
        elif kind == "dquoted":
            val = val[1:-1].replace('""', '"')
        elif kind == "bracket":
            val = val[1:-1].replace("]]", "]")
        tokens.append((kind, val))
    return tokens


class _Parser:
    """Shared recursive-descent core for the tiny arithmetic-of-aggregations grammar.

    Subclasses supply :meth:`_column` (how a column reference is spelled + resolved) and the set of
    recognized function names; everything else (precedence, aggregation shape, null-handling
    wrappers) is common so the Tableau and DAX front-ends produce byte-identical ASTs for equivalent
    expressions.
    """

    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def _next(self):
        tok = self._peek()
        self.i += 1
        return tok

    def _expect_op(self, op):
        kind, val = self._next()
        if kind != "op" or val != op:
            raise _Unsupported("expected %r" % op)

    def _expect_word(self, word):
        kind, val = self._next()
        if kind != "name" or val.upper() != word:
            raise _Unsupported("expected %s" % word)

    def _peek_word(self):
        """The upper-cased keyword at the cursor, or None -- lets a clause spot its terminator."""
        kind, val = self._peek()
        return val.upper() if kind == "name" else None

    def parse(self):
        node = self._expr()
        if self.i != len(self.toks):
            raise _Unsupported("trailing tokens")
        return node

    # Precedence, loosest first: OR < AND < NOT < comparison < +- < */ < factor. This matches both
    # Tableau and DAX, so the same source text binds the same way through either front-end.
    def _expr(self):
        return self._or_expr()

    def _take_logic(self, word):
        """Consume ``AND``/``OR`` in either spelling (word form, or DAX ``&&``/``||``)."""
        kind, val = self._peek()
        if kind == "name" and val.upper() == word:
            self._next()
            return True
        if kind == "op" and val == ("&&" if word == "AND" else "||"):
            self._next()
            return True
        return False

    def _or_expr(self):
        node = self._and_expr()
        while self._take_logic("OR"):
            node = Logic("OR", [node, self._and_expr()])
        return node

    def _and_expr(self):
        node = self._not_expr()
        while self._take_logic("AND"):
            node = Logic("AND", [node, self._not_expr()])
        return node

    def _not_expr(self):
        if self._peek_word() == "NOT":
            self._next()
            return Logic("NOT", [self._not_expr()])
        return self._cmp_expr()

    def _cmp_expr(self):
        node = self._add_expr()
        kind, val = self._peek()
        if kind == "op" and val in _CMP_OPS:
            self._next()
            # Non-associative on purpose: ``a < b < c`` is a modelling mistake in both dialects, and
            # chaining it would silently compare a boolean against a number.
            return Cmp(_CMP_OPS[val], node, self._add_expr())
        return node

    def _add_expr(self):
        node = self._term()
        while True:
            kind, val = self._peek()
            if kind == "op" and val in ("+", "-"):
                self._next()
                node = Bin(val, node, self._term())
            else:
                return node

    def _term(self):
        node = self._factor()
        while True:
            kind, val = self._peek()
            if kind == "op" and val in ("*", "/"):
                self._next()
                node = Bin(val, node, self._factor())
            else:
                return node

    def _factor(self):
        kind, val = self._peek()
        if kind == "op" and val == "-":
            self._next()
            return Bin("-", Num(0), self._factor())
        if kind == "op" and val == "+":
            self._next()
            return self._factor()
        if kind == "op" and val == "(":
            self._next()
            node = self._expr()
            self._expect_op(")")
            return node
        if kind == "number":
            self._next()
            return Num(val)
        if kind == "dquoted":
            self._next()
            return Str(val)
        if kind == "name" and val.upper() in ("TRUE", "FALSE"):
            # Bare ``TRUE``/``FALSE`` (Tableau) and the call form ``TRUE()``/``FALSE()`` (DAX) are
            # the same literal; accepting both here is what lets ``CASE [x] WHEN TRUE ...`` line up
            # with ``SWITCH(TRUE(), ...)``.
            self._next()
            nkind, nval = self._peek()
            if nkind == "op" and nval == "(":
                self._next()
                self._expect_op(")")
            return Bool(val.upper() == "TRUE")
        if kind == "name":
            return self._name_lead()
        if kind in ("bracket", "quoted"):
            # A bare column at scalar position is not a measure -- reject (keeps top-level scalar).
            raise _Unsupported("bare column at scalar position")
        raise _Unsupported("unexpected token %r" % (val,))

    def _args(self):
        """Parse ``( a, b, ... )`` -> list of row/scalar expressions."""
        self._expect_op("(")
        args = []
        kind, val = self._peek()
        if kind == "op" and val == ")":
            self._next()
            return args
        args.append(self._expr())
        while True:
            kind, val = self._peek()
            if kind == "op" and val == ",":
                self._next()
                args.append(self._expr())
            elif kind == "op" and val == ")":
                self._next()
                return args
            else:
                raise _Unsupported("malformed argument list")

    # -- hooks overridden per front-end -----------------------------------------------------------
    def _name_lead(self):  # pragma: no cover - overridden
        raise _Unsupported("name")


def _row_expr_ok(node):
    """A validated row-level expression -- anything but an aggregation nested inside one.

    Walks via :func:`_children` rather than re-listing the node types, so a node added to the AST
    later cannot slip past this check by being forgotten here.
    """
    if isinstance(node, Agg):
        raise _Unsupported("aggregation nested inside an aggregation")
    for child in _children(node):
        _row_expr_ok(child)
    return node


class _DaxParser(_Parser):
    def __init__(self, tokens, default_table=None):
        super().__init__(tokens)
        self.default_table = default_table

    def _column_from(self, table):
        kind, val = self._next()
        if kind != "bracket":
            raise _Unsupported("expected [column]")
        return Col(table, val)

    def _name_lead(self):
        kind, val = self._next()
        upper = val.upper()
        nxt_kind, nxt_val = self._peek()
        if nxt_kind == "op" and nxt_val == "(":
            return self._func(upper)
        if nxt_kind == "bracket":
            # Table[Column]
            return self._column_from(val)
        raise _Unsupported("bare name %r" % val)

    def _func(self, fn):
        if fn in _DAX_AGG:
            args = self._args()
            if len(args) != 1:
                raise _Unsupported("%s expects one column" % fn)
            return Agg(_DAX_AGG[fn], _row_expr_ok(args[0]))
        if fn in _DAX_ITER:
            # FN(table, rowexpr): the first arg is a bare table reference, not 'Table'[Column].
            self._expect_op("(")
            tkind, tval = self._next()
            if tkind not in ("quoted", "name"):
                raise _Unsupported("%s expects (table, expr)" % fn)
            self._expect_op(",")
            saved = self.default_table
            self.default_table = tval  # bare [Col] in the row expr resolves against this table
            try:
                expr = self._expr()
            finally:
                self.default_table = saved
            self._expect_op(")")
            return Agg(_DAX_ITER[fn], _row_expr_ok(expr))
        if fn == "COUNTROWS":
            args = self._args_tableref()
            return Agg("COUNTROWS", None, table=args)
        if fn == "DIVIDE":
            args = self._args()
            if len(args) not in (2, 3):
                raise _Unsupported("DIVIDE expects 2 or 3 args")
            return Bin("/", args[0], args[1], alt=(args[2] if len(args) == 3 else None))
        if fn == "COALESCE":
            args = self._args()
            if not args:
                raise _Unsupported("COALESCE expects args")
            return Coalesce(args)
        if fn == "IF":
            args = self._args()
            if len(args) not in (2, 3):
                raise _Unsupported("IF expects 2 or 3 args")
            return If([(args[0], args[1])], args[2] if len(args) == 3 else None)
        if fn == "SWITCH":
            return self._switch()
        if fn == "BLANK":
            self._expect_op("(")
            self._expect_op(")")
            return Null()
        if fn == "NOT":
            args = self._args()
            if len(args) != 1:
                raise _Unsupported("NOT expects one arg")
            return Logic("NOT", args)
        if fn in ("AND", "OR"):
            args = self._args()
            if len(args) != 2:
                raise _Unsupported("%s expects two args" % fn)
            return Logic(fn, args)
        raise _Unsupported("unsupported DAX function %r" % fn)

    def _switch(self):
        """``SWITCH(expr, v1, r1, ..., [default])`` -> the shared :class:`If`.

        The ``SWITCH(TRUE(), cond1, r1, ...)`` idiom is recognised and desugared to bare conditions
        rather than ``TRUE() = cond1``. Both forms are legal DAX and the translator emits either
        depending on the shape of the source calc, so collapsing them here is what stops an
        ``IF``-vs-``SWITCH`` spelling difference from reading as a numeric disagreement.
        """
        args = self._args()
        if len(args) < 3:
            raise _Unsupported("SWITCH expects at least 3 args")
        subject, rest = args[0], list(args[1:])
        alt = rest.pop() if len(rest) % 2 else None
        switch_true = isinstance(subject, Bool) and subject.value
        branches = []
        for i in range(0, len(rest) - 1, 2):
            test, result = rest[i], rest[i + 1]
            branches.append((test if switch_true else Cmp("=", subject, test), result))
        if not branches:
            raise _Unsupported("SWITCH with no branches")
        return If(branches, alt)

    def _args_tableref(self):
        """COUNTROWS('Table') / COUNTROWS(Table) -> the referenced table name."""
        self._expect_op("(")
        kind, val = self._next()
        if kind not in ("quoted", "name"):
            raise _Unsupported("COUNTROWS expects a table")
        self._expect_op(")")
        return val

    def _factor(self):
        # Support 'Table'[Column] (quoted table lead) before the shared factor rules.
        kind, val = self._peek()
        if kind == "quoted":
            self._next()
            return self._column_from(val)
        if kind == "bracket":
            # bare [Column] inside a *X iterator -> resolve against the default table.
            if self.default_table is None:
                raise _Unsupported("bare [column] with no table context")
            _, col = self._next()
            return Col(self.default_table, col)
        return super()._factor()


class _TableauParser(_Parser):
    def __init__(self, tokens, resolver):
        super().__init__(tokens)
        self.resolver = resolver

    def _resolve(self, caption):
        if self.resolver is None:
            raise _Unsupported("no resolver for [%s]" % caption)
        hit = self.resolver(caption)
        if not hit or len(hit) < 2 or not hit[0] or not hit[1]:
            raise _Unsupported("unresolved field [%s]" % caption)
        return Col(hit[0], hit[1])

    def _name_lead(self):
        word = self._peek_word()
        if word == "IF":
            self._next()
            return self._parse_if()
        if word == "CASE":
            self._next()
            return self._parse_case()
        kind, val = self._next()
        upper = val.upper()
        nxt_kind, nxt_val = self._peek()
        if nxt_kind == "op" and nxt_val == "(":
            return self._func(upper)
        raise _Unsupported("bare name %r" % val)

    def _parse_if(self):
        """``IF c THEN v [ELSEIF c THEN v]* [ELSE v] END`` -- the ``IF`` token is already consumed.

        A nested ``ELSE IF ... END END`` needs no special case: the ELSE value is parsed as a full
        expression, and ``IF`` is a valid expression lead.
        """
        branches = []
        cond = self._expr()
        self._expect_word("THEN")
        branches.append((cond, self._expr()))
        while True:
            word = self._peek_word()
            if word == "ELSEIF":
                self._next()
                cond = self._expr()
                self._expect_word("THEN")
                branches.append((cond, self._expr()))
            elif word == "ELSE":
                self._next()
                alt = self._expr()
                self._expect_word("END")
                return If(branches, alt)
            elif word == "END":
                self._next()
                return If(branches, None)
            else:
                raise _Unsupported("malformed IF (expected ELSEIF/ELSE/END)")

    def _parse_case(self):
        """``CASE subject WHEN v THEN r [...] [ELSE d] END`` -> ``If`` over ``subject = v`` tests.

        Desugaring to the same node DAX ``SWITCH`` produces is the whole point: the translator is
        free to emit either shape and the oracle still sees one AST on both sides.
        """
        subject = self._expr()
        branches = []
        alt = None
        while True:
            word = self._peek_word()
            if word == "WHEN":
                self._next()
                test = self._expr()
                self._expect_word("THEN")
                branches.append((Cmp("=", subject, test), self._expr()))
            elif word == "ELSE":
                self._next()
                alt = self._expr()
                self._expect_word("END")
                break
            elif word == "END":
                self._next()
                break
            else:
                raise _Unsupported("malformed CASE (expected WHEN/ELSE/END)")
        if not branches:
            raise _Unsupported("CASE with no WHEN branch")
        return If(branches, alt)

    def _func(self, fn):
        if fn in _TABLEAU_AGG:
            args = self._args()
            if len(args) != 1:
                raise _Unsupported("%s expects one expression" % fn)
            return Agg(_TABLEAU_AGG[fn], _row_expr_ok(args[0]))
        if fn == "ZN":
            args = self._args()
            if len(args) != 1:
                raise _Unsupported("ZN expects one arg")
            return Coalesce([args[0], Num(0)])
        if fn == "IFNULL":
            args = self._args()
            if len(args) != 2:
                raise _Unsupported("IFNULL expects two args")
            return Coalesce(args)
        if fn == "IIF":
            # The 4-argument form takes a separate value for an UNKNOWN condition, which the shared
            # If node has no way to represent -- so it stays out of subset rather than being
            # approximated by the 3-argument semantics.
            args = self._args()
            if len(args) != 3:
                raise _Unsupported("IIF expects three args")
            return If([(args[0], args[1])], args[2])
        raise _Unsupported("unsupported Tableau function %r" % fn)

    def _factor(self):
        kind, val = self._peek()
        if kind == "bracket":
            self._next()
            return self._resolve(val)
        if kind == "quoted":
            # Tableau single-quotes are string literals; only the DAX front-end reads them as tables.
            self._next()
            return Str(val)
        return super()._factor()


# --------------------------------------------------------------------------- evaluation
def _to_number(value):
    """Coerce a cell to float, or None when it is blank/non-numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        f = float(value)
        return None if math.isnan(f) else f
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_bool(value):
    """Coerce a raw value to True/False, or None when it is not a recognisable boolean."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and math.isnan(value)) else value != 0
    s = str(value).strip().lower()
    if s in ("true", "t", "yes", "1"):
        return True
    if s in ("false", "f", "no", "0"):
        return False
    return None


def _truthy(value):
    """Branch selection. An unknown condition is NOT taken -- it falls through to ELSE.

    Tableau (``IF NULL THEN a ELSE b END``) and DAX (``IF(BLANK(), a, b)``) agree on that, so the
    shared evaluator does too.
    """
    return _to_bool(value) is True


def _comparable(a, b):
    """Put two raw values on one comparable type, or return None meaning 'cannot be compared'.

    Boolean-ness wins first (so a landed ``"True"`` cell lines up with a literal ``TRUE``), then a
    numeric reading of both sides, then a case-insensitive text comparison -- which matches both
    Tableau's and DAX's default text collation. A number against non-numeric text is *unknown*
    rather than false: guessing an ordering there is exactly how a checker invents agreement.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        x, y = _to_bool(a), _to_bool(b)
        return None if x is None or y is None else (x, y)
    na, nb = _to_number(a), _to_number(b)
    if na is not None and nb is not None:
        return (na, nb)
    if isinstance(a, str) and isinstance(b, str):
        return (a.strip().casefold(), b.strip().casefold())
    return None


def _compare(op, a, b):
    if a is None or b is None:
        return None
    pair = _comparable(a, b)
    if pair is None:
        return None
    x, y = pair
    if op == "=":
        return x == y
    if op == "<>":
        return x != y
    if op == ">":
        return x > y
    if op == "<":
        return x < y
    if op == ">=":
        return x >= y
    if op == "<=":
        return x <= y
    raise _Unsupported("comparison %r" % op)


def _logic(op, values):
    """Three-valued AND/OR/NOT: unknown only survives when it can still change the answer."""
    vals = [_to_bool(v) for v in values]
    if op == "NOT":
        v = vals[0] if vals else None
        return None if v is None else (not v)
    if op == "AND":
        if any(v is False for v in vals):
            return False
        return None if any(v is None for v in vals) else True
    if op == "OR":
        if any(v is True for v in vals):
            return True
        return None if any(v is None for v in vals) else False
    raise _Unsupported("logical operator %r" % op)


def _eval_row_raw(node, row):
    """Evaluate a row-level expression to its RAW value (float, str, bool or None).

    Comparisons need the uncoerced cell -- ``[Category] = "Furniture"`` is meaningless once the
    cell has been forced through :func:`_to_number`. :func:`_eval_row` is this function plus that
    coercion, so the arithmetic path is unchanged.
    """
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Bool):
        return node.value
    if isinstance(node, Null):
        return None
    if isinstance(node, Col):
        v = row.get(node.column)
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v
    if isinstance(node, Coalesce):
        for a in node.args:
            v = _eval_row_raw(a, row)
            if v is not None:
                return v
        return None
    if isinstance(node, Cmp):
        return _compare(node.op, _eval_row_raw(node.left, row), _eval_row_raw(node.right, row))
    if isinstance(node, Logic):
        return _logic(node.op, [_eval_row_raw(a, row) for a in node.args])
    if isinstance(node, If):
        for cond, value in node.branches:
            if _truthy(_eval_row_raw(cond, row)):
                return _eval_row_raw(value, row)
        return _eval_row_raw(node.alt, row) if node.alt is not None else None
    if isinstance(node, Bin):
        return _combine(node, _eval_row(node.left, row), _eval_row(node.right, row))
    raise _Unsupported("non row-level node in row context")


def _eval_row(node, row):
    """Evaluate a row-level expression against a single row dict -> float or None."""
    return _to_number(_eval_row_raw(node, row))


def _raw_cell(node, row):
    """Raw (uncoerced) cell for COUNT/COUNTD, which count non-blank values incl. text."""
    v = _eval_row_raw(node, row)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _combine(node, a, b):
    op = node.op
    if op == "/":
        if b is None or b == 0:
            return node.alt.value if isinstance(node.alt, Num) else None
        if a is None:
            return None
        return a / b
    if a is None or b is None:
        return None
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    raise _Unsupported("op %r" % op)


def _aggregate(node, rows):
    fn = node.fn
    if fn == "COUNTROWS":
        return float(len(rows))
    if fn in ("COUNT", "COUNTD"):
        vals = [_raw_cell(node.arg, r) for r in rows]
        vals = [v for v in vals if v is not None]
        return float(len(set(vals))) if fn == "COUNTD" else float(len(vals))
    vals = [_eval_row(node.arg, r) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    if fn == "SUM":
        return float(sum(vals))
    if fn == "AVG":
        return statistics.fmean(vals)
    if fn == "MIN":
        return float(min(vals))
    if fn == "MAX":
        return float(max(vals))
    if fn == "MEDIAN":
        return float(statistics.median(vals))
    raise _Unsupported("aggregation %r" % fn)


def _eval_scalar(node, rows):
    """Evaluate a scalar measure AST over a set of rows -> float, str, bool or None."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Bool):
        return node.value
    if isinstance(node, Null):
        return None
    if isinstance(node, Agg):
        return _aggregate(node, rows)
    if isinstance(node, Coalesce):
        for a in node.args:
            v = _eval_scalar(a, rows)
            if v is not None:
                return v
        return None
    if isinstance(node, Cmp):
        return _compare(node.op, _eval_scalar(node.left, rows), _eval_scalar(node.right, rows))
    if isinstance(node, Logic):
        return _logic(node.op, [_eval_scalar(a, rows) for a in node.args])
    if isinstance(node, If):
        for cond, value in node.branches:
            if _truthy(_eval_scalar(cond, rows)):
                return _eval_scalar(value, rows)
        return _eval_scalar(node.alt, rows) if node.alt is not None else None
    if isinstance(node, Bin):
        return _combine(node, _scalar_num(node.left, rows), _scalar_num(node.right, rows))
    if isinstance(node, Col):
        raise _Unsupported("bare column is not a measure")
    raise _Unsupported("unsupported node")


def _scalar_num(node, rows):
    """Scalar arithmetic operand, coerced to a number -- a branch may have returned text."""
    v = _eval_scalar(node, rows)
    return _to_number(v) if isinstance(v, (str, bool)) else v


# --------------------------------------------------------------------------- table / verdict utils
def _normalize_tables(tables):
    out = {}
    for name, tbl in (tables or {}).items():
        if isinstance(tbl, dict) and "rows" in tbl:
            rows = list(tbl["rows"])
            cols = list(tbl.get("columns") or (rows[0].keys() if rows else []))
        else:  # a bare list of row dicts
            rows = list(tbl)
            cols = list(rows[0].keys()) if rows else []
        out[name] = {"columns": cols, "rows": rows}
    return out


def _children(node):
    """Every sub-node of ``node``.

    One place, deliberately: :func:`_tables_of`, :func:`_columns_of` and :func:`_row_expr_ok` all
    walk through here. If a new node type were added to the AST and forgotten in a bespoke walker,
    :func:`_single_table_of` could miss a second table and the oracle would then evaluate a
    two-table expression against one table's rows -- a manufactured disagreement, the one failure
    mode this module exists to avoid.
    """
    if isinstance(node, (Bin, Cmp)):
        return [node.left, node.right]
    if isinstance(node, (Coalesce, Logic)):
        return list(node.args)
    if isinstance(node, If):
        out = []
        for cond, value in node.branches:
            out.append(cond)
            out.append(value)
        if node.alt is not None:
            out.append(node.alt)
        return out
    if isinstance(node, Agg):
        return [node.arg] if node.arg is not None else []
    return []


def _tables_of(node, acc):
    if getattr(node, "table", None):
        acc.add(node.table)
    for child in _children(node):
        _tables_of(child, acc)
    return acc


def _columns_of(node, acc):
    """Column names referenced (as measure inputs) by an AST -- excluded from auto-grain dims."""
    if isinstance(node, Col) and node.column:
        acc.add(node.column)
    for child in _children(node):
        _columns_of(child, acc)
    return acc


def _single_table_of(node):
    tables = _tables_of(node, set())
    if len(tables) == 1:
        return next(iter(tables))
    return None


def _auto_grain(tbl, used, *, max_cols=8, max_distinct=50):
    """Pick low-cardinality, non-measure columns to reconcile per group.

    Without this, a candidate that only *coincidentally* equals the Tableau formula at the grand
    total would PASS. Grouping by each dimension independently makes such a coincidence hold across
    many groups -- astronomically unlikely for two genuinely different functions over real data --
    so a wrong candidate is caught. A truly-equivalent candidate agrees at every grain, so PASS is
    never lost.
    """
    rows = tbl["rows"]
    limit = min(max_distinct, max(2, len(rows) // 2))
    out = []
    for c in tbl["columns"]:
        if c in used:
            continue
        seen = set()
        ok = True
        for r in rows:
            v = r.get(c)
            if v is None:
                continue
            s = v.strip() if isinstance(v, str) else v
            if s == "":
                continue
            seen.add(s)
            if len(seen) > limit:
                ok = False
                break
        if ok and 2 <= len(seen) <= limit:
            out.append(c)
        if len(out) >= max_cols:
            break
    return out


def _num_eq(a, b, tol):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    fa, fb = float(a), float(b)
    if math.isnan(fa) and math.isnan(fb):
        return True
    return abs(fa - fb) <= tol + tol * max(abs(fa), abs(fb))


def _verdict(status, reason, **extra):
    v = {"status": status, "reason": reason}
    v.update(extra)
    return v


def _resolve_dims(grain, resolver, table, tbl):
    """Map a grain spec (captions or (table,col)) to column names in ``table``."""
    dims = []
    for item in grain:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            t, c = item[0], item[1]
        elif resolver is not None:
            hit = resolver(item)
            if not hit or len(hit) < 2:
                raise _Unsupported("unresolved grain dimension %r" % (item,))
            t, c = hit[0], hit[1]
        else:
            t, c = table, item
        if t != table:
            raise _Unsupported("grain dimension %r not in table %r" % (c, table))
        if c not in tbl["columns"] and (not tbl["rows"] or c not in tbl["rows"][0]):
            raise _Unsupported("grain column %r missing from landed data" % c)
        dims.append(c)
    return dims


def _group_rows(rows, dims):
    groups = {}
    for r in rows:
        key = tuple(r.get(d) for d in dims)
        groups.setdefault(key, []).append(r)
    return groups


def reconcile(tableau_formula, candidate_dax, tables, *, resolver=None, grain=None,
              tolerance=1e-9, max_rows=500000):
    """Empirically reconcile ``candidate_dax`` against ``tableau_formula`` over landed ``tables``.

    ``tables`` maps a model table name to either ``{"columns": [...], "rows": [ {col: val} ]}`` or a
    bare list of row dicts. ``resolver(caption) -> (table, column, ...)`` maps a Tableau field
    caption to its model column (needed for the Tableau side). ``grain`` optionally lists dimensions
    (captions or ``(table, column)``) to also reconcile per group.

    Returns a verdict dict ``{"status": pass|fail|inconclusive, "reason": str, ...}``. ``pass`` means
    both sides parsed inside the supported subset and agreed over the real data at every evaluated
    grain; ``fail`` means they diverged; ``inconclusive`` means the oracle could not decide (out of
    subset, missing/empty data, unresolved or multi-table references) -- keep the stub.
    """
    tables = _normalize_tables(tables)

    try:
        t_ast = _TableauParser(_tokenize(tableau_formula or ""), resolver).parse()
    except _Unsupported as exc:
        return _verdict(INCONCLUSIVE, "tableau formula outside oracle subset: %s" % exc)
    t_tbl = _single_table_of(t_ast)
    try:
        d_ast = _DaxParser(_tokenize(candidate_dax or ""), default_table=t_tbl).parse()
    except _Unsupported as exc:
        return _verdict(INCONCLUSIVE, "candidate DAX outside oracle subset: %s" % exc)

    d_tbl = _single_table_of(d_ast)
    if t_tbl is None or d_tbl is None:
        # Zero and multiple are very different problems -- an expression over no column at all is
        # unreconcilable in principle, whereas a genuinely cross-table one is a coverage gap worth
        # closing. Reporting them under one label hides which of the two an estate actually has.
        n_t, n_d = len(_tables_of(t_ast, set())), len(_tables_of(d_ast, set()))
        if not n_t and not n_d:
            return _verdict(INCONCLUSIVE, "measure references no table at all (nothing to evaluate over)")
        return _verdict(
            INCONCLUSIVE,
            "measure references multiple tables (tableau=%d, candidate=%d; the oracle is single-table)"
            % (n_t, n_d))
    if t_tbl != d_tbl:
        return _verdict(
            INCONCLUSIVE,
            "candidate references table %r but the Tableau formula references %r" % (d_tbl, t_tbl))

    tbl = tables.get(t_tbl)
    if not tbl or not tbl["rows"]:
        return _verdict(INCONCLUSIVE, "no landed data for table %r" % t_tbl)
    if len(tbl["rows"]) > max_rows:
        return _verdict(INCONCLUSIVE, "table %r has more than max_rows=%d rows" % (t_tbl, max_rows))
    rows = tbl["rows"]

    try:
        dims = _resolve_dims(grain, resolver, t_tbl, tbl) if grain else []
    except _Unsupported as exc:
        return _verdict(INCONCLUSIVE, "grain outside oracle subset: %s" % exc)

    # Grand total is always compared; either the caller's grain (one combined grouping) or, when
    # none was given, each auto-picked dimension on its own strengthens it against a coincidental
    # total-only match.
    groupings = [[]]
    if dims:
        groupings.append(dims)
    else:
        used = _columns_of(t_ast, set()) | _columns_of(d_ast, set())
        groupings.extend([c] for c in _auto_grain(tbl, used))

    comparable = 0
    for gcols in groupings:
        grouped = _group_rows(rows, gcols) if gcols else {(): rows}
        for key, grp in sorted(grouped.items(), key=lambda kv: repr(kv[0])):
            tv = _safe_scalar(t_ast, grp)
            dv = _safe_scalar(d_ast, grp)
            if tv is _EVAL_ERROR or dv is _EVAL_ERROR:
                return _verdict(INCONCLUSIVE, "evaluation error over table %r" % t_tbl)
            if tv is None or dv is None:
                continue  # skip a grain that is null on either side (e.g. an empty group)
            comparable += 1
            if not _num_eq(tv, dv, tolerance):
                where = "grand total" if not gcols else "%s=%r" % (", ".join(gcols), key)
                return _verdict(
                    FAIL,
                    "values differ at %s: tableau=%r candidate=%r" % (where, tv, dv),
                    grain=where, tableau_value=tv, candidate_value=dv,
                    rows=len(rows), groups_compared=comparable)
    if comparable == 0:
        return _verdict(INCONCLUSIVE, "no comparable (non-null) grain to reconcile against")
    return _verdict(
        PASS,
        "candidate matches the Tableau formula over %d row(s) across %d grain(s)"
        % (len(rows), comparable),
        rows=len(rows), groups_compared=comparable)


# --------------------------------------------------------------------------- landed-CSV loader
def load_tables_from_csv(table_csv_paths, column_map=None, *, max_rows=None, encoding="utf-8-sig"):
    """Load landed CSV files into the ``tables`` shape :func:`reconcile` consumes.

    ``table_csv_paths`` maps a model table name to the path of the CSV a local run landed for it
    (``migrate_estate`` writes these under ``out/data/<ds>/``). Each CSV's header row supplies the
    column names; every value is kept as the raw string ``csv`` yields (the evaluator coerces to a
    number only where an aggregation needs one, so text dimensions survive for grouping).

    A landed CSV's headers are the extract's *physical* names, whereas the resolver and the candidate
    DAX speak the model's *sanitized* column names. ``column_map`` -- an optional
    ``{model_table: {csv_header: model_column}}`` -- renames headers to those model names so both
    sides line up; a header absent from the map (or a table with no map) is kept verbatim. Building
    that map from the datasource descriptor is the wiring layer's job, so this stays a pure,
    stdlib-only primitive.

    ``max_rows`` optionally caps rows read per table (a light guard for very large extracts). Returns
    ``{model_table: {"columns": [...], "rows": [ {col: val} ]}}`` ready to hand straight to
    :func:`reconcile`.
    """
    tables = {}
    for table, path in (table_csv_paths or {}).items():
        rename = (column_map or {}).get(table) or {}
        with open(path, "r", encoding=encoding, newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                tables[table] = {"columns": [], "rows": []}
                continue
            columns = [rename.get(h, h) for h in header]
            rows = []
            for raw in reader:
                if max_rows is not None and len(rows) >= max_rows:
                    break
                # tolerate ragged rows: pad short, ignore overflow cells
                row = {}
                for i, col in enumerate(columns):
                    row[col] = raw[i] if i < len(raw) else None
                rows.append(row)
        tables[table] = {"columns": columns, "rows": rows}
    return tables


_EVAL_ERROR = object()


def _safe_scalar(node, rows):
    try:
        value = _eval_scalar(node, rows)
    except _Unsupported:
        return _EVAL_ERROR
    # A conditional can hand back text or a boolean. Numeric reconciliation has nothing to say about
    # a string measure, so coerce: a boolean becomes 1/0, non-numeric text becomes None, and
    # reconcile() skips a grain that is None on either side rather than comparing incomparables.
    if isinstance(value, (str, bool)):
        return _to_number(value)
    return value
