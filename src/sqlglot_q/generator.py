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
    exp.Sign: "signum",
}

# Spelled as lambdas because q has no word for them; parser.py reads these
# exact texts back.
ROUND = "{signum[x]*floor 0.5+abs x}"  # half away from zero, as SQL
ROUND_TO = "{(signum x)*(floor 0.5+abs[x]*10 xexp y)%10 xexp y}"
INT_DIV = "{signum[x*y]*div[abs x;abs y]}"  # toward zero; q's div floors
MOD = "{x-y*signum[x%y]*floor abs x%y}"
STRING_AGG = (
    "{{$[all null x;`;`$({sep}) sv string x where not null x]}}"  # SQL's %: the dividend's sign
)
LENGTH = "{?[null x;0N;count each string x]}"  # a null symbol has no length
# Aggregates of no values (none, or all null) are NULL in SQL; q's sum is 0,
# min and max are 0W and -0W. And q's min and max refuse symbols ('type),
# where SQL's take the text's order.
# Each starts with an aggregate q knows (sum, first, last): q's select only
# takes a lambda's result as one row when an aggregate is outermost.
NULLS = "{$[all null x;x 0N;x]}"  # all null (or none): a null, which sum keeps
NON_NULL = "{x where not null x}"
SUM = f"sum {NULLS}"
MIN = f"first asc {NON_NULL}"  # asc orders symbols too; first of none is null
MAX = "last asc"  # nulls sort first, so last is the largest
PARSE = {"boolean": "B", "short": "H", "int": "I", "long": "J", "real": "E", "float": "F"}
# LEFT(s, n) and RIGHT(s, n): sublist, not #, which repeats a short string.
LEFT = ("{`$x sublist/:string y}", "{x sublist/:y}")
RIGHT = ("{`$(neg x) sublist/:string y}", "{(neg x) sublist/:y}")

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


class QText(exp.Expression):
    """q text standing in a tree the generator prints: a window stage's
    value (windows.py). Printed by TRANSFORMS[QText]."""

    arg_types = {"this": True}


def expression_classes() -> dict[str, type]:
    """sqlglot's node classes by key; exp.EXPR_CLASSES from 30.x on."""
    known = getattr(exp, "EXPR_CLASSES", None)
    if known:
        return known
    found, stack = {}, [exp.Expression]
    while stack:
        cls = stack.pop()
        found.setdefault(cls.key, cls)
        stack.extend(cls.__subclasses__())
    return found


def q_dispatch(cls: type) -> dict:
    """The generator's handlers, built the way sqlglot builds a generator's
    (TRANSFORMS first, then each <key>_sql method), except that a method
    inherited from sqlglot's SQL generator never counts: its text is SQL,
    and q would refuse it or, worse, read it as something else."""
    table = dict(cls.TRANSFORMS)
    classes = expression_classes()
    for name in dir(cls):
        if not name.endswith("_sql") or name.startswith("_"):
            continue
        owner = next(k for k in cls.__mro__ if name in vars(k))
        if not issubclass(owner, QGenerator):
            continue
        node = classes.get(name[:-4])
        if node is not None and node not in table:
            table[node] = getattr(cls, name)
    return table


_DISPATCH: dict[type, dict] = {}


def monad(word: str, skip_nulls: bool = False):
    """A TRANSFORMS entry for a one-argument function q spells with a word.
    skip_nulls when q's word counts a null that SQL's function skips."""

    def handler(self, node):
        arg = self.operand(node.this)
        return f"{word} {arg} where not null {arg}" if skip_nulls else f"{word} {arg}"

    return handler


def dyad(word: str):
    """A TRANSFORMS entry for x <word> y."""

    def handler(self, node):
        return f"{self.operand(node.this)} {word} {self.operand(node.expression)}"

    return handler


