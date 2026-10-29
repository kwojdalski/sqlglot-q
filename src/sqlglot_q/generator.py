"""Generate q from a sqlglot syntax tree.

q is not SQL with different spellings, so this does not lean on sqlglot's SQL
generator at all: every node type has its own `q_<key>` method, and a node
without one is reported as unsupported rather than passed through as SQL text
that q would refuse, or worse, read as something else.

Three facts about q shape most of what follows (docs/design.md has the rest):

- q evaluates right to left with no operator precedence, so `a*b+c` is
  `a*(b+c)`. Every compound operand is parenthesised.
- A qSQL clause is cut at its commas, so a comma can only appear inside
  brackets or parentheses.
- A SQL string literal is usually a symbol in q (`` `abc ``), and that is the
  default; `string_literals="string"` makes them char vectors instead.
"""

from __future__ import annotations

import json
import re
from importlib import resources

from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.generator import Generator

RESERVED = frozenset(json.loads(resources.files(__package__).joinpath("reserved.json").read_text()))

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
SYMBOL = re.compile(r"[A-Za-z0-9_.:]*\Z")

# The SQL comparison and arithmetic operators that q spells with one glyph.
GLYPHS = {
    exp.EQ: "=",
    exp.NEQ: "<>",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.Add: "+",
    exp.Sub: "-",
    exp.Mul: "*",
    exp.Div: "%",
    exp.And: "&",
    exp.Or: "|",
}

COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)

# SQL aggregates whose q keyword skips nulls the way SQL does.
AGGREGATES = {
    exp.Sum: "sum",
    exp.Avg: "avg",
    exp.Min: "min",
    exp.Max: "max",
}

MONADIC = {
    exp.Abs: "abs",
    exp.Upper: "upper",
    exp.Lower: "lower",
    exp.Floor: "floor",
    exp.Ceil: "ceiling",
    exp.Sqrt: "sqrt",
    exp.Exp: "exp",
    exp.Ln: "log",
}

# SQL type names to the q type each casts to, and the empty list a new column
# of that type starts as. Text is a symbol unless string_literals="string".
TYPES = {
    exp.DataType.Type.BOOLEAN: ("boolean", "`boolean$()"),
    exp.DataType.Type.TINYINT: ("short", "`short$()"),
    exp.DataType.Type.SMALLINT: ("short", "`short$()"),
    exp.DataType.Type.INT: ("int", "`int$()"),
    exp.DataType.Type.BIGINT: ("long", "`long$()"),
    exp.DataType.Type.FLOAT: ("real", "`real$()"),
    exp.DataType.Type.DOUBLE: ("float", "`float$()"),
    exp.DataType.Type.DECIMAL: ("float", "`float$()"),
    exp.DataType.Type.DATE: ("date", "`date$()"),
    exp.DataType.Type.TIMESTAMP: ("timestamp", "`timestamp$()"),
    exp.DataType.Type.TIMESTAMPNTZ: ("timestamp", "`timestamp$()"),
    exp.DataType.Type.DATETIME: ("timestamp", "`timestamp$()"),
    exp.DataType.Type.TIME: ("time", "`time$()"),
}
INTEGERS = {"short", "int", "long"}
HALF_EVEN = "{f:floor x+0.5;f-(x=f-0.5)&1=f mod 2}"

TEXT = {
    exp.DataType.Type.VARCHAR,
    exp.DataType.Type.CHAR,
    exp.DataType.Type.TEXT,
    exp.DataType.Type.NVARCHAR,
    exp.DataType.Type.NCHAR,
}


def atomic(node: exp.Expr) -> bool:
    """Whether a node prints as one q token, so it needs no parentheses as an operand."""
    if isinstance(node, exp.Neg):
        return False
    if isinstance(node, exp.Interval):
        return True  # a timespan literal
    if type(node).__name__ == "QText":
        return True  # q_qtext parenthesises it itself
    if isinstance(node, exp.Cast) and is_text(node.this) and node.to.this in TEMPORAL:
        return True  # DATE '...' is a q date literal
    # A subquery prints parenthesised already (q_subquery).
    return isinstance(
        node, (exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.Subquery)
    )