class QGenerator(Generator):
    """A sqlglot generator whose output is q.

    It follows sqlglot's conventions, so a dialect built on it can extend it
    the way any sqlglot dialect is extended: a `<key>_sql` method for a node,
    an entry in TRANSFORMS, or a name in FUNCTIONS for a function sqlglot
    parses as anonymous (a UDF):

        class MyQ(Q):
            class Generator(Q.Generator):
                FUNCTIONS = {**Q.Generator.FUNCTIONS, "VWAP": "wavg"}

    The option string_literals ("symbol", the default, or "string") is a
    dialect setting, `write="q, string_literals=string"`, or a generator
    argument, `transpile(..., string_literals="string")`.
    """

    TRANSFORMS: dict = {}  # filled in below the class, once its methods exist

    # Anonymous SQL functions (names sqlglot does not know) and the q
    # function each is: f(a) is f a, f(a, b) is f[a;b].
    FUNCTIONS: dict[str, str] = {}

    def __init__(self, *args, string_literals: str | None = None, **kwargs):
        # A construct with no q translation is an error unless asked otherwise:
        # q text that does something else is worse than no text.
        kwargs.setdefault("unsupported_level", ErrorLevel.RAISE)
        super().__init__(*args, **kwargs)
        if string_literals is None:
            string_literals = self.dialect.settings.get("string_literals", "symbol")
        if string_literals not in ("symbol", "string"):
            raise ValueError(
                f"string_literals must be 'symbol' or 'string', not {string_literals!r}"
            )
        self.symbols = string_literals == "symbol"
        self.depth = 0
        cls = type(self)
        if cls not in _DISPATCH:
            _DISPATCH[cls] = q_dispatch(cls)
        self._q_dispatch = _DISPATCH[cls]

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
        handler = self._q_dispatch.get(type(expression))
        if handler is None:
            return self.refuse(f"{type(expression).__name__} has no q translation")
        return handler(self, expression)

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

    def column_sql(self, node: exp.Column) -> str:
        # q columns are unqualified: t.a is a, whichever table it came from.
        return self.name(node.name)

    def identifier_sql(self, node: exp.Identifier) -> str:
        return self.name(node.name)

    def table_sql(self, node: exp.Table) -> str:
        if node.args.get("db") or node.args.get("catalog"):
            return self.refuse(f"{node.sql()}: a qualified table name has no q form")
        return self.name(node.name)

    def literal_sql(self, node: exp.Literal) -> str:
        if node.is_string:
            return self.symbol(node.this) if self.symbols else self.chars(node.this)
        return number(node.this)

    def null_sql(self, node: exp.Null) -> str:
        return "0N"

    def boolean_sql(self, node: exp.Boolean) -> str:
        return "1b" if node.this else "0b"

    def paren_sql(self, node: exp.Paren) -> str:
        return f"({self.sql(node.this)})"

    def neg_sql(self, node: exp.Neg) -> str:
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

    eq_sql = neq_sql = gt_sql = gte_sql = lt_sql = lte_sql = binary
    add_sql = sub_sql = mul_sql = div_sql = and_sql = or_sql = binary

    def nullsafeeq_sql(self, node) -> str:
        # q's = is already null-safe: 0N=0N is true, 0N=1 false.
        return self.sql(exp.EQ(this=node.this.copy(), expression=node.expression.copy()))

    def nullsafeneq_sql(self, node) -> str:
        return self.sql(exp.NEQ(this=node.this.copy(), expression=node.expression.copy()))

    def groupconcat_sql(self, node) -> str:
        sep = node.args.get("separator")
        if sep is None:
            sep = exp.Literal.string(",")
        if not (isinstance(sep, exp.Literal) and sep.is_string):
            return self.refuse("STRING_AGG is translated with a literal separator")
        if isinstance(node.this, (exp.Distinct, exp.Order)) or node.args.get("order"):
            return self.refuse("STRING_AGG with DISTINCT or ORDER BY has no q translation here")
        if not self.symbols:
            return self.refuse('STRING_AGG is translated for symbols, not string_literals="string"')
        # Skips nulls as SQL does; all null (or none) is the null symbol.
        fn = STRING_AGG.format(sep=self.chars(sep.this).replace("enlist ", ""))
        return f"first {fn}[{self.sql(node.this)}]"

    def mod_sql(self, node: exp.Mod) -> str:
        floor = floor_mod(node)
        if floor is not None:
            # ((x % y) + y) % y is the floor modulo, which is q's mod.
            x, y = floor
            return f"{self.operand(x)} mod {self.operand(y)}"
        # SQL's % takes the dividend's sign (-7 % 3 is -1); q's mod floors (2).
        return f"{MOD}[{self.sql(node.this)};{self.sql(node.expression)}]"

    def not_sql(self, node: exp.Not) -> str:
        return f"not {self.sql(node.this)}"

    def is_sql(self, node: exp.Is) -> str:
        if isinstance(node.expression, exp.Null):
            return f"null {self.sql(node.this)}"
        return self.refuse("IS is only translated for IS NULL and IS NOT NULL")

    def in_sql(self, node: exp.In) -> str:
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

    def between_sql(self, node: exp.Between) -> str:
        bounds = [node.args["low"], node.args["high"]]
        return f"{self.operand(node.this)} within {self.vector(bounds)}"

    def like_sql(self, node: exp.Like) -> str:
        pattern = node.expression
        if not (isinstance(pattern, exp.Literal) and pattern.is_string):
            return self.refuse("LIKE is only translated with a literal pattern")
        return f"{self.operand(node.this)} like {self.chars(like_pattern(pattern.this))}"

    def ilike_sql(self, node: exp.ILike) -> str:
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

    def count_sql(self, node: exp.Count) -> str:
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
        word = NULL_SAFE.get(type(node), AGGREGATES[type(node)])
        return f"{word} {self.sql(node.this)}"

    sum_sql = avg_sql = min_sql = max_sql = aggregate

    def monadic(self, node: exp.Expr) -> str:
        return f"{MONADIC[type(node)]} {self.sql(node.this)}"

    abs_sql = upper_sql = lower_sql = floor_sql = ceil_sql = sqrt_sql = exp_sql = ln_sql = (
        sign_sql
    ) = monadic

    def round_sql(self, node: exp.Round) -> str:
        places = node.args.get("decimals")
        if places is None:
            return f"{ROUND} {self.operand(node.this)}"
        return f"{ROUND_TO}[{self.sql(node.this)};{self.sql(places)}]"

    def pow_sql(self, node: exp.Pow) -> str:
        return f"{self.operand(node.this)} xexp {self.operand(node.expression)}"

    def intdiv_sql(self, node: exp.IntDiv) -> str:
        return f"{INT_DIV}[{self.sql(node.this)};{self.sql(node.expression)}]"

    def greatest_sql(self, node: exp.Greatest) -> str:
        # | is q's max, and like GREATEST it skips a null: 1|0N is 1.
        return "|".join(self.operand(i) for i in [node.this, *node.expressions])

    def least_sql(self, node: exp.Least) -> str:
        # & is q's min, but 1&0N is 0N where LEAST skips the null. Filling
        # each side from the other (y^x) fails when a side is a literal atom
        # ('nyi), so the nulls are taken apart with ?[...], which also
        # promotes a long against a float as SQL does.
        items = [node.this, *node.expressions]
        text = self.operand(items[0])
        for item in items[1:]:
            x, y = text, self.operand(item)
            text = f"?[null {x};{y};?[null {y};{x};{x}&{y}]]"
        return text

    def nullif_sql(self, node: exp.Nullif) -> str:
        case = exp.Case(
            ifs=[
                exp.If(
                    this=exp.EQ(this=node.this.copy(), expression=node.expression.copy()),
                    true=exp.Null(),
                )
            ],
            default=node.this.copy(),
        )
        return self.case_sql(case)

    def filter_sql(self, node: exp.Filter) -> str:
        agg, cond = node.this, node.expression.this
        where = self.condition(cond)
        if isinstance(agg, exp.Count) and (agg.this is None or isinstance(agg.this, exp.Star)):
            return f"count i where {where}"
        if type(agg) in AGGREGATES and not agg.args.get("distinct"):
            word = NULL_SAFE.get(type(agg), AGGREGATES[type(agg)])
            return f"{word} {self.operand(agg.this)} where {where}"
        return self.refuse(f"{agg.sql()} FILTER (...) has no q translation here")

    def trim_sql(self, node: exp.Trim) -> str:
        if node.expression is not None or node.args.get("collation"):
            return self.refuse("TRIM of characters other than spaces has no q translation here")
        word = {"LEADING": "ltrim", "TRAILING": "rtrim"}.get(
            (node.args.get("position") or "").upper(), "trim"
        )
        arg = self.operand(node.this)
        return f"`$({word} string {arg})" if self.symbols else f"{word} {arg}"

    def concat_sql(self, node: exp.Concat) -> str:
        return self.joined([node.this, *node.expressions] if node.this else node.expressions)

    def dpipe_sql(self, node: exp.DPipe) -> str:
        text = self.joined(flatten_dpipe(node))
        columns = []
        for part in flatten_dpipe(node):
            if not isinstance(part, exp.Literal) and part not in columns:
                columns.append(part)
        if not columns:
            return text
        # || is NULL when any side is; CONCAT skips nulls.
        guards = [f"null {self.operand(c)}" for c in columns]
        guard = guards[0] if len(guards) == 1 else "|".join(f"({g})" for g in guards)
        empty = "`" if self.symbols else '""'
        return f"?[{guard};{empty};{text}]"

    def joined(self, parts: list[exp.Expr]) -> str:
        """Text parts joined row by row: ,' between two columns, ,\\: and ,/:
        where one side is a literal, so a literal string is not paired item
        by item with the rows."""
        pieces = []
        for part in parts:
            if isinstance(part, exp.Literal) and part.is_string:
                pieces.append((self.chars(part.this), True))
            elif self.symbols:
                pieces.append((f"string {self.operand(part)}", False))
            else:
                pieces.append((self.operand(part), False))
        if self.symbols:
            first, last = parts[0], parts[-1]
            for part, edge in ((first, str.lstrip), (last, str.rstrip)):
                if (
                    isinstance(part, exp.Literal)
                    and part.is_string
                    and edge(part.this) != part.this
                ):
                    # `$ trims a symbol's ends: the space would be lost.
                    return self.refuse(
                        "a symbol cannot start or end with a space: concatenate with "
                        'string_literals="string"'
                    )
        text, atom = pieces[0]
        for piece, lit in pieces[1:]:
            left = text if " " not in text else f"({text})"
            right = piece if " " not in piece else f"({piece})"
            glyph = "," if atom and lit else ",\\:" if lit else ",/:" if atom else ",'"
            text, atom = f"{left}{glyph}{right}", atom and lit
        return f"`$({text})" if self.symbols else text

    def length_sql(self, node: exp.Length) -> str:
        if self.symbols:
            # LENGTH(NULL) is NULL, where count each string ` is 0.
            return f"{LENGTH} {self.operand(node.this)}"
        return f"count each {self.operand(node.this)}"

    def coalesce_sql(self, node: exp.Coalesce) -> str:
        # x^y is y with its nulls filled from x, so the first argument goes last.
        items = [node.this, *node.expressions]
        return "^".join(self.operand(i) for i in reversed(items))

    def cast_sql(self, node: exp.Cast) -> str:
        kind = node.to.this
        if is_text(node.this) and kind in TEMPORAL:
            # DATE '2024-01-15' is a literal: q writes it 2024.01.15.
            return self.temporal(node.this.this, kind)
        if is_text(node.this) and kind in TYPES and TYPES[kind][0] in PARSE:
            # CAST('5' AS BIGINT) parses the text: q's "J"$"5". `long$ of
            # the symbol `5 is 'type.
            return f'"{PARSE[TYPES[kind][0]]}"${self.chars(node.this.this)}'
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

    def interval_sql(self, node: exp.Interval) -> str:
        from sqlglot_q.temporal import timespan

        span = timespan(node)
        if span is None:
            return self.refuse(
                f"{node.sql()}: only fixed intervals (days and smaller) have a q timespan"
            )
        return span

    def extract_sql(self, node: exp.Extract) -> str:
        from sqlglot_q.temporal import PARTS

        part = node.name.upper()
        if part not in PARTS:
            return self.refuse(f"EXTRACT({part}) has no q cast")
        return f"`{PARTS[part]}${self.operand(node.expression)}"

    def date_part(self, node: exp.Expr) -> str:
        from sqlglot_q.temporal import PARTS

        part = {exp.Year: "YEAR", exp.Month: "MONTH", exp.Day: "DAY"}[type(node)]
        return f"`{PARTS[part]}${self.operand(node.this)}"

    year_sql = month_sql = day_sql = date_part

    def datebin_sql(self, node: exp.DateBin) -> str:
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

    def timestamptrunc_sql(self, node: exp.TimestampTrunc) -> str:
        from sqlglot_q.temporal import UNITS, span_text

        unit = node.text("unit").upper().rstrip("S")
        if unit not in UNITS or UNITS[unit] > UNITS["DAY"]:
            return self.refuse(f"DATE_TRUNC to {unit} has no q xbar")
        return f"{span_text(UNITS[unit])} xbar {self.sql(node.this)}"

    def case_sql(self, node: exp.Case) -> str:
        default = node.args.get("default")
        branches = [w.args["true"] for w in node.args["ifs"]] + [default]
        known = next((b for b in branches if b is not None and not isinstance(b, exp.Null)), None)

        def value(b) -> str:
            if b is not None and not isinstance(b, exp.Null):
                return self.sql(b)
            # No ELSE, or THEN NULL: a null of the other branches' type, so
            # ?[...] does not mix a long null into a symbol column.
            if known is None:
                return "0N"
            if isinstance(known, (exp.Literal, exp.Boolean)) or atomic_literal(known):
                return f"(enlist {self.sql(known)}) 0N"
            return f"{self.operand(known)} 0N"

        subject = node.this
        text = value(default)
        for when in reversed(node.args["ifs"]):
            cond = when.this
            if subject is not None:
                cond = exp.EQ(this=subject.copy(), expression=cond.copy())
            text = f"?[{self.condition(cond)};{value(when.args['true'])};{text}]"
        return text

    def if_sql(self, node: exp.If) -> str:
        otherwise = node.args.get("false")
        if otherwise is None:
            return self.refuse("IF without an else value has no q form")
        return f"?[{self.condition(node.this)};{self.sql(node.args['true'])};{self.sql(otherwise)}]"

    def anonymous_sql(self, node: exp.Anonymous) -> str:
        word = self.FUNCTIONS.get(node.name.upper())
        if word is None:
            return self.refuse(
                f"{node.name}() is not a function sqlglot knows; map it to q in "
                "QGenerator.FUNCTIONS"
            )
        args = node.expressions
        if len(args) == 1:
            return f"{word} {self.operand(args[0])}"
        return f"{word}[{';'.join(self.sql(a) for a in args)}]"

    def alias_sql(self, node: exp.Alias) -> str:
        return f"{self.name(node.alias)}:{self.sql(node.this)}"

    def subquery_sql(self, node: exp.Subquery) -> str:
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

    def select_sql(self, node: exp.Select) -> str:
        from sqlglot_q.query import select

        self.depth += 1
        try:
            return select(self, node, outermost=self.depth == 1)
        finally:
            self.depth -= 1

    def union_sql(self, node: exp.Union) -> str:
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

    def insert_sql(self, node: exp.Insert) -> str:
        verb = "insert"
        conflict = node.args.get("conflict")
        if (node.args.get("alternative") or "").upper() == "REPLACE":
            verb = "upsert"  # INSERT OR REPLACE: a row with the key replaces
        elif conflict is not None:
            if not replaces_all(conflict):
                return self.refuse(
                    "ON CONFLICT is translated as DO UPDATE SET c = EXCLUDED.c for every "
                    "column set, which is q's upsert; DO NOTHING keeps the old row and has "
                    "no q form here"
                )
            verb = "upsert"
        return self.inserted(node, verb)

    def inserted(self, node: exp.Insert, verb: str) -> str:
        target = node.this
        columns = []
        if isinstance(target, exp.Schema):
            columns = [self.name(c) for c in target.expressions]
            target = target.this
        table = f"`{self.table_sql(target)}"
        source = node.expression
        if isinstance(source, exp.Values):
            rows = [r.expressions for r in source.expressions]
            if columns:
                cells = [self.vector([row[i] for row in rows]) for i in range(len(columns))]
                body = ";".join(f"{c}:{v}" for c, v in zip(columns, cells, strict=True))
                return f"{table} {verb} ([] {body})"
            if len(rows) == 1:
                return f"{table} {verb} ({';'.join(self.sql(v) for v in rows[0])})"
            cols = [self.vector([row[i] for row in rows]) for i in range(len(rows[0]))]
            return f"{table} {verb} ({';'.join(cols)})"
        if columns:
            return self.refuse("INSERT INTO t (cols) SELECT ... is not translated")
        return f"{table} {verb} {self.operand_query(source)}"

    def update_sql(self, node: exp.Update) -> str:
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
        return f"update {sets} from `{self.table_sql(node.this)}{where}"

    def delete_sql(self, node: exp.Delete) -> str:
        return f"delete from `{self.table_sql(node.this)}{self.where(node.args.get('where'))}"

    def create_sql(self, node: exp.Create) -> str:
        schema = node.this
        query = node.expression
        if node.args.get("kind") == "TABLE" and isinstance(schema, exp.Table) and query:
            # CREATE TABLE t AS SELECT ...: q names the result.
            return f"{self.table_sql(schema)}:{self.sql(query)}"
        if node.args.get("kind") != "TABLE" or not isinstance(schema, exp.Schema):
            return self.refuse("CREATE is only translated for CREATE TABLE with columns")
        cols, keys = [], []
        for col in schema.expressions:
            if isinstance(col, exp.PrimaryKey):
                keys += [c.name for c in col.expressions]
                continue
            if not isinstance(col, exp.ColumnDef):
                return self.refuse(f"{col.sql()} in CREATE TABLE has no q form")
            if any(
                isinstance(c.args.get("kind"), exp.PrimaryKeyColumnConstraint)
                for c in col.args.get("constraints") or []
            ):
                keys.append(col.name)
            kind = col.args["kind"].this
            if kind in TEXT:
                empty = "`symbol$()" if self.symbols else "()"
            elif kind in TYPES:
                empty = TYPES[kind][1]
            else:
                return self.refuse(f"column type {col.args['kind'].sql()} has no q type here")
            cols.append((col.name, f"{self.name(col.name)}:{empty}"))
        # A primary key makes a keyed table: ([k:...] v:...), whose upsert
        # replaces the row with the same key.
        keyed = [text for name, text in cols if name in keys]
        rest = [text for name, text in cols if name not in keys]
        if set(keys) - {name for name, _ in cols}:
            return self.refuse("PRIMARY KEY names a column the table does not have")
        if keys and [n for n, _ in cols][: len(keys)] != [k for k in keys]:
            return self.refuse("a keyed q table has its key columns first")
        return f"{self.table_sql(schema.this)}:([{';'.join(keyed)}] {';'.join(rest)})"

    def truncatetable_sql(self, node: exp.TruncateTable) -> str:
        tables = node.expressions
        if len(tables) != 1:
            return self.refuse("TRUNCATE of several tables is several q statements")
        return f"delete from `{self.table_sql(tables[0])}"

    def log_sql(self, node: exp.Log) -> str:
        if node.expression is None:
            # LOG(x) is base 10 in DuckDB and Postgres and e elsewhere; which
            # one the SQL meant is gone by the time it reaches here.
            return self.refuse("LOG(x) means log10 or ln by dialect: write LOG10(x) or LN(x)")
        return f"{self.operand(node.this)} xlog {self.operand(node.expression)}"

    def startswith_sql(self, node) -> str:
        return self.matches(node, "{}*")

    def endswith_sql(self, node) -> str:
        return self.matches(node, "*{}")

    def contains_sql(self, node) -> str:
        return self.matches(node, "*{}*")

    def matches(self, node, shape: str) -> str:
        text = node.expression
        if not (isinstance(text, exp.Literal) and text.is_string):
            return self.refuse(f"{node.sql()}: the text to find must be a literal")
        escaped = "".join(f"[{c}]" if c in "*?[]" else c for c in text.this)
        return f"{self.operand(node.this)} like {self.chars(shape.format(escaped))}"

    def left_sql(self, node) -> str:
        return self.ends(node, LEFT)

    def right_sql(self, node) -> str:
        return self.ends(node, RIGHT)

    def ends(self, node, lambdas) -> str:
        fn = lambdas[0] if self.symbols else lambdas[1]
        return f"{fn}[{self.sql(node.expression)};{self.sql(node.this)}]"

    def drop_sql(self, node: exp.Drop) -> str:
        if node.args.get("kind") != "TABLE":
            return self.refuse("DROP is only translated for DROP TABLE")
        tables = node.args.get("tables") or [node.this]
        return f"delete {','.join(self.table_sql(t) for t in tables)} from `."

    def condition(self, node: exp.Expr) -> str:
        """A condition where SQL keeps what is true: CASE WHEN, FILTER, HAVING."""
        from sqlglot_q.truth import holds

        text = holds(self, node)
        return text

    def where(self, node: exp.Where | None) -> str:
        if node is None:
            return ""
        # A top-level AND is a q where list: each constraint filters what the
        # last one left, which for SQL's pure predicates is the same rows.
        from sqlglot_q.truth import holds

        # Each constraint keeps the rows where it is true, as SQL's WHERE does.
        return " where " + ", ".join(holds(self, c) for c in constraints(node.this))


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


NULL_SAFE = {exp.Sum: SUM, exp.Min: MIN, exp.Max: MAX}


def floor_mod(node: exp.Mod):
    """(x, y) when the node is ((x % y) + y) % y."""

    def bare(n):
        while isinstance(n, exp.Paren):
            n = n.this
        return n

    outer_y = node.expression
    add = bare(node.this)
    if not isinstance(add, exp.Add) or add.expression != outer_y:
        return None
    inner = bare(add.this)
    if isinstance(inner, exp.Mod) and inner.expression == outer_y:
        return inner.this, outer_y
    return None


def constraints(node: exp.Expr) -> list[exp.Expr]:
    """A condition as the list of what must all hold: an AND's sides, and,
    by De Morgan, NOT (p OR q) as NOT p and NOT q. One spelling for one
    condition, so it reads back the same."""
    from sqlglot_q.query import conjuncts

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return [c for part in conjuncts(node) for c in constraints(part)]
    if isinstance(node, exp.Not):
        inner = node.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, exp.Or):
            return [
                *constraints(exp.Not(this=inner.this.copy())),
                *constraints(exp.Not(this=inner.expression.copy())),
            ]
        if isinstance(inner, exp.Not):
            return constraints(inner.this)
    return [node]


def atomic_literal(node: exp.Expr) -> bool:
    return isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal)