class QGenerator(Generator):
    """A sqlglot generator whose output is q.

    Extra option, passed through `sqlglot.transpile(..., write="q", ...)`:

    string_literals: "symbol" (the default) or "string".
    """

    def __init__(self, *args, string_literals: str = "symbol", **kwargs):
        # A construct with no q translation is an error unless asked otherwise:
        # q text that does something else is worse than no text.
        kwargs.setdefault("unsupported_level", ErrorLevel.RAISE)
        super().__init__(*args, **kwargs)
        if string_literals not in ("symbol", "string"):
            raise ValueError(
                f"string_literals must be 'symbol' or 'string', not {string_literals!r}"
            )
        self.symbols = string_literals == "symbol"
        self.depth = 0

    def preprocess(self, expression: exp.Expr) -> exp.Expr:
        from sqlglot_q.canonical import canonical

        # The tree's grouping in one spelling, so the q written for (a > 2)
        # and for a > 2 is the same text, as it is the same query.
        return canonical(super().preprocess(expression))

    # Dispatch -------------------------------------------------------------

    def sql(self, expression, key=None, comment=True) -> str:
        if not expression:
            return ""
        if isinstance(expression, str):
            return expression
        if key:
            value = expression.args.get(key)
            return self.sql(value) if value else ""
        handler = getattr(self, f"q_{expression.key}", None)
        if handler is None:
            return self.refuse(f"{type(expression).__name__} has no q translation")
        return handler(expression)

    def refuse(self, message: str) -> str:
        self.unsupported(message)
        # Under ErrorLevel.WARN or IGNORE the text still has to be something q
        # refuses, never something it runs.
        return "'unsupported"

    def operand(self, node: exp.Expr) -> str:
        return self.operand_text(node, self.sql(node))

    @staticmethod
    def operand_text(node: exp.Expr, text: str) -> str:
        return text if atomic(node) else f"({text})"

    # Names and literals ---------------------------------------------------

    def name(self, identifier: exp.Expr | str) -> str:
        text = identifier if isinstance(identifier, str) else identifier.name
        if not NAME.match(text):
            return self.refuse(f"{text!r} is not a q name")
        if text in RESERVED:
            return self.refuse(f"{text!r} is a q keyword and cannot name a column or table")
        return text

    def q_column(self, node: exp.Column) -> str:
        # q columns are unqualified: t.a is a, whichever table it came from.
        return self.name(node.name)

    def q_identifier(self, node: exp.Identifier) -> str:
        return self.name(node.name)

    def q_table(self, node: exp.Table) -> str:
        if node.args.get("db") or node.args.get("catalog"):
            return self.refuse(f"{node.sql()}: a qualified table name has no q form")
        return self.name(node.name)

    def q_literal(self, node: exp.Literal) -> str:
        if node.is_string:
            return self.symbol(node.this) if self.symbols else self.chars(node.this)
        return number(node.this)

    def q_null(self, node: exp.Null) -> str:
        return "0N"

    def q_boolean(self, node: exp.Boolean) -> str:
        return "1b" if node.this else "0b"

    def q_paren(self, node: exp.Paren) -> str:
        return f"({self.sql(node.this)})"

    def q_neg(self, node: exp.Neg) -> str:
        inner = node.this
        if isinstance(inner, exp.Literal) and not inner.is_string:
            return f"-{number(inner.this)}"
        return f"neg {self.sql(inner)}"

    def symbol(self, text: str) -> str:
        if SYMBOL.match(text):
            return f"`{text}"
        return f"`$({self.chars(text)})"

    @staticmethod
    def chars(text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        # A one-character string is a char atom in q; enlist keeps it a string.
        return f'enlist "{escaped}"' if len(text) == 1 else f'"{escaped}"'

    # Operators ------------------------------------------------------------

    def binary(self, node: exp.Expr) -> str:
        glyph = GLYPHS[type(node)]
        left, right = node.this, node.expression
        width = xbar_width(node)
        if width is not None:
            # FLOOR(x / n) * n buckets x by n: q's n xbar x.
            return f"{self.operand(width)} xbar {self.sql(node.this.this.this.this)}"
        if isinstance(node, COMPARISONS) and (
            isinstance(left, exp.Null) or isinstance(right, exp.Null)
        ):
            # In SQL x = NULL is unknown on every row; q's x=0N is true on the
            # null ones. There is no q boolean for unknown.
            return self.refuse("a comparison with NULL is never true in SQL: write IS NULL")
        if not self.symbols and isinstance(node, (exp.EQ, exp.NEQ)):
            # = on strings compares char by char; ~ compares whole strings.
            if is_text(right) or is_text(left):
                if is_text(left):
                    left, right = right, left
                match = f"{self.operand(left)}~\\:{self.sql(right)}"
                return match if isinstance(node, exp.EQ) else f"not {match}"
        return f"{self.operand(left)}{glyph}{self.operand(right)}"

    q_eq = q_neq = q_gt = q_gte = q_lt = q_lte = binary
    q_add = q_sub = q_mul = q_div = q_and = q_or = binary

    def q_mod(self, node: exp.Mod) -> str:
        return f"{self.operand(node.this)} mod {self.operand(node.expression)}"

    def q_not(self, node: exp.Not) -> str:
        return f"not {self.sql(node.this)}"

    def q_is(self, node: exp.Is) -> str:
        if isinstance(node.expression, exp.Null):
            return f"null {self.sql(node.this)}"
        return self.refuse("IS is only translated for IS NULL and IS NOT NULL")

    def q_in(self, node: exp.In) -> str:
        subject = self.operand(node.this)
        query = node.args.get("query")
        if query is not None:
            select = query.this if isinstance(query, exp.Subquery) else query
            if len(select.expressions) != 1:
                return self.refuse("IN (SELECT ...) needs exactly one column")
            column = select.expressions[0]
            inner = select.copy()
            inner.set("expressions", [exp.alias_(column, "x")])
            return f"{subject} in exec x from ({self.sql(inner)})"
        return f"{subject} in {self.vector(node.expressions)}"

    def q_between(self, node: exp.Between) -> str:
        bounds = [node.args["low"], node.args["high"]]
        return f"{self.operand(node.this)} within {self.vector(bounds)}"

    def q_like(self, node: exp.Like) -> str:
        pattern = node.expression
        if not (isinstance(pattern, exp.Literal) and pattern.is_string):
            return self.refuse("LIKE is only translated with a literal pattern")
        return f"{self.operand(node.this)} like {self.chars(like_pattern(pattern.this))}"

    def q_ilike(self, node: exp.ILike) -> str:
        pattern = node.expression
        if not (isinstance(pattern, exp.Literal) and pattern.is_string):
            return self.refuse("ILIKE is only translated with a literal pattern")
        text = like_pattern(pattern.this.lower())
        return f"(lower {self.sql(node.this)}) like {self.chars(text)}"

    def vector(self, items: list[exp.Expr]) -> str:
        """A q list of literals: `1 2 3`, `` `a`b ``, or `(x;y)` when it must be general."""
        if all(isinstance(i, exp.Literal) and not i.is_string for i in items):
            text = " ".join(number(i.this) for i in items)
            return f"enlist {text}" if len(items) == 1 else text
        if self.symbols and all(isinstance(i, exp.Literal) and i.is_string for i in items):
            if all(SYMBOL.match(i.this) for i in items):
                text = "".join(f"`{i.this}" for i in items)
                return f"enlist {text}" if len(items) == 1 else text
        if len(items) == 1:
            return f"enlist {self.operand(items[0])}"
        return "(" + ";".join(self.sql(i) for i in items) + ")"

    # Functions ------------------------------------------------------------

    def q_count(self, node: exp.Count) -> str:
        arg = node.this
        if arg is None or isinstance(arg, exp.Star):
            return "count i"
        if isinstance(arg, exp.Distinct):
            if len(arg.expressions) != 1:
                return self.refuse("COUNT(DISTINCT ...) is only translated for one column")
            col = self.operand(arg.expressions[0])
            return f"count distinct {col} where not null {col}"
        col = self.operand(arg)
        # COUNT(x) counts the rows where x is not null.
        return f"count {col} where not null {col}"

    def aggregate(self, node: exp.Expr) -> str:
        if node.args.get("distinct") or isinstance(node.this, exp.Distinct):
            return self.refuse(
                f"{node.sql()}: DISTINCT inside an aggregate is only translated for COUNT"
            )
        return f"{AGGREGATES[type(node)]} {self.sql(node.this)}"

    q_sum = q_avg = q_min = q_max = aggregate

    def monadic(self, node: exp.Expr) -> str:
        return f"{MONADIC[type(node)]} {self.sql(node.this)}"

    q_abs = q_upper = q_lower = q_floor = q_ceil = q_sqrt = q_exp = q_ln = monadic

    def q_length(self, node: exp.Length) -> str:
        arg = self.sql(node.this)
        return f"count each string {arg}" if self.symbols else f"count each {arg}"

    def q_coalesce(self, node: exp.Coalesce) -> str:
        # x^y is y with its nulls filled from x, so the first argument goes last.
        items = [node.this, *node.expressions]
        return "^".join(self.operand(i) for i in reversed(items))

    def q_cast(self, node: exp.Cast) -> str:
        kind = node.to.this
        if is_text(node.this) and kind in TEMPORAL:
            # DATE '2024-01-15' is a literal: q writes it 2024.01.15.
            return self.temporal(node.this.this, kind)
        arg = self.operand(node.this)
        if kind in TEXT:
            return f"`$string {arg}" if self.symbols else f"string {arg}"
        if kind not in TYPES:
            return self.refuse(f"CAST to {node.to.sql()} has no q type here")
        name = TYPES[kind][0]
        if name in INTEGERS:
            # SQL casts a float to an integer rounding half to even (2.5 is 2,
            # 3.5 is 4); q's `long$ rounds half away from zero (2.5 is 3).
            return f"`{name}${HALF_EVEN} {arg}"
        return f"`{name}${arg}"

    def temporal(self, text: str, kind) -> str:
        from sqlglot_q.temporal import date_literal, time_literal, timestamp_literal

        spell = {
            exp.DataType.Type.DATE: date_literal,
            exp.DataType.Type.TIME: time_literal,
        }.get(kind, timestamp_literal)
        literal = spell(text)
        return literal if literal else self.refuse(f"{text!r} is not a {kind.value} q can write")

    def q_interval(self, node: exp.Interval) -> str:
        from sqlglot_q.temporal import timespan

        span = timespan(node)
        if span is None:
            return self.refuse(
                f"{node.sql()}: only fixed intervals (days and smaller) have a q timespan"
            )
        return span

    def q_extract(self, node: exp.Extract) -> str:
        from sqlglot_q.temporal import PARTS

        part = node.name.upper()
        if part not in PARTS:
            return self.refuse(f"EXTRACT({part}) has no q cast")
        return f"`{PARTS[part]}${self.operand(node.expression)}"

    def date_part(self, node: exp.Expr) -> str:
        from sqlglot_q.temporal import PARTS

        part = {exp.Year: "YEAR", exp.Month: "MONTH", exp.Day: "DAY"}[type(node)]
        return f"`{PARTS[part]}${self.operand(node.this)}"

    q_year = q_month = q_day = date_part

    def q_datebin(self, node: exp.DateBin) -> str:
        from sqlglot_q.temporal import DAY, nanoseconds

        if node.args.get("zone") or len([a for a in node.args.values() if a is not None]) > 2:
            return self.refuse("TIME_BUCKET with an origin or offset has no q xbar")
        width = node.this
        ns = nanoseconds(width) if isinstance(width, exp.Interval) else None
        if ns is None or DAY % ns:
            # q counts buckets from 2000.01.01, DuckDB from 2000-01-03: they
            # agree only for widths that divide a day.
            return self.refuse("TIME_BUCKET is translated for widths that divide a day")
        return f"{self.sql(width)} xbar {self.sql(node.expression)}"

    def q_timestamptrunc(self, node: exp.TimestampTrunc) -> str:
        from sqlglot_q.temporal import UNITS, span_text

        unit = node.text("unit").upper().rstrip("S")
        if unit not in UNITS or UNITS[unit] > UNITS["DAY"]:
            return self.refuse(f"DATE_TRUNC to {unit} has no q xbar")
        return f"{span_text(UNITS[unit])} xbar {self.sql(node.this)}"

    def q_case(self, node: exp.Case) -> str:
        default = node.args.get("default")
        if default is None:
            return self.refuse("CASE without ELSE: q needs a value of the right type for no match")
        subject = node.this
        text = self.sql(default)
        for when in reversed(node.args["ifs"]):
            cond = when.this
            if subject is not None:
                cond = exp.EQ(this=subject.copy(), expression=cond.copy())
            text = f"?[{self.sql(cond)};{self.sql(when.args['true'])};{text}]"
        return text

    def q_if(self, node: exp.If) -> str:
        otherwise = node.args.get("false")
        if otherwise is None:
            return self.refuse("IF without an else value has no q form")
        return f"?[{self.sql(node.this)};{self.sql(node.args['true'])};{self.sql(otherwise)}]"

    def q_qtext(self, node) -> str:
        # q a window stage computed (windows.py); one token or parenthesised.
        text = node.this
        return text if " " not in text else f"({text})"

    def q_alias(self, node: exp.Alias) -> str:
        return f"{self.name(node.alias)}:{self.sql(node.this)}"

    def q_subquery(self, node: exp.Subquery) -> str:
        # A subquery reached here stands for a value (WHERE a > (SELECT ...)):
        # q's select returns a table, so take its one column's first item.
        # FROM, JOIN and UNION read subqueries as tables, via operand_query.
        select = node.this
        if not isinstance(select, exp.Select) or len(select.expressions) != 1:
            return self.refuse("a scalar subquery needs exactly one column")
        inner = select.copy()
        inner.set("expressions", [exp.alias_(unalias(select.expressions[0]), "x")])
        # Parenthesised, or a select phrase around it reads its from ('from).
        return f"(first exec x from ({self.sql(inner)}))"

    # Queries --------------------------------------------------------------

    def q_select(self, node: exp.Select) -> str:
        from sqlglot_q.query import select

        self.depth += 1
        try:
            return select(self, node, outermost=self.depth == 1)
        finally:
            self.depth -= 1

    def q_union(self, node: exp.Union) -> str:
        from sqlglot_q.query import limited, order_terms, output_names, sort

        both = f"{self.operand_query(node.this)},{self.operand_query(node.expression)}"
        text = both if not node.args.get("distinct") else f"distinct {both}"
        # ORDER BY and LIMIT after a UNION belong to the union, and name the
        # columns of its first query.
        order = node.args.get("order")
        if order:
            first = node.this
            while isinstance(first, (exp.Union, exp.Subquery)):
                first = first.this
            items = list(first.expressions)
            terms, _ = order_terms(self, order, items, output_names(items), allow_source=False)
            text = sort(terms, text)
        return limited(self, node, text)

    def operand_query(self, node: exp.Expr) -> str:
        """A query used as a table: FROM (...), a join side, a UNION operand."""
        if isinstance(node, exp.Subquery):
            node = node.this
        return f"({self.sql(node)})"

    # Statements -----------------------------------------------------------

    def q_insert(self, node: exp.Insert) -> str:
        target = node.this
        columns = []
        if isinstance(target, exp.Schema):
            columns = [self.name(c) for c in target.expressions]
            target = target.this
        table = f"`{self.q_table(target)}"
        source = node.expression
        if isinstance(source, exp.Values):
            rows = [r.expressions for r in source.expressions]
            if columns:
                cells = [self.vector([row[i] for row in rows]) for i in range(len(columns))]
                body = ";".join(f"{c}:{v}" for c, v in zip(columns, cells, strict=True))
                return f"{table} insert ([] {body})"
            if len(rows) == 1:
                return f"{table} insert ({';'.join(self.sql(v) for v in rows[0])})"
            cols = [self.vector([row[i] for row in rows]) for i in range(len(rows[0]))]
            return f"{table} insert ({';'.join(cols)})"
        if columns:
            return self.refuse("INSERT INTO t (cols) SELECT ... is not translated")
        return f"{table} insert {self.operand_query(source)}"

    def q_update(self, node: exp.Update) -> str:
        where = self.where(node.args.get("where"))
        sets = []
        for e in node.expressions:
            col, value = self.name(e.this.name), self.sql(e.expression)
            if where and not (not self.symbols and is_text(e.expression)):
                # Amending some rows of a column needs the column's own type:
                # q refuses 0 into a float column ('type) where SQL converts.
                value = f"(type {col})${self.operand_text(e.expression, value)}"
            sets.append(f"{col}:{value}")
        sets = ",".join(sets)
        return f"update {sets} from `{self.q_table(node.this)}{where}"

    def q_delete(self, node: exp.Delete) -> str:
        return f"delete from `{self.q_table(node.this)}{self.where(node.args.get('where'))}"

    def q_create(self, node: exp.Create) -> str:
        schema = node.this
        query = node.expression
        if node.args.get("kind") == "TABLE" and isinstance(schema, exp.Table) and query:
            # CREATE TABLE t AS SELECT ...: q names the result.
            return f"{self.q_table(schema)}:{self.sql(query)}"
        if node.args.get("kind") != "TABLE" or not isinstance(schema, exp.Schema):
            return self.refuse("CREATE is only translated for CREATE TABLE with columns")
        cols = []
        for col in schema.expressions:
            kind = col.args["kind"].this
            if kind in TEXT:
                empty = "`symbol$()" if self.symbols else "()"
            elif kind in TYPES:
                empty = TYPES[kind][1]
            else:
                return self.refuse(f"column type {col.args['kind'].sql()} has no q type here")
            cols.append(f"{self.name(col.name)}:{empty}")
        return f"{self.q_table(schema.this)}:([] {';'.join(cols)})"

    def q_drop(self, node: exp.Drop) -> str:
        if node.args.get("kind") != "TABLE":
            return self.refuse("DROP is only translated for DROP TABLE")
        tables = node.args.get("tables") or [node.this]
        return f"delete {','.join(self.q_table(t) for t in tables)} from `."

    def where(self, node: exp.Where | None) -> str:
        if node is None:
            return ""
        # A top-level AND is a q where list: each constraint filters what the
        # last one left, which for SQL's pure predicates is the same rows.
        from sqlglot_q.query import conjuncts

        return " where " + ", ".join(self.sql(c) for c in conjuncts(node.this))


TEMPORAL = {
    exp.DataType.Type.DATE,
    exp.DataType.Type.TIME,
    exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPNTZ,
    exp.DataType.Type.DATETIME,
}


def xbar_width(node: exp.Expr) -> exp.Expr | None:
    """n in FLOOR(x / n) * n, the SQL spelling of q's n xbar x."""
    if not isinstance(node, exp.Mul):
        return None
    floor = node.this.this if isinstance(node.this, exp.Paren) else node.this
    if not isinstance(floor, exp.Floor) or floor.args.get("decimals"):
        return None
    div = floor.this.this if isinstance(floor.this, exp.Paren) else floor.this
    if isinstance(div, exp.Div) and div.expression == node.expression:
        if isinstance(floor.this, exp.Paren):
            floor.set("this", div)
        return node.expression
    return None


def unalias(node: exp.Expr) -> exp.Expr:
    return node.this if isinstance(node, exp.Alias) else node


def is_text(node: exp.Expr) -> bool:
    return isinstance(node, exp.Literal) and node.is_string


LONG = 2**63 - 1


def number(text: str) -> str:
    """A SQL numeric literal in q's spelling: an integer that fits stays a
    long, and anything else is a float, which q will not read in every form
    SQL writes one (1E3 and 1.5E+3 are refused; 1e3 and 1.5e+3 are not)."""
    if text.isdigit() and int(text) < LONG:
        return str(int(text))
    value = float(text)
    if value != value:
        return "0n"
    if value in (float("inf"), float("-inf")):
        return "0w" if value > 0 else "-0w"
    out = repr(value)
    return out if any(c in out for c in ".e") else f"{out}f"


def like_pattern(sql: str) -> str:
    """A SQL LIKE pattern as a q like pattern: % is *, _ is ?, and q's own
    wildcard characters are bracketed so they match themselves."""
    out = []
    for ch in sql:
        if ch == "%":
            out.append("*")
        elif ch == "_":
            out.append("?")
        elif ch in "*?[]":
            out.append(f"[{ch}]")
        else:
            out.append(ch)
    return "".join(out)