def flatten_dpipe(node: exp.Expr) -> list[exp.Expr]:
    if isinstance(node, exp.Paren) and isinstance(node.this, exp.DPipe):
        node = node.this
    if isinstance(node, exp.DPipe):
        return [*flatten_dpipe(node.this), *flatten_dpipe(node.expression)]
    return [node]


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


def qtext(self, node) -> str:
    # q a window stage computed (windows.py); one token or parenthesised.
    text = node.this
    return text if " " not in text else f"({text})"


def _select_rewrites():
    from sqlglot import transforms

    from sqlglot_q.rewrites import expand_group_by_all, q_names

    # sqlglot's own rewrites, as its dialects use them: QUALIFY and DISTINCT
    # ON become a window in a subquery, which the window stages translate.
    return transforms.preprocess(
        [
            transforms.eliminate_qualify,
            transforms.eliminate_distinct_on,
            expand_group_by_all,
            q_names,
        ]
    )


QGenerator.TRANSFORMS = {
    QText: qtext,
    exp.Select: _select_rewrites(),
    # Statistics. q's med counts a null as the smallest value; SQL's MEDIAN
    # skips it. sdev, dev, svar and var skip nulls as SQL does.
    exp.Median: monad("med", skip_nulls=True),
    exp.Stddev: monad("sdev"),
    exp.StddevSamp: monad("sdev"),
    exp.StddevPop: monad("dev"),
    exp.Variance: monad("svar"),
    exp.VariancePop: monad("var"),
    exp.Corr: dyad("cor"),
    exp.CovarPop: dyad("cov"),
    exp.CovarSamp: dyad("scov"),
    exp.CountIf: monad("sum"),
    exp.AnyValue: monad("first", skip_nulls=True),
    # DuckDB parses ANY_VALUE(x) as IGNORE NULLS around it: first skips them.
    exp.IgnoreNulls: lambda self, node: (
        self.sql(node.this)
        if isinstance(node.this, exp.AnyValue)
        else self.refuse("IGNORE NULLS is translated for ANY_VALUE only")
    ),
}


def replaces_all(conflict: exp.OnConflict) -> bool:
    """ON CONFLICT DO UPDATE SET c = EXCLUDED.c, ...: the new row replaces
    the old, which is q's upsert on a keyed table."""
    action = conflict.args.get("action")
    if action is None or action.name.upper() != "DO UPDATE":
        return False
    sets = conflict.expressions
    return bool(sets) and all(
        isinstance(e, exp.EQ)
        and isinstance(e.expression, exp.Column)
        and e.expression.table.upper() == "EXCLUDED"
        and e.expression.name == e.this.name
        for e in sets
    )
