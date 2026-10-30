"""Read qSQL into a sqlglot syntax tree, so q can be transpiled to SQL.

This reads the q the generator writes, plus the common hand-written shapes of
the same queries (a keyed `select ... by`, `` `a xasc t ``, `sym=`x`), and
refuses anything else with a ParseError. q has no grammar a SQL tokenizer can
approximate, so this is its own small reader:

- q evaluates right to left with no precedence: in `a*b+c` the right operand
  of `*` is `b+c`. `expr` reads one term and, if a dyad follows, everything to
  its right as that dyad's right operand.
- A qSQL clause is cut at its top-level commas, and the words by, from and
  where end the clauses before them; inside a select phrase `where` is the
  monadic function (`count d where not null d`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.errors import ParseError

from sqlglot_q import scalars
from sqlglot_q.canonical import canonical
from sqlglot_q.generator import (
    ANONYMOUS,
    ANY_FILL,
    BOOL_AND,
    BOOL_OR,
    BUCKET,
    BUCKET_FROM,
    COUNT_ROWS,
    CUME_DIST,
    DOW,
    DOY,
    EXCEPT_ALL,
    EXTRA_PARTS,
    FIRST_KNOWN,
    FIRST_KNOWN_RUN,
    FRAMES,
    FULL_JOIN,
    HALF_EVEN,
    IN_ANY,
    INT_DIV,
    INTERSECT_ALL,
    LAG_DEFAULT,
    LAST_KNOWN,
    LAST_KNOWN_RUN,
    LAST_ROW,
    LEAD_DEFAULT,
    LEFT,
    LEFT_JOIN,
    LENGTH,
    LIKE_EACH,
    MILLISECOND,
    MOD,
    MOVING_NULLS,
    NAME,
    NON_NULL,
    NTH,
    NTH_WHOLE,
    NTILE,
    NULL_LIKE,
    NULL_OF,
    NULLIF_ANY,
    NULLS,
    ORDERED,
    OTHERS,
    PARSE,
    PERCENT_RANK,
    QUARTER,
    RANGE_FRAMES,
    REPLACE,
    REPLACE_FIRST,
    RESERVED,
    REVERSE,
    RIGHT,
    ROUND,
    ROUND_TO,
    RUNNING_MAX,
    RUNNING_MIN,
    RUNNING_SUM,
    SIGN,
    SUBSTRING,
    SUBSTRING_FROM,
    TEMPORAL_PARSE,
    TEXT_AGG,
    TEXT_BOOL,
    TEXT_CMP,
    TEXT_FILL,
    TEXT_GREATEST,
    TEXT_KEY,
    TEXT_LEAST,
    TEXT_LENGTH,
    TEXT_MAX,
    TEXT_MIN,
    TEXT_NULL,
    TEXT_OF,
    TEXT_PART,
    TEXT_PICK,
    TEXT_SPREAD,
    TRUNC,
    TRY_PARSE,
    TUPLE_IN,
    TYPES,
)
from sqlglot_q.temporal import CASTS as PART_CASTS
from sqlglot_q.temporal import interval, span_nanoseconds

SPREAD = "{(count x)#y}"
CASTS = {name: kind for kind, (name, _) in TYPES.items()}
CASTS["symbol"] = exp.DataType.Type.VARCHAR

AGGREGATES = {"sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}
MONADIC = {
    "abs": exp.Abs,
    "upper": exp.Upper,
    "lower": exp.Lower,
    "floor": exp.Floor,
    "ceiling": exp.Ceil,
    "sqrt": exp.Sqrt,
    "exp": exp.Exp,
    "log": exp.Ln,
    "sin": exp.Sin,
    "cos": exp.Cos,
    "tan": exp.Tan,
    "asin": exp.Asin,
    "acos": exp.Acos,
    "atan": exp.Atan,
}
GLYPHS = {
    "=": exp.EQ,
    "<>": exp.NEQ,
    ">": exp.GT,
    ">=": exp.GTE,
    "<": exp.LT,
    "<=": exp.LTE,
    "+": exp.Add,
    "-": exp.Sub,
    "*": exp.Mul,
    "%": exp.Div,
    "&": exp.And,
    "|": exp.Or,
    "and": exp.And,
    "or": exp.Or,
}
# q functions with no SQL counterpart outside a window (windows_read.py).
WINDOW_MONADS = {"sums", "avgs", "maxs", "mins", "differ", "til", "last"}
WINDOW_DYADS = {"xprev", "msum", "mavg", "mmax", "mmin", "mcount"}
KEYWORD_DYADS = {
    "and",
    "or",
    "mod",
    "in",
    "within",
    "like",
    "xasc",
    "xdesc",
    "sublist",
    "lj",
    "ij",
    "uj",
    "xkey",
    "cross",
    "inter",
    "except",
    "union",
    "xcol",
    "insert",
    "upsert",
    "wavg",
    "wsum",
    "xbar",
    "xexp",
    "xlog",
    "div",
    "fby",
    "cor",
    "cov",
    "scov",
    *WINDOW_DYADS,
}
STATISTICS = {
    "sdev": exp.Stddev,
    "dev": exp.StddevPop,
    "svar": exp.Variance,
    "var": exp.VariancePop,
}
MONADS = (
    {"signum", "trim", "ltrim", "rtrim", "med", "any", "asc", "deltas", "prev", "next"}
    | set(STATISTICS)
    | WINDOW_MONADS
    | {"enlist", "not", "null", "neg", "count", "distinct", "string", "first", "where"}
    | set(AGGREGATES)
    | set(MONADIC)
)
QUERY_WORDS = {"select", "exec", "update", "delete"}
CLAUSE_WORDS = {"by", "from", "where"}


@dataclass
class Token:
    kind: str  # name, sym, num, str, bool, op, lambda, punct
    text: str
    value: object = None
    spaced: bool = False  # whitespace before it


TOKEN = re.compile(
    r"""
    (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<lambda>\{)
  | (?P<temporal>(?:\d{4}\.\d{2}\.\d{2}(?:D\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?
      |\d+D(?:\d{2}(?::\d{2}(?::\d{2}(?:\.\d+)?)?)?)?
      |\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?![A-Za-z0-9_.]))
  | (?P<bool>[01]+b(?![A-Za-z0-9_]))
  | (?P<num>0[NnWw][a-z]?|\d+(?:\.\d*)?(?:e[+-]?\d+)?[a-z]?|\.\d+(?:e[+-]?\d+)?[a-z]?)
  | (?P<sym>(?:`[A-Za-z0-9_.:]*)+)
  | (?P<name>\.z\.[DPT](?![A-Za-z0-9_.])|[A-Za-z][A-Za-z0-9_.]*)
  | (?P<op>~\\:|~'|,'|,\\:|,/:|=/:|<>|<=|>=|[=<>+\-*%&|^#~,!@$?_])
  | (?P<punct>[()\[\];:])
    """,
    re.VERBOSE,
)
NOUN_END = {"name", "sym", "num", "str", "bool", "lambda", "temporal"}


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(text):
        if text[i] in " \t\r\n":
            i += 1
            continue
        m = TOKEN.match(text, i)
        if not m:
            raise ParseError(f"q: cannot read {text[i : i + 20]!r}")
        kind = m.lastgroup
        spaced = i > 0 and text[i - 1] in " \t\r\n"
        if kind == "lambda":
            depth, j = 0, i
            while j < len(text):
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
                if depth == 0:
                    break
            if j < len(text) and text[j] == "'":
                j += 1  # {...}' : the lambda applied each
            tokens.append(Token("lambda", text[i:j], spaced=spaced))
            i = j
            continue
        word = m.group()
        prev = tokens[-1] if tokens else None
        noun_before = prev is not None and (prev.kind in NOUN_END or prev.text in (")", "]"))
        if kind == "op" and word == "-" and (not noun_before or spaced) and m.end() < len(text):
            # -1 after an operator, at the start, or after a space is a
            # negative literal (abs -3 is abs[-3], n -1 is n[-1]); a-1 and
            # a - 1 are subtraction.
            n = TOKEN.match(text, m.end())
            if n and n.lastgroup == "num" and n.start() == m.end():
                tokens.append(Token("num", "-" + n.group(), spaced=spaced))
                i = n.end()
                continue
        if kind in ("num", "temporal") and prev is not None and prev.kind == kind and spaced:
            # 1 2 3 is one vector literal.
            prev.value = [*(prev.value or [prev.text]), word]
            i = m.end()
            continue
        if kind == "str":
            tokens.append(Token("str", word, value=unescape(word[1:-1]), spaced=spaced))
        elif kind == "sym":
            tokens.append(Token("sym", word, value=word.split("`")[1:], spaced=spaced))
        else:
            tokens.append(Token(kind, word, spaced=spaced))
        i = m.end()
    return tokens


def unescape(body: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


def split_statements(text: str) -> list[str]:
    """Statements are separated by a top-level `;`, or a line that does not
    start with whitespace (an indented line continues the one above)."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("/"):
            continue
        code = re.sub(r"\s/.*$", "", line)
        if line[:1] in " \t" and lines:
            lines[-1] += " " + code.strip()
        else:
            lines.append(code)
    out = []
    for line in lines:
        depth, start, in_str = 0, 0, False
        for i, ch in enumerate(line):
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str:
                depth += {"(": 1, "[": 1, "{": 1, ")": -1, "]": -1, "}": -1}.get(ch, 0)
                if ch == ";" and depth == 0:
                    out.append(line[start:i])
                    start = i + 1
        out.append(line[start:])
    return [s.strip() for s in out if s.strip()]


class QParser:
    """Parses q text into sqlglot expressions, one per statement."""

    def __init__(self, dialect=None, **opts):
        self.opts = opts
        generator = getattr(dialect, "generator_class", None)
        # The dialect's FUNCTIONS, read backwards: wavg[a;b] is VWAP(a, b)
        # when the dialect maps VWAP to wavg.
        self.functions = {q: sql for sql, q in getattr(generator, "FUNCTIONS", {}).items()}

    def parse(self, text: str) -> list[exp.Expr]:
        return [
            Statement(tokenize(t), t, self.functions).statement()
            for t in map(insert_forms, split_statements(text))
        ]


class Statement:
    def __init__(self, tokens: list[Token], text: str, functions: dict | None = None):
        self.t = tokens
        self.i = 0
        self.text = text
        self.functions = functions or {}

    # Token stream -----------------------------------------------------------

    def peek(self, k: int = 0) -> Token | None:
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def next(self) -> Token:
        tok = self.peek()
        if tok is None:
            self.fail("unexpected end")
        self.i += 1
        return tok

    def is_(self, text: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok is not None and tok.text == text and tok.kind in ("punct", "op", "name")

    def expect(self, text: str) -> None:
        if not self.is_(text):
            tok = self.peek()
            self.fail(f"expected {text!r}, found {tok.text if tok else 'the end'!r}")
        self.i += 1

    def fail(self, message: str):
        raise ParseError(f"q: {message} in {self.text!r}")

    def stops(self, stop: set[str]) -> bool:
        tok = self.peek()
        return (
            tok is None
            or tok.text in (")", "]", ";")
            or (tok.text in stop and tok.kind in ("name", "op"))
        )

    # Statements -------------------------------------------------------------

    def statement(self) -> exp.Expr:
        tok, after = self.peek(), self.peek(1)
        if tok and tok.kind == "name" and self.is_(":", 1) and self.is_(":", 2):
            # v::select ...: a q view, CREATE VIEW.
            self.i += 3
            query = self.expr(set())
            if not is_query(query):
                self.fail("a view is read when it names a query")
            node = exp.Create(this=exp.to_table(tok.text), kind="VIEW", expression=query)
            return canonical(node)
        if tok and tok.kind == "name" and after and after.text == ":" and after.kind == "punct":
            if self.is_("(", 2) and self.is_("[", 3):
                node = self.create(tok.text)
            else:
                self.i += 2
                query = self.expr(set())
                if not is_query(query):
                    self.fail("an assignment is only read when it names a query")
                node = exp.Create(this=exp.to_table(tok.text), kind="TABLE", expression=query)
        else:
            node = self.expr(set())
        if self.peek() is not None:
            self.fail(f"unexpected {self.peek().text!r}")
        if isinstance(node, TableLiteral):
            node = no_from(self, node)
        if isinstance(node, Sorted) and isinstance(node.this, exp.Column):
            # `c xasc t on its own: t's rows in that order.
            order = node.args["order"]
            node = exp.Select(expressions=[exp.Star()]).from_(
                table(self, node.this.name), copy=False
            )
            node.set("order", order)
        if isinstance(node, Joined):
            # aj[...], ej[...] or t lj k on its own: every column of the join.
            node = select(self, [], [], node, None, False)
        if isinstance(node, Exec):
            # exec c from t, on its own: the values of the one column, which
            # SQL returns as the rows of SELECT c.
            node = node.this
        if isinstance(node, exp.Count) and node.meta.get("of"):
            # count t, outside a select: the rows of the table t.
            node = exp.Select(expressions=[exp.alias_(node, "x")]).from_(
                table(self, node.meta["of"]), copy=False
            )
        for column in node.find_all(exp.Column):
            if column.name == "q_keys" and not column.table:
                self.fail("flip of columns is read only as a window's order keys")
            if column.name == "i" and not column.table:
                self.fail("i is q's row index, and SQL's rows have no order to number")
        from sqlglot_q.qnulls import faithful

        node = canonical(faithful(node.transform(literal)))
        for found in node.find_all(exp.Intersect, exp.Except):
            if found.meta.get("q_bare"):
                self.fail(
                    f"{'inter' if isinstance(found, exp.Intersect) else 'except'} of two "
                    "tables keeps the first's copies; it is read under distinct"
                )
        leftover = node.find(*HELPERS) if not isinstance(node, HELPERS) else node
        if leftover is not None:
            self.fail(f"{type(leftover).__name__.lower()} has no SQL form here")
        return node

    def create(self, name: str) -> exp.Expr:
        self.i += 2
        self.expect("(")
        self.expect("[")
        cols, keys = [], []
        closing = "]"
        while True:
            if self.is_(closing):
                self.next()
                if closing == ")":
                    break
                keys = [c.name for c in cols]
                closing = ")"
                continue
            col = self.next().text
            self.expect(":")
            cast = self.next()
            if cast.kind != "sym" or not self.is_("$"):
                self.fail("CREATE TABLE columns are `type$()")
            self.next()
            self.expect("(")
            self.expect(")")
            kind = CASTS.get(cast.value[0] or "symbol")
            if kind is None:
                self.fail(f"no SQL type for `{cast.value[0]}")
            cols.append(exp.ColumnDef(this=exp.to_identifier(col), kind=exp.DataType(this=kind)))
            if self.is_(";"):
                self.next()
        if keys:
            # ([k:...] v:...) is keyed on k: SQL's PRIMARY KEY.
            cols.append(exp.PrimaryKey(expressions=[exp.to_identifier(k) for k in keys]))
        schema = exp.Schema(this=exp.to_table(name), expressions=cols)
        return exp.Create(this=schema, kind="TABLE")

    # Expressions ------------------------------------------------------------

    def expr(self, stop: set[str]) -> exp.Expr:
        left = self.term(stop)
        tok = self.peek()
        if tok is None or self.stops(stop):
            return left
        if tok.kind == "op" or (tok.kind == "name" and tok.text in KEYWORD_DYADS):
            self.next()
            right = self.expr(stop)
            return self.dyad(tok.text, left, right)
        if tok.kind == "num" and tok.text == "0N" and not tok.value:
            # v 0N, (enlist 1) 0N: a null of v's type, the generator's
            # spelling of SQL's NULL beside a typed value.
            if isinstance(left, (exp.Column, exp.Paren)) or (
                isinstance(left, exp.Paren) and isinstance(left.this, Enlisted)
            ):
                self.next()
                return TypedNull(this=left)
        if isinstance(left, exp.Column) and self.starts_term():
            # Juxtaposition: x where not null x indexes x.
            return Index(this=left, expression=self.expr(stop))
        self.fail(f"cannot apply {tok.text!r} here")

    def starts_term(self) -> bool:
        tok = self.peek()
        if tok is None:
            return False
        if tok.text == "?" and self.is_("[", 1):
            return True  # ?[...]
        return tok.kind in NOUN_END or tok.text in ("(",)

    def term(self, stop: set[str]) -> exp.Expr:
        tok = self.next()
        if tok.kind == "num":
            if tok.value:
                return Rows(items=[number(v) for v in tok.value])
            return number(tok.text)
        if tok.kind == "temporal":
            if tok.value:
                return Rows(items=[temporal(self, v) for v in tok.value])
            return temporal(self, tok.text)
        if tok.kind == "bool":
            bits = tok.text[:-1]
            if len(bits) != 1:
                self.fail("a boolean vector has no SQL form")
            return exp.Boolean(this=bits == "1")
        if tok.kind == "str":
            letters = {v: k for k, v in (PARSE | TEMPORAL_PARSE).items()}
            if tok.value in letters and self.is_("$") and not self.peek(1).kind == "str":
                # "D"$x: q's parse of text, null where it is no date.
                self.next()
                arg = self.expr(stop)
                if isinstance(arg, exp.Cast) and arg.to.this == exp.DataType.Type.VARCHAR:
                    arg = arg.this  # "D"$string d: the symbols' text
                to = next(k for k, v in TYPES.items() if v[0] == letters[tok.value])
                return exp.TryCast(this=literal(arg), to=exp.DataType.build(to))
            return Chars(this=tok.value)
        if tok.kind == "sym":
            if self.is_("$"):
                self.next()
                return self.cast(tok.value[0], stop)
            if len(tok.value) == 1:
                return exp.Literal.string(tok.value[0])
            return Rows(items=[exp.Literal.string(v) for v in tok.value])
        if tok.kind == "lambda":
            return self.lambda_call(tok.text, stop)
        if tok.text == "(":
            return self.parenthesised()
        if tok.text == "?" and self.is_("[") and self.bracket_arity() == 4:
            return self.functional()
        if tok.text == "?" and self.is_("["):
            return self.conditional()
        if tok.kind == "name":
            return self.word(tok.text, stop)
        self.fail(f"unexpected {tok.text!r}")

    def word(self, word: str, stop: set[str]) -> exp.Expr:
        if word in QUERY_WORDS:
            return self.query(word, stop)
        if word in NOW:
            return NOW[word]()
        if word == "flip" and self.is_("("):
            # flip(o;n): a window's order keys, as one key of rows (RANK and
            # DENSE_RANK of several). The window's order is the sort ahead.
            self.next()  # (, which parenthesised expects taken
            self.parenthesised()
            return exp.column("q_keys")
        if word == "ej" and self.is_("["):
            return self.equijoin()
        if word == "aj" and self.is_("["):
            return self.asof_join()
        if word in self.functions:
            name = self.functions[word]
            if self.is_("["):
                self.next()
                args = [self.expr(set())]
                while self.is_(";"):
                    self.next()
                    args.append(self.expr(set()))
                self.expect("]")
            else:
                args = [self.expr(stop)]
            return exp.Anonymous(this=name, expressions=args)
        if word == "count" and self.is_("each"):
            # count each string s: each text's length. (A null symbol's
            # string is "", of length 0, where SQL's LENGTH is NULL.)
            self.next()
            arg = self.expr(stop)
            if isinstance(arg, exp.Cast) and arg.to.this == exp.DataType.Type.VARCHAR:
                inner = arg.this
                length = exp.Length(this=inner)
                return exp.Coalesce(this=length, expressions=[exp.Literal.number(0)])
            self.fail("count each is read on the text of a column, count each string x")
        if word in MONADS and self.is_("[") and not self.is_(";", 2):
            # sum[qty] is sum qty: the one argument in brackets.
            self.expect("[")
            arg = self.expr(set())
            if self.is_("]"):
                self.next()
                return self.monad(word, arg)
            self.fail(f"{word}[...] is read with one argument")
        if word in MONADS and (self.starts_term() or self.peek_query()):
            return self.monad(word, self.expr(stop))
        if word in MONADS | KEYWORD_DYADS | QUERY_WORDS:
            self.fail(f"{word} has no SQL form here")
        if word == "type":
            return exp.column(word)  # only read in (type c)$v, see dyad
        if word in RESERVED or not NAME.match(word):
            self.fail(f"{word!r} is a q keyword, and the writer could not write it back")
        if "." in word:
            # s.city is a foreign-key lookup or a namespaced global in q, and
            # a table-qualified column in SQL.
            self.fail(f"{word}: a dotted name has no SQL form")
        return exp.column(word)

    def peek_query(self) -> bool:
        tok = self.peek()
        return tok is not None and tok.kind == "name" and tok.text in QUERY_WORDS

    def aggregate_of(self, word: str, arg: exp.Expr) -> exp.Expr:
        if isinstance(arg, Index) and isinstance(arg.expression, Where_):
            agg = AGGREGATES[word](this=arg.this)
            return exp.Filter(this=agg, expression=exp.Where(this=arg.expression.this))
        return AGGREGATES[word](this=distinct_of(arg))

    def monad(self, word: str, arg: exp.Expr) -> exp.Expr:
        if word == "not" and isinstance(arg, NoNulls):
            return arg
        if word == "not":
            return exp.Not(this=arg)
        if word == "any" and isinstance(arg, EqEach):
            return exp.In(this=arg.this, expressions=arg.args["items"])
        if word == "any" and isinstance(arg, exp.Is) and isinstance(arg.this, exp.Paren):
            if isinstance(arg.this.this, Exec):
                arg = exp.Is(this=arg.this.this, expression=arg.expression)  # null (exec ...)
        if word == "any" and isinstance(arg, exp.Is) and isinstance(arg.this, Exec):
            return NoNulls(this=arg.this)
        if word == "null":
            return exp.Is(this=arg, expression=exp.Null())
        if word == "neg":
            return exp.Neg(this=arg)
        if word == "string":
            return exp.Cast(this=arg, to=exp.DataType.build("VARCHAR"))
        if word == "sum" and isinstance(arg, NullsAsNull):
            return self.aggregate_of("sum", arg.this)  # SUM, NULL over no values
        if word == "sum" and not boolean(arg):
            # q's sum over no values (or only nulls) is 0; SQL's SUM is NULL.
            total = self.aggregate_of("sum", arg)
            return exp.Coalesce(this=total, expressions=[exp.Literal.number(0)])
        if word == "first" and isinstance(arg, exp.GroupConcat):
            return arg
        if word == "first" and lifted(arg):
            # first f[enlist `abc]: the generator's constant over a list of one.
            return arg.transform(
                lambda n: n.this if isinstance(n, Enlisted) and is_literal(n.this) else n
            )
        if (
            word == "first"
            and isinstance(arg, Enlisted)
            and (call := scalars.find(arg.this)) is not None
            and call.aggregate
        ):
            return arg.this  # first enlist f[...]: an aggregate of scalars.py
        if word == "first" and isinstance(arg, Enlisted) and isinstance(arg.this, exp.Count):
            return arg.this  # first enlist COUNT_ROWS[...]: COUNT(DISTINCT a, b)
        if (
            word == "first"
            and isinstance(arg, Enlisted)
            and isinstance(arg.this, exp.Anonymous)
            and ANONYMOUS.get(arg.this.name, ("", False))[1]
        ):
            return arg.this  # first enlist PRODUCT[...]
        if (
            word == "first"
            and isinstance(arg, Enlisted)
            and isinstance(arg.this, (exp.LogicalAnd, exp.LogicalOr))
        ):
            return arg.this  # first enlist BOOL_AND[...]
        if word == "first" and isinstance(arg, Enlisted) and isinstance(arg.this, exp.GroupConcat):
            return arg.this  # first enlist: the text form
        if word == "first" and isinstance(arg, TextOrder) and arg.args.get("nonnull"):
            return aggregate(exp.Min, arg.this)
        if word == "last" and isinstance(arg, TextOrder) and not arg.args.get("nonnull"):
            return aggregate(exp.Max, arg.this)
        if word == "first" and isinstance(bare(arg), exp.Column):
            return QCall(this=arg, fn="first")  # FIRST_VALUE, in a window stage
        if word == "first" and isinstance(arg, QCall) and arg.args["fn"] == "asc":
            inner = arg.this
            if isinstance(inner, NonNull):
                return aggregate(exp.Min, inner.this)  # first asc of the non-nulls
        if word == "last" and isinstance(arg, QCall) and arg.args["fn"] == "asc":
            return aggregate(exp.Max, arg.this)  # last asc: nulls sort first
        if word == "sum" and boolean(arg):
            return exp.CountIf(this=bare(arg))  # sum c counts the rows where c holds
        if word in STATISTICS:
            return STATISTICS[word](this=arg)
        if word in ("med", "first") and non_null(arg):
            # med x where not null x: q's med counts nulls, MEDIAN skips them
            return (exp.Median if word == "med" else exp.AnyValue)(this=arg.this)
        if word in AGGREGATES:
            if isinstance(arg, Index) and isinstance(arg.expression, Where_):
                # sum x where c: SUM(x) FILTER (WHERE c)
                agg = AGGREGATES[word](this=arg.this)
                return exp.Filter(this=agg, expression=exp.Where(this=arg.expression.this))
            return AGGREGATES[word](this=distinct_of(arg))
        if word == "signum":
            # q's signum of a null is -1 (a null is below zero); SQL's SIGN is NULL
            minus = exp.Neg(this=exp.Literal.number(1))
            null = exp.Is(this=arg.copy(), expression=exp.Null())
            return exp.Case(ifs=[exp.If(this=null, true=minus)], default=exp.Sign(this=arg))
        if word in ("deltas", "prev", "next"):
            if word == "deltas":
                # deltas keeps the first value: x - LAG(x, 1, 0)
                return exp.Sub(this=arg, expression=QCall(this=arg.copy(), fn="lag0"))
            n = exp.Literal.number(1) if word == "prev" else exp.Neg(this=exp.Literal.number(1))
            return QCall(this=arg, fn="xprev", n=n)
        if word in ("trim", "ltrim", "rtrim"):
            inner = arg.this if isinstance(arg, exp.Cast) else arg
            position = {"ltrim": "LEADING", "rtrim": "TRAILING"}.get(word)
            return exp.Trim(this=inner, position=position)
        if word in MONADIC:
            return MONADIC[word](this=arg)
        if word == "sums" and not isinstance(arg, QCall):
            # q's sums of a leading run of nulls is 0; SQL's running SUM, NULL.
            running = QCall(this=arg, fn="sums")
            return exp.Coalesce(this=running, expressions=[exp.Literal.number(0)])
        if word in WINDOW_MONADS or word == "asc":
            return QCall(this=arg, fn=word)
        if word == "count" and isinstance(bare(arg), exp.Column) and bare(arg).name != "i":
            node = exp.Count(this=exp.Star())  # count x is the rows, nulls and all
            node.meta["of"] = bare(arg).name  # at the top, count t: t's rows
            return node
        if word == "count" and is_query(arg):
            return CountRows(this=arg)
        if word == "count":
            if isinstance(arg, exp.Column) and arg.name == "i":
                return exp.Count(this=exp.Star())
            if isinstance(arg, Distinct) and non_null(arg.this):
                return exp.Count(this=exp.Distinct(expressions=[arg.this.this]))
            if non_null(arg):
                return exp.Count(this=arg.this)
            if (
                isinstance(arg, Index)
                and isinstance(arg.this, exp.Column)
                and arg.this.name == "i"
                and isinstance(arg.expression, Where_)
            ):
                # count i where c: COUNT(*) FILTER (WHERE c)
                return exp.Filter(
                    this=exp.Count(this=exp.Star()),
                    expression=exp.Where(this=arg.expression.this),
                )
            self.fail("count is only read as COUNT(*), COUNT(x) and COUNT(DISTINCT x)")
        if word == "enlist":
            # enlist "a" is the one-character string; ([] x:enlist v) is a
            # one-row table.
            if isinstance(arg, Chars) and len(arg.this) == 1 and not arg.meta.get("one"):
                # enlist "a": the writer's one-character string.
                arg.meta["one"] = True
                return arg
            # enlist "abc", enlist enlist "a": a list of one string.
            return Enlisted(this=arg)
        if word == "distinct":
            if isinstance(arg, (exp.Intersect, exp.Except)) and not arg.args.get("distinct"):
                arg.set("distinct", True)  # distinct a inter b: INTERSECT
                arg.meta.pop("q_bare", None)
                return arg
            if is_query(arg):
                arg.set("distinct", exp.Distinct())
                return arg
            return Distinct(this=arg)
        if word == "where":
            return Where_(this=arg)
        if word == "first" and isinstance(arg, Exec):
            return exp.Subquery(this=arg.this)
        if word == "first" and not arg.find(*HELPERS):
            # first (upper g) of a group: FIRST of it, the group's one value.
            return exp.First(this=literal(bare(arg)))
        self.fail(f"{word} has no SQL form here")

    def cast(self, type_name: str, stop: set[str]) -> exp.Expr:
        if self.is_("("):
            # `$("a b") is a symbol literal.
            save = self.i
            self.next()
            tok = self.peek()
            if tok and tok.kind == "str" and self.is_(")", 1):
                self.i += 2
                return exp.Literal.string(tok.value)
            self.i = save
        tok = self.peek()
        if tok and tok.kind == "lambda" and tok.text == HALF_EVEN:
            self.next()
        arg = self.expr(stop)
        if type_name == "" and isinstance(arg, exp.Cast):
            return arg  # `$string x
        if (
            type_name == ""
            and isinstance(bare(arg), Rows)
            and all(isinstance(i, Chars) for i in bare(arg).args["items"])
        ):
            # `$("Group";"my col"): a symbol list, names xcol gives columns.
            return Rows(items=[exp.Literal.string(i.this) for i in bare(arg).args["items"]])
        if type_name == "" and isinstance(bare(arg), (exp.Column, Chars)):
            # (`$w)>`$"b": text compared as symbols, in the text's order
            return literal(bare(arg))
        if type_name == "" and isinstance(bare(arg), exp.Concat):
            return bare(arg)  # `$(a,'b): the joined text, as a symbol
        if type_name == "" and isinstance(bare(arg), (Joins, exp.Trim)):
            arg = bare(arg)
            return concat(self, arg) if isinstance(arg, Joins) else arg
        if type_name in PART_CASTS:
            return exp.Extract(this=exp.var(PART_CASTS[type_name]), expression=arg)
        kind = CASTS.get(type_name)
        if kind is None:
            self.fail(f"no SQL type for `{type_name}")
        if type_name == "symbol":
            kind = exp.DataType.Type.VARCHAR
        return exp.Cast(this=arg, to=exp.DataType(this=kind))

    def lambda_call(self, text: str, stop: set[str]) -> exp.Expr:
        if text in (LEFT_JOIN, FULL_JOIN) and self.is_("["):
            joined = self.equijoin()
            joined.set("side", "LEFT" if text == LEFT_JOIN else "FULL")
            return joined
        if text in (TEXT_OF, TEXT_PART):
            return exp.Cast(this=self.expr(stop), to=exp.DataType.build("VARCHAR"))
        if text == scalars.ADD_MONTHS and self.is_("["):
            self.expect("[")
            arg = self.expr(set())
            self.expect(";")
            n = literal(self.expr(set()))
            self.expect("]")
            count = n.this.this if isinstance(n, exp.Neg) else n.this
            if not (isinstance(n, (exp.Literal, exp.Neg)) and str(count).isdigit()):
                self.fail("months are read as a literal count")
            span = exp.Interval(this=exp.Literal.string(str(count)), unit=exp.var("MONTH"))
            kind = exp.Sub if isinstance(n, exp.Neg) else exp.Add
            return kind(this=arg, expression=span)
        call = scalars.READ.get(text)
        if call is not None and self.is_("["):
            self.expect("[")
            args = [self.expr(set())]
            while self.is_(";"):
                self.next()
                args.append(self.expr(set()))
            self.expect("]")
            node = scalars.read(call, [literal(a) for a in args])
            if node is None:
                self.fail("a lambda with the wrong arguments")
            return node
        joined = re.fullmatch(r"\{(?:`\$)?\((.*?)\) sv x where 0<count each x:(.*) each x\}", text)
        if joined is not None and joined.group(2) not in (TEXT_OF, TEXT_PART):
            joined = None
        if joined is not None and self.is_("each"):
            # The generator's CONCAT_WS, of the parts' non-null text.
            toks = [t for t in tokenize(joined.group(1)) if t.text != "enlist"]
            if not (len(toks) == 1 and toks[0].kind == "str"):
                self.fail("a lambda has no SQL form")
            self.next()
            if self.is_("flip"):
                self.next()
                rows = self.expr(stop)
                if not isinstance(rows, Rows):
                    self.fail("CONCAT_WS is read over a list of columns")
                parts = [literal(p) for p in rows.args["items"]]
            else:
                self.expect("enlist")
                self.expect("each")
                parts = [literal(self.expr(stop))]
            sep = exp.Literal.string(str(toks[0].value))
            return exp.ConcatWs(expressions=[sep, *parts], safe=True, coalesce=True)
        if text == BUCKET_FROM and self.is_("["):
            self.expect("[")
            width = self.expr(set())
            self.expect(";")
            value = self.expr(set())
            self.expect(";")
            origin = self.expr(set())
            self.expect("]")
            return exp.DateBin(this=width, expression=value, unit=literal(origin))
        if text == BUCKET and self.is_("["):
            self.expect("[")
            width = self.expr(set())
            self.expect(";")
            value = self.expr(set())
            self.expect("]")
            if not isinstance(width, exp.Interval):
                self.fail("a bucket is read with a timespan width")
            return exp.DateBin(this=width, expression=value)
        if text == TEXT_SPREAD and self.is_("["):
            self.expect("[")
            self.expr(set())  # i: the partition's rows
            self.expect(";")
            value = self.expr(set())
            self.expect("]")
            return value
        if text in ORDERED.values() and self.is_("["):
            # x in a key's order, an aggregate's ORDER BY.
            (desc, first) = next(k for k, v in ORDERED.items() if v == text)
            self.expect("[")
            value = self.expr(set())
            self.expect(";")
            key = self.expr(set())
            self.expect("]")
            term = exp.Ordered(this=literal(key), desc=desc, nulls_first=first)
            return exp.Order(this=literal(value), expressions=[term])
        if text == COUNT_ROWS and self.is_("["):
            self.expect("[")
            parts = self.expr(set())
            self.expect("]")
            items = parts.args["items"] if isinstance(parts, Rows) else [parts]
            return exp.Count(this=exp.Distinct(expressions=[literal(i) for i in items]))
        if text in REPLACE_FIRST and self.is_("["):
            self.expect("[")
            args = [self.expr(set())]
            while self.is_(";"):
                self.next()
                args.append(self.expr(set()))
            self.expect("]")
            if len(args) != 3:
                self.fail("REGEXP_REPLACE is read with three arguments")
            value, pattern, replacement = (literal(a) for a in args)
            return exp.RegexpReplace(
                this=value, expression=pattern, replacement=replacement, single_replace=True
            )
        if text == NULLIF_ANY and self.is_("["):
            self.expect("[")
            value = self.expr(set())
            self.expect(";")
            cond = bare(self.expr(set()))
            self.expect("]")
            if not isinstance(cond, exp.EQ):
                self.fail("NULLIF is read with an equality")
            other = cond.expression if cond.this == value else cond.this
            return exp.Nullif(this=literal(value), expression=literal(other))
        if text in (TEXT_GREATEST, TEXT_LEAST) and self.is_("["):
            self.expect("[")
            first = self.expr(set())
            self.expect(";")
            second = self.expr(set())
            self.expect("]")
            kind = exp.Greatest if text == TEXT_GREATEST else exp.Least
            head = literal(first)
            if isinstance(head, kind):  # a fold of three or more
                items = [*head.expressions, literal(second)]
                return kind(this=head.this, expressions=items, ignore_nulls=True)
            return kind(this=head, expressions=[literal(second)], ignore_nulls=True)
        if text == TEXT_BOOL:
            arg = self.expr(stop)
            return exp.Cast(this=literal(arg), to=exp.DataType.build("BOOLEAN"))
        if text == NULL_LIKE:
            arg = self.expr(stop)
            return TypedNull(this=arg)  # the null of a column's type, a CASE's ELSE
        if text == TEXT_CMP:
            return self.expr(stop)  # strings compared as symbols: the values
        if text == ANY_FILL and self.is_("["):
            self.expect("[")
            first = self.expr(set())
            self.expect(";")
            second = self.expr(set())
            self.expect("]")
            return exp.Coalesce(this=literal(first), expressions=[literal(second)])
        if text == TEXT_NULL:
            return exp.Is(this=self.expr(stop), expression=exp.Null())  # empty: NULL
        if text == TEXT_LENGTH:
            return exp.Length(this=self.expr(stop))
        if text in (TEXT_PICK, TEXT_FILL):
            self.expect("[")
            args = [self.expr(set())]
            while self.is_(";"):
                self.next()
                args.append(self.expr(set()))
            self.expect("]")
            args = [literal(per_row(a)) for a in args]
            if text == TEXT_FILL and len(args) == 2:
                rest = args[1].expressions if isinstance(args[1], exp.Coalesce) else []
                head = args[1].this if isinstance(args[1], exp.Coalesce) else args[1]
                return exp.Coalesce(this=args[0], expressions=[head, *rest])
            if text == TEXT_PICK and len(args) == 3:
                cond, then, other = args
                return self.case_of(cond, then, other)
            self.fail("a text lambda with the wrong arguments")
        if text.startswith("{x i") and TEXT_SORT.match(text):
            m = TEXT_SORT.match(text)
            target = self.expr(stop)
            if m[2] == TEXT_NULL:  # {x iasc TEXT_NULL x`k}: nulls last (or first)
                return null_order(self, target, m[3], first=m[1] == "idesc")
            return ordered(self, exp.Literal.string(m[3]), target, desc=m[1] == "idesc")
        position = POSITION_SORT.match(text)
        if position:
            return by_position(self, position[1], self.expr(stop), desc=position[2] == "xdesc")
        position = POSITION_KEYS.match(text)
        if position:
            # A text key, or nulls moved, of the k-th column.
            target = self.expr(stop)
            if position[2] in (TEXT_NULL, "null"):
                return null_order(self, target, f"#{position[3]}", first=position[1] == "idesc")
            return by_position(self, position[3], target, desc=position[1] == "idesc")
        nulls = NULL_ORDER.match(text)
        if nulls:
            # {x iasc null x`k} r: r with k's nulls moved last (or, with
            # idesc, first), the generator's spelling of NULLS LAST/FIRST.
            # A lambda applies to all of the expression on its right.
            target = self.expr(stop)
            return null_order(self, target, nulls[2], first=nulls[1] == "idesc")
        if text in PART_LAMBDAS:
            return exp.Extract(this=exp.var(PART_LAMBDAS[text]), expression=self.expr(stop))
        if text in TRUNC_LAMBDAS:
            unit = exp.var(TRUNC_LAMBDAS[text])
            return exp.TimestampTrunc(this=self.expr(stop), unit=unit)
        if text in REVERSE:
            return exp.Reverse(this=self.expr(stop))
        if text in (PERCENT_RANK, CUME_DIST):
            return QCall(
                this=self.expr(stop), fn="percent_rank" if text == PERCENT_RANK else "cume_dist"
            )
        if text in (*REPLACE, *SUBSTRING, *SUBSTRING_FROM, NTILE):
            self.expect("[")
            args = [self.expr(set())]
            while self.is_(";"):
                self.next()
                args.append(self.expr(set()))
            self.expect("]")
            args = [literal(a) for a in args]
            if text in REPLACE and len(args) == 3:
                return exp.Replace(this=args[0], expression=args[1], replacement=args[2])
            if text in SUBSTRING and len(args) == 3:
                return exp.Substring(this=args[0], start=args[1], length=args[2])
            if text in SUBSTRING_FROM and len(args) == 2:
                return exp.Substring(this=args[0], start=args[1])
            if text == NTILE and len(args) == 2:
                return QCall(this=args[1], fn="ntile", n=args[0])
            self.fail("a lambda with the wrong arguments")
        if text in (TEXT_MIN_FN, TEXT_MAX_FN):
            return TextOrder(this=self.expr(stop), nonnull=text == TEXT_MIN_FN)
        if TEXT_AGG_TEXT.match(text):
            sep = TEXT_AGG_TEXT.match(text)[1]
            self.expect("[")
            arg = distinct_of(self.expr(set()))
            self.expect("]")
            return exp.GroupConcat(this=arg, separator=exp.Literal.string(unescape(sep)))
        agg = STRING_AGG_TEXT.match(text)
        if agg:
            self.expect("[")
            arg = distinct_of(self.expr(set()))
            self.expect("]")
            return exp.GroupConcat(this=arg, separator=exp.Literal.string(unescape(agg[1])))
        if text == NULLS:
            return NullsAsNull(this=self.expr(stop))
        if text == NON_NULL:
            return NonNull(this=self.expr(stop))
        if text in (LENGTH, MOD):
            if text == LENGTH:
                return exp.Length(this=self.expr(stop))
            self.expect("[")
            x = self.expr(set())
            self.expect(";")
            y = self.expr(set())
            self.expect("]")
            return exp.Mod(this=x, expression=y)
        if text == RUNNING_SUM:
            return QCall(this=self.expr(stop), fn="sums")
        if text in (RUNNING_MAX, RUNNING_MIN):
            return QCall(this=self.expr(stop), fn="maxs" if text == RUNNING_MAX else "mins")
        for letter, name in {v: k for k, v in (PARSE | TEMPORAL_PARSE).items()}.items():
            if text == TRY_PARSE.replace("L", letter):
                to = next(k for k, v in TYPES.items() if v[0] == name)
                return exp.TryCast(this=self.expr(stop), to=exp.DataType.build(to))
        named = next((n for n, (fn, _) in ANONYMOUS.items() if fn == text), None)
        if named is not None and self.is_("["):
            self.expect("[")
            args = [self.expr(set())]
            while self.is_(";"):
                self.next()
                args.append(self.expr(set()))
            self.expect("]")
            return exp.Anonymous(this=named, expressions=[literal(a) for a in args])
        if text == IN_ANY and self.is_("["):
            self.expect("[")
            subject = self.expr(set())
            self.expect(";")
            values = self.expr(set())
            self.expect("]")
            return self.dyad("in", subject, values)
        if text == TUPLE_IN and self.is_("["):
            self.expect("[")
            parts = self.expr(set())
            self.expect(";")
            query = self.expr(set())
            self.expect("]")
            while isinstance(query, exp.Paren):
                query = query.this
            items = parts.args.get("items") if isinstance(parts, Rows) else None
            if not items or not is_query(query):
                self.fail("(a, b) IN is read with a list of values and a query")
            row = exp.Tuple(expressions=[literal(i) for i in items])
            return exp.In(this=row, query=exp.Subquery(this=query))
        if text == LIKE_EACH and self.is_("["):
            self.expect("[")
            subject = self.expr(set())
            self.expect(";")
            pattern = self.expr(set())
            self.expect("]")
            if isinstance(pattern, exp.Cast) and pattern.to.this == exp.DataType.Type.VARCHAR:
                pattern = pattern.this  # string p: the symbols' text
            return exp.Like(this=subject, expression=literal(pattern))
        if text in (INTERSECT_ALL, EXCEPT_ALL) and self.is_("["):
            self.expect("[")
            left = self.expr(set())
            self.expect(";")
            right = self.expr(set())
            self.expect("]")
            if not (is_query(left) and is_query(right)):
                self.fail("INTERSECT ALL and EXCEPT ALL are read over two queries")
            kind = exp.Intersect if text == INTERSECT_ALL else exp.Except
            return kind(this=left, expression=right, distinct=False)
        if text in (BOOL_AND, BOOL_OR) and self.is_("["):
            self.expect("[")
            cond = self.expr(set())
            self.expect(";")
            self.expr(set())  # where it is known: read from the condition
            self.expect("]")
            kind = exp.LogicalAnd if text == BOOL_AND else exp.LogicalOr
            return kind(this=cond)
        if text == OTHERS and self.is_("["):
            # A join side without another side's columns: the side itself,
            # as SQL tells them apart by table.
            self.expect("[")
            side = bare(self.expr(set())).copy()
            self.expect(";")
            names = self.expr(set())
            self.expect("]")
            items = names.args["items"] if isinstance(names, Rows) else [names]
            # Remembered: the names another side has, which qualify columns.
            side.meta["drops"] = [i.this for i in items]
            return side
        if text == LAST_ROW:
            return QCall(this=self.expr(stop), fn="lastrow")
        known = {
            FIRST_KNOWN: "firstknown",
            LAST_KNOWN: "lastknown",
            FIRST_KNOWN_RUN: "firstknownrun",
            LAST_KNOWN_RUN: "lastknownrun",
        }
        if text in known:
            return QCall(this=self.expr(stop), fn=known[text])
        if text in (NTH, NTH_WHOLE) and self.is_("["):
            self.expect("[")
            arg = self.expr(set())
            self.expect(";")
            n = self.expr(set())
            self.expect("]")
            return QCall(this=arg, fn="nth" if text == NTH else "nthwhole", n=n)
        if text in RANGE_FRAMES.values():
            fn = next(k for k, v in RANGE_FRAMES.items() if v == text)
            self.expect("[")
            args = [self.expr(set())]
            while self.is_(";"):
                self.next()
                args.append(self.expr(set()))
            self.expect("]")
            if len(args) != 3:
                self.fail("a time frame lambda with the wrong arguments")
            return QCall(this=args[2], fn=f"rframe_{fn}", n=args[0])
        if text in FRAMES.values():
            fn = next(k for k, v in FRAMES.items() if v == text)
            self.expect("[")
            args = [self.expr(set())]
            while self.is_(";"):
                self.next()
                args.append(self.expr(set()))
            self.expect("]")
            if len(args) != 3:
                self.fail("a frame lambda with the wrong arguments")
            return QCall(this=args[2], fn=f"frame_{fn}", n=args[0], default=args[1])
        if text in MOVING_NULLS.values():
            fn = next(k for k, v in MOVING_NULLS.items() if v == text)
            self.expect("[")
            n = self.expr(set())
            self.expect(";")
            arg = self.expr(set())
            self.expect("]")
            return QCall(this=arg, fn=fn, n=n)
        if text == SIGN:
            return exp.Sign(this=self.expr(stop))
        if text in (LAG_DEFAULT, LEAD_DEFAULT):
            self.expect("[")
            args = [self.expr(set())]
            while self.is_(";"):
                self.next()
                args.append(self.expr(set()))
            self.expect("]")
            if len(args) != 3:
                self.fail("a lambda with the wrong arguments")
            fn = "lagd" if text == LAG_DEFAULT else "leadd"
            return QCall(this=args[0], fn=fn, n=args[1], default=literal(args[2]))
        if text in (*LEFT, *RIGHT):
            self.expect("[")
            n = self.expr(set())
            self.expect(";")
            target = self.expr(set())
            self.expect("]")
            node = exp.Left if text in LEFT else exp.Right
            return node(this=target, expression=n)
        if text in (ROUND, ROUND_TO, INT_DIV):
            if text == ROUND:
                return exp.Round(this=self.expr(stop))
            self.expect("[")
            x = self.expr(set())
            self.expect(";")
            y = self.expr(set())
            self.expect("]")
            if text == ROUND_TO:
                return exp.Round(this=x, decimals=y)
            return exp.IntDiv(this=x, expression=y)
        if text != SPREAD or not self.is_("["):
            self.fail("a lambda has no SQL form")
        self.next()
        index = self.expr(set())
        if not (isinstance(index, exp.Column) and index.name == "i"):
            self.fail("a lambda has no SQL form")
        self.expect(";")
        value = self.expr(set())
        self.expect("]")
        return value

    def parenthesised(self) -> exp.Expr:
        if self.is_(")"):
            self.fail("an empty list has no SQL form")
        if self.is_("[") and self.is_("]", 1):
            return self.table_literal()
        tok = self.peek()
        if tok and tok.kind == "name" and tok.text in FBY_AGGREGATES and self.is_(";", 1):
            # (max;qty) fby sym: the aggregate of qty per sym, on each row.
            self.next()
            self.next()
            arg = self.expr(set())
            self.expect(")")
            return FbyPair(this=arg, fn=tok.text)
        items = [self.expr(set())]
        while self.is_(";"):
            self.next()
            items.append(self.expr(set()))
        self.expect(")")
        if len(items) > 1:
            return Rows(items=items)
        inner = items[0]
        if is_query(inner) or isinstance(inner, (exp.Subquery, Sorted, Renamed, Joined)):
            return inner
        if isinstance(inner, Joins):
            return concat(self, inner)  # (w,'v): text joined, in string mode
        return exp.Paren(this=inner)

    def table_literal(self) -> exp.Expr:
        self.i += 2
        cols, values = [], []
        while not self.is_(")"):
            name = self.next()
            self.expect(":")
            cols.append(name.text)
            values.append(self.expr(set()))
            if self.is_(";"):
                self.next()
        self.expect(")")
        return TableLiteral(columns=cols, values=values)

    def case_of(self, cond, then, other) -> exp.Expr:
        pipes = dpipe_of(cond, then, other)
        if pipes is not None:
            return pipes
        then = exp.Null() if is_empty_text(then) else then
        other = None if is_empty_text(other) else other
        ifs = [exp.If(this=cond, true=then)]
        if isinstance(other, exp.Case) and other.this is None:
            ifs += other.args["ifs"]
            other = other.args.get("default")
        return exp.Case(ifs=ifs, default=other)

    def bracket_arity(self) -> int:
        """The arguments of the bracket ahead: ; at its own depth, plus one."""
        depth, count, j = 0, 1, self.i
        while j < len(self.t):
            text = self.t[j].text
            if text in "([" and self.t[j].kind == "punct":
                depth += 1
            elif text in ")]" and self.t[j].kind == "punct":
                depth -= 1
                if depth == 0:
                    return count
            elif text == ";" and depth == 1:
                count += 1
            j += 1
        return count

    def functional(self) -> exp.Expr:
        """?[t;c;b;a], q's functional select: c a list of parse trees
        (constraints), b 0b, 1b (distinct) or a dict of by keys, a () or a
        dict of columns. Each tree is read as the plain select's expression."""
        self.expect("[")
        source = self.expr(set())
        self.expect(";")
        constraints = self.tree()
        self.expect(";")
        by = self.tree()
        self.expect(";")
        columns = self.tree()
        self.expect("]")

        def items(node) -> list:
            return node if isinstance(node, list) else [node]

        conds = [] if constraints == [] else items(constraints)
        if conds and not isinstance(conds[0], list) and isinstance(constraints, list):
            conds = [constraints]  # enlist (>;`a;1) read as the tree itself
        where = conjunction([self.from_tree(c) for c in conds]) if conds else None
        distinct, keys = False, []
        if isinstance(by, exp.Boolean):
            distinct = by.this
        elif isinstance(by, dict):
            keys = [exp.alias_(self.from_tree(v), k) for k, v in by.items()]
        elif by != []:
            self.fail("a functional select's by is 0b, 1b or a dict")
        if isinstance(columns, dict):
            cols = [exp.alias_(self.from_tree(v), k) for k, v in columns.items()]
        elif columns == []:
            cols = []
        else:
            self.fail("a functional select's columns are () or a dict")
        if keys:
            cols = [per_group(c, keys) for c in cols]
        return select(self, cols, keys, source, where, distinct)

    def tree(self):
        """A q parse tree: (f;x;y) a list, `c a column's name, enlist v a
        literal v, k!v a dict, () empty."""
        tok = self.next()
        if tok.text == "(":
            if self.is_(")"):
                self.next()
                return []
            parts = [self.tree_item()]
            while self.is_(";"):
                self.next()
                parts.append(self.tree_item())
            self.expect(")")
            node = parts if len(parts) > 1 else parts[0]
        elif tok.kind == "name" and tok.text == "enlist":
            inner = self.tree()
            node = [inner] if isinstance(inner, list) else ("lit", inner)
        elif tok.kind == "sym":
            node = ("col", tok.value) if len(tok.value) > 1 else ("col", tok.value[0])
        elif tok.kind == "bool":
            node = exp.Boolean(this=tok.text[:-1] == "1")
        elif tok.kind in ("num", "str", "temporal"):
            self.i -= 1
            node = ("lit", self.term(set()))
        else:
            self.fail(f"unexpected {tok.text!r} in a parse tree")
        if self.is_("!"):
            self.next()
            values = self.tree()
            if isinstance(node, tuple) and node[0] == "lit" and isinstance(node[1], tuple):
                node = node[1]  # (enlist `n)!...: a list of one name
            if isinstance(values, tuple) and values[0] == "lit" and isinstance(values[1], tuple):
                values = values[1]  # ...!enlist (count;`i): a list of one tree
            names = node[1] if isinstance(node, tuple) and node[0] == "col" else None
            if isinstance(node, list):
                names = [n[1] for n in node]
            if isinstance(names, str):
                names = [names]
            if isinstance(values, tuple) and values[0] == "col" and isinstance(values[1], list):
                values = [("col", v) for v in values[1]]  # `a`b: a column each
            if (
                isinstance(values, list)
                and len(values) == 1
                and isinstance(values[0], list)
                and len(names) == 1
            ):
                values = values[0] if values[0] and isinstance(values[0][0], list) else values
            elif not (isinstance(values, list) and len(names) > 1):
                values = [values]
            if names is None or len(names) != len(values):
                self.fail("a dict in a parse tree pairs names with trees")
            return dict(zip(names, values, strict=True))
        return node

    def tree_item(self):
        tok = self.peek()
        named = tok.kind == "name" and tok.text in MONADS | KEYWORD_DYADS and tok.text != "enlist"
        if tok.kind == "op" or named:
            self.next()
            return ("fn", tok.text)
        return self.tree()

    def from_tree(self, node) -> exp.Expr:
        if isinstance(node, tuple) and node[0] == "col":
            names = node[1]
            if isinstance(names, list):
                return exp.column(names[0]) if len(names) == 1 else self.fail("one name")
            return exp.column(names)
        if isinstance(node, tuple) and node[0] == "lit":
            value = node[1]
            if isinstance(value, tuple) and value[0] == "col":
                return exp.Literal.string(value[1])  # enlist `a: the symbol `a
            return value
        if isinstance(node, list) and node and isinstance(node[0], tuple) and node[0][0] == "fn":
            fn, args = node[0][1], [self.from_tree(a) for a in node[1:]]
            if len(args) == 2:
                return self.dyad(fn, args[0], args[1])
            if len(args) == 1:
                return self.monad(fn, args[0]) if fn in MONADS else self.fail(f"{fn} of one")
            self.fail(f"{fn} of {len(args)} arguments has no SQL form here")
        if isinstance(node, exp.Expr):
            return node
        self.fail("a parse tree with no SQL form here")

    def conditional(self) -> exp.Expr:
        self.expect("[")
        cond = self.expr(set())
        self.expect(";")
        then = self.expr(set())
        self.expect(";")
        other = self.expr(set())
        self.expect("]")
        least = least_of(cond, then, other)
        if least is not None:
            return least
        then = exp.Null() if isinstance(then, TypedNull) else then
        if isinstance(other, TypedNull):
            other = None  # no ELSE
        ifs = [exp.If(this=cond, true=then)]
        # ?[a;b;?[c;d;e]] is one CASE with two WHENs.
        if isinstance(other, exp.Case) and other.this is None:
            ifs += other.args["ifs"]
            other = other.args.get("default")
        return exp.Case(ifs=ifs, default=other)

    def equijoin(self) -> exp.Expr:
        self.expect("[")
        keys = self.next()
        if keys.kind != "sym":
            self.fail("ej takes a symbol list of keys")
        self.expect(";")
        left = self.expr(set())
        self.expect(";")
        right = self.expr(set())
        self.expect("]")
        return Joined(this=left, expression=right, keys=keys.value, side="")

    def asof_join(self) -> exp.Expr:
        self.expect("[")
        keys = self.next()
        if keys.kind != "sym":
            self.fail("aj takes a symbol list of keys")
        self.expect(";")
        left = self.expr(set())
        self.expect(";")
        right = self.expr(set())
        self.expect("]")
        if isinstance(right, Sorted):
            # aj[k;t;k xasc q]: the sort is aj's precondition, not part of
            # the query.
            right = right.this
        return Joined(this=left, expression=right, keys=keys.value, side="LEFT", method="ASOF")

    def dyad(self, op: str, left: exp.Expr, right: exp.Expr) -> exp.Expr:
        if op == "<" and isinstance(left, exp.Literal) and left.this == "0":
            if isinstance(right, CountRows):
                return exp.Exists(this=right.this)  # 0<count (select ...)
        if op in ("=", "<>") and any(chars(n) for n in (left, right)):
            # = on a string compares it character by character; SQL's = on
            # strings is q's ~\: (match each).
            self.fail("= on a string compares characters: SQL equality is ~\\:")
        if op in ("&", "and") and guard_of(left, right):
            # (not null a)&a<3: the generator's guard for SQL's a < 3, which
            # is never true on a null already. Under a guard, q's null-safe
            # = and <> are SQL's plain ones.
            right = bare(right)
            if isinstance(right, exp.NullSafeEQ):
                right = exp.EQ(this=right.this, expression=right.expression)
            elif isinstance(right, exp.NullSafeNEQ):
                right = exp.NEQ(this=right.this, expression=right.expression)
            right.meta["guarded"] = True
            return right
        if op in ("&", "and") and isinstance(bare(right), NoNulls):
            # (not x in S)&not any null S: SQL's x NOT IN (S)
            return left
        flag = lambda n: boolean(n) or isinstance(bare(n), exp.Column)  # noqa: E731
        if (
            op in ("or", "and")
            and not (boolean(left) and boolean(right))
            and flag(left)
            and flag(right)
        ):
            # The generator's f and a>2: a column taken as a condition.
            kind = exp.And if op == "and" else exp.Or
            return kind(this=left, expression=right)
        if op in ("|", "&", "or", "and") and not (boolean(left) and boolean(right)):
            # | and & are max and min; only between conditions are they OR
            # and AND. max skips a null as GREATEST does; & does not, so a
            # bare min has no SQL form (least_of reads the generator's).
            if op in ("&", "and"):
                return QMin(this=left, expression=right)
            items = right.expressions if isinstance(right, exp.Greatest) else []
            first = right.this if isinstance(right, exp.Greatest) else right
            # ignore_nulls: as DuckDB's GREATEST, which skips a null.
            return exp.Greatest(this=left, expressions=[first, *items], ignore_nulls=True)
        if op == "<>" or (op == "=" and nullable_side(left) and nullable_side(right)):
            # q's <> is true for a null against a value, and = for two nulls:
            # SQL's IS [NOT] DISTINCT FROM, not its = and <>.
            node = exp.NullSafeNEQ if op == "<>" else exp.NullSafeEQ
            return node(this=left, expression=right)
        if op in GLYPHS:
            return GLYPHS[op](this=left, expression=right)
        if op in (",'", ",\\:", ",/:") or (op == "," and (text_part(left) or text_part(right))):
            parts = []
            for side in (bare(left), bare(right)):
                parts += side.expressions if isinstance(side, Joins) else [side]
            return Joins(expressions=parts)
        if op == "#" and isinstance(bare(left), exp.Count) and isinstance(right, (Enlisted, Chars)):
            inner = right.this if isinstance(right, Enlisted) else right
            return PerRow(this=inner)
        if op == "=/:" and isinstance(right, Rows):
            return EqEach(this=left, items=right.args["items"])
        if op == "xexp":
            return exp.Pow(this=left, expression=right)
        if op in ("wavg", "wsum"):
            # q drops a pair with a null on either side.
            total = exp.Sum(this=exp.Mul(this=left, expression=right))
            if op == "wsum":
                return exp.Coalesce(this=total, expressions=[exp.Literal.number(0)])
            known = exp.Not(this=exp.Is(this=right.copy(), expression=exp.Null()))
            weight = exp.Case(ifs=[exp.If(this=known, true=left.copy())])
            return exp.Div(this=total, expression=exp.Sum(this=weight))
        if op == "xlog":
            return exp.Log(this=left, expression=right)
        if op in ("cor", "cov", "scov"):
            node = {"cor": exp.Corr, "cov": exp.CovarPop, "scov": exp.CovarSamp}[op]
            return node(this=left, expression=right)
        if op == "upsert":
            node = inserted(self, left, right)
            node.set("alternative", "REPLACE")  # INSERT OR REPLACE
            return node
        if (
            op == "$"
            and isinstance(left, Chars)
            and left.this in PARSE_TYPES
            and isinstance(bare(right), Chars)
        ):
            kind = PARSE_TYPES[left.this]
            return exp.Cast(this=exp.Literal.string(bare(right).this), to=exp.DataType(this=kind))
        if op == "$" and isinstance(left, exp.Paren) and isinstance(left.this, Index):
            fn = left.this.this
            if isinstance(fn, exp.Column) and fn.name == "type":
                return TypedAs(this=right)
        if op in WINDOW_DYADS:
            return QCall(this=right, fn=op, n=left)
        if op == "?" and isinstance(bare(left), exp.Column) and bare(left) == bare(right):
            right = bare(right)
            return QCall(this=right, fn="find")
        if op == "xbar":
            if isinstance(left, exp.Interval):
                from sqlglot_q.temporal import UNITS, nanoseconds

                ns = nanoseconds(left)
                unit = next((u for u, n in UNITS.items() if n == ns and u != "DAY"), None)
                timestamp = isinstance(right, exp.Cast) and right.to.this in (
                    exp.DataType.Type.TIMESTAMP,
                    exp.DataType.Type.DATETIME,
                )
                if unit is not None and timestamp:
                    # 0D01 xbar `timestamp$x: the generator's DATE_TRUNC('hour', x).
                    return exp.TimestampTrunc(this=right.this, unit=exp.var(unit))
                return exp.DateBin(this=left, expression=right)
            # n xbar x is x rounded down to a multiple of n.
            return exp.Mul(
                this=exp.Floor(this=exp.Div(this=right, expression=left)), expression=left
            )
        if op == "fby" and isinstance(left, FbyPair):
            # (max;qty) fby sym: MAX(qty) OVER (PARTITION BY sym).
            keys = right.args["items"] if isinstance(right, Rows) else [right]
            if not all(isinstance(k, exp.Column) for k in keys):
                self.fail("fby is read grouped by columns")
            kind = FBY_AGGREGATES[left.args["fn"]]
            value = exp.Count(this=exp.Star()) if kind is exp.Count else kind(this=left.this)
            return exp.Window(this=value, partition_by=list(keys), over="OVER")
        if op == "div":
            # x div y floors: FLOOR(x / y).
            return exp.Floor(this=exp.Div(this=left, expression=right))
        if op == "mod":
            # q's mod floors (-7 mod 3 is 2); SQL's % takes the dividend's
            # sign (-1). ((x % y) + y) % y is the floor modulo in SQL.
            inner = exp.Add(this=exp.Mod(this=left, expression=right), expression=right.copy())
            return exp.Mod(this=inner, expression=right.copy())
        if op == "^":
            # x^y is y with its nulls filled from x: COALESCE(y, x).
            items = right.expressions if isinstance(right, exp.Coalesce) else []
            first = right.this if isinstance(right, exp.Coalesce) else right
            return exp.Coalesce(this=first, expressions=[*items, left])
        if op == "~\\:":
            return exp.EQ(this=left, expression=literal(right))
        if op == "~'":
            return exp.EQ(this=left, expression=right)  # strings matched row by row
        if op == "in":
            while isinstance(right, exp.Paren) and isinstance(right.this, (Exec, exp.Paren)):
                right = right.this  # in (exec x from ...): parenthesised in a where list
            if isinstance(right, exp.Subquery):
                return exp.In(this=left, query=right)
            if isinstance(right, Exec):
                return exp.In(this=left, query=exp.Subquery(this=right.this))
            if isinstance(right, Enlisted):
                right = Rows(items=[right.this])
            if isinstance(right, (exp.Literal, Chars)):
                right = Rows(items=[right])  # sym in `a: a list of one
            if not isinstance(right, Rows) or not all(
                isinstance(i, (exp.Literal, exp.Null, exp.Boolean, exp.Neg, Chars))
                for i in right.args["items"]
            ):
                # x in z tests against z's items; SQL's IN (z) against z.
                self.fail("in is read with a list of literals or a subquery")
            return exp.In(this=left, expressions=right.args["items"])
        if op == "within":
            if not isinstance(right, Rows) or len(right.args["items"]) != 2:
                self.fail("within takes two bounds")
            low, high = right.args["items"]
            return exp.Between(this=left, low=low, high=high)
        if op == "like":
            if not isinstance(right, Chars):
                self.fail("like takes a literal string pattern")
            pattern = sql_pattern(right.this)
            if isinstance(left, exp.Paren) and isinstance(left.this, exp.Lower):
                test = exp.ILike(this=left.this.this, expression=exp.Literal.string(pattern))
            else:
                test = exp.Like(this=left, expression=exp.Literal.string(pattern))
            if any(ch in "%_" for ch in right.this):
                # A % or _ q matches as itself: SQL's ESCAPE.
                return exp.Escape(this=test, expression=exp.Literal.string("\\"))
            return test
        if op in ("xasc", "xdesc"):
            return ordered(self, left, right, desc=op == "xdesc")
        if op == "sublist":
            return limited(self, left, right)
        if op == "_" and isinstance(left, exp.Literal) and is_query(right):
            return with_modifier(right, offset=left)
        if op == "!" and isinstance(left, exp.Literal) and left.this == "0" and is_query(right):
            return right  # 0! unkeys; SQL's GROUP BY result is unkeyed already
        if op == "xkey" and isinstance(left, (exp.Literal, Rows)):
            keys = (
                [left.this]
                if isinstance(left, exp.Literal)
                else [r.this for r in left.args["items"]]
            )
            return Keyed(this=right, keys=keys)
        if op == "!" and isinstance(left, exp.Literal) and left.this.isdigit() and is_query(right):
            # n!q: q keyed on its first n columns.
            names = [i.alias_or_name for i in right.expressions][: int(left.this)]
            if len(names) != int(left.this) or not all(names):
                self.fail("n!q is read when q names its first n columns")
            return Keyed(this=right, keys=names)
        if op in ("lj", "ij"):
            keyed = keyed_of(right)
            if keyed is None and isinstance(bare(right), Keyed):
                # `k xkey q, n!q: keys q may repeat, and lj takes the first
                # match where SQL's join takes every one.
                self.fail(f"{op} is read with a table keyed by a grouping (select ... by k)")
            if keyed is not None:
                # lj with a keyed table (a select by k is keyed on k): a LEFT
                # JOIN on the keys, which match once; ij an inner join.
                query, keys = keyed
                side = "LEFT" if op == "lj" else ""
                return Joined(this=left, expression=query, keys=keys, side=side)
        if op == "uj" and is_query(bare(left)) and is_query(bare(right)):
            # x uj y: the rows of both, columns matched by name.
            return exp.Union(this=bare(left), expression=bare(right), distinct=False, by_name=True)
        if op == "cross":
            return Joined(this=left, expression=right, keys=[], side="CROSS")
        if op == "xcol":
            return renamed(self, left, right)
        if op in (",", "inter", "except", "union"):
            # ([] g:enlist `a): a SELECT of constants, as a set operation's side.
            def constants(side):
                inner = side
                while isinstance(inner, exp.Paren):
                    inner = inner.this
                return no_from(self, inner) if isinstance(inner, TableLiteral) else side

            left, right = constants(left), constants(right)
        if op == "union" and is_query(left) and is_query(right):
            # x union y is distinct x,y: SQL's UNION.
            return exp.Union(this=left, expression=right, distinct=True)
        if op == "," and is_query(left) and is_query(right):
            return exp.Union(this=left, expression=right, distinct=False)
        if op in ("inter", "except") and is_query(left) and is_query(right):
            kind = exp.Intersect if op == "inter" else exp.Except
            node = kind(this=left, expression=right, distinct=False)
            # Read only under distinct: a inter b keeps a's copies, which is
            # neither INTERSECT nor INTERSECT ALL.
            node.meta["q_bare"] = True
            return node
        if op == "insert":
            return inserted(self, left, right)
        self.fail(f"{op!r} has no SQL form here")

    # Queries ----------------------------------------------------------------

    def query(self, word: str, stop: set[str]) -> exp.Expr:
        if word == "update":
            return self.update(stop)
        if word == "delete":
            return self.delete(stop)
        limit = offset = sort = None
        if word == "select" and self.is_("["):
            # select[n]: the first n rows, SQL's LIMIT n; select[n;>c] sorts
            # the result on c first, select[>c] only sorts.
            self.next()
            if not (self.is_(">") or self.is_("<")):
                limit = self.expr(set())
                if self.is_(";"):
                    self.next()
            if self.is_(">") or self.is_("<"):
                desc = self.next().text == ">"
                sort = (self.expr(set()), desc)
            self.expect("]")
            offset = None
            if limit is None:
                pass
            elif isinstance(limit, Rows) and len(limit.args["items"]) == 2:
                # select[m n]: n rows from the m-th, LIMIT n OFFSET m.
                offset, limit = limit.args["items"]
                if not (isinstance(offset, exp.Literal) and offset.this.isdigit()):
                    self.fail("select[m n] is read with counts m, n >= 0")
            if limit is not None and not (isinstance(limit, exp.Literal) and limit.this.isdigit()):
                self.fail("select[n] is read with a count n >= 0; select[-n] takes the last rows")
        distinct = False
        if word == "select" and self.is_("distinct"):
            self.next()
            distinct = True
        cols = self.phrase({"by", "from"})
        keys = []
        if self.is_("by"):
            self.next()
            keys = self.phrase({"from"})
        self.expect("from")
        source = self.expr(stop | {"where"})
        where = None
        if self.is_("where"):
            self.next()
            where = self.conditions(stop)
        qualify = None
        if where is not None and where.find(exp.Window):
            # fby in a where: a window, which SQL filters on in QUALIFY,
            # after WHERE; so only when the fby constraints come last.
            parts = list(where.flatten()) if isinstance(where, exp.And) else [where]
            first = next(i for i, p in enumerate(parts) if p.find(exp.Window))
            if not all(p.find(exp.Window) for p in parts[first:]):
                self.fail("fby is read when its constraints come after the others")
            qualify = conjunction(parts[first:])
            where = conjunction(parts[:first]) if first else None
        if where is not None:
            where = scalar_aggregates(self, where, source)
        if word == "select":
            # first x and last x of a select: the first and last row's value,
            # nulls and all, as DuckDB's FIRST and LAST in a table's order.
            cols = [first_last(c) for c in cols]
        if word == "select" and not keys and isinstance(source, Sorted):
            cols = sorted_windows(self, cols, source.args["order"])
        if word == "select" and not keys:
            cols = whole_table_windows(cols)
        if keys:
            cols = [per_group(c, keys) for c in cols]
        elif word == "select":
            # select l:enlist n from t: the generator's ARRAY_AGG over all rows.
            cols = [
                exp.alias_(exp.ArrayAgg(this=c.this.this), c.alias)
                if isinstance(c, exp.Alias)
                and isinstance(c.this, Enlisted)
                and not isinstance(c.this.this, (exp.Literal, Chars))
                else c
                for c in cols
            ]
        node = select(self, cols, keys, source, where, distinct)
        if qualify is not None:
            node.set("qualify", exp.Qualify(this=qualify))
        if sort is not None:
            column, desc = sort
            if not (isinstance(column, exp.Column) and not column.table):
                self.fail("select[n;>c] is read with one column c")
            names = {c.alias_or_name for c in node.expressions}
            if not (column.name in names or node.find(exp.Star) and not keys):
                self.fail(f"select[n;>c] sorts the result, which has no {column.name}")
            node = ordered(self, exp.Literal.string(column.name), node, desc=desc)
        if limit is not None and offset is not None:
            node = with_modifier(node, limit=limit, offset=offset)
        elif limit is not None:
            node = with_modifier(node, limit=limit)
        if word == "exec":
            if keys or len(cols) != 1:
                self.fail("exec is only read as the one column of a subquery")
            return Exec(this=exec_query(node))
        return node

    def conditions(self, stop: set[str]) -> exp.Expr:
        items = self.phrase(stop)
        if not items or any(isinstance(i, exp.Alias) for i in items):
            # where px:1 assigns; it is not a condition SQL can state.
            self.fail("a where phrase is read as conditions only")
        return conjunction(items)

    def phrase(self, stop: set[str]) -> list[exp.Expr]:
        items = []
        if self.stops(stop):
            return items
        while True:
            items.append(self.item(stop))
            if self.is_(","):
                self.next()
                continue
            return items

    def item(self, stop: set[str]) -> exp.Expr:
        tok, after = self.peek(), self.peek(1)
        if tok and tok.kind == "name" and after and after.text == ":" and after.kind == "punct":
            self.i += 2
            return exp.alias_(self.expr(stop | {","}), tok.text)
        return self.expr(stop | {","})

    def update(self, stop: set[str]) -> exp.Expr:
        items = self.phrase({"by", "from"})
        keys = []
        if self.is_("by"):
            self.next()
            keys = self.phrase({"from"})
        self.expect("from")
        tok = self.peek()
        if keys or not (tok and tok.kind == "sym" and len(tok.value) == 1):
            # update ... from a table value returns a new table: the
            # generator's window stages (windows.py), read back as one.
            from sqlglot_q.windows_read import window_stage

            return window_stage(self, items, keys, self.expr(stop))
        self.i -= 1  # back to from, for the statement form below
        sets = []
        for item in items:
            if not isinstance(item, exp.Alias):
                self.fail("update sets name:value")
            value = item.this
            # (type c)$v, which keeps an amend's type, is SQL's plain SET.
            if isinstance(value, TypedAs):
                value = value.this
            sets.append(exp.EQ(this=exp.column(item.alias), expression=value))
        self.expect("from")
        target = self.table_symbol()
        where = None
        if self.is_("where"):
            self.next()
            where = exp.Where(this=self.conditions(stop))
        return exp.Update(this=target, expressions=sets, where=where)

    def delete(self, stop: set[str]) -> exp.Expr:
        if not self.is_("from"):
            names = [self.next().text]
            while self.is_(","):
                self.next()
                names.append(self.next().text)
            self.expect("from")
            ns = self.peek()
            if not (ns and ns.kind == "sym" and ns.value == ["."]):
                # delete a,b from a table value: SELECT * EXCLUDE (a, b).
                source = self.expr(stop)
                if isinstance(bare(source), exp.Column):
                    # delete a from t: t's other columns.
                    source = exp.Select(expressions=[exp.Star()]).from_(
                        table(self, bare(source).name), copy=False
                    )
                if not is_query(source):
                    self.fail("delete of columns is read from a query, as SELECT * EXCLUDE")
                return excluded(self, names, source)
            self.next()
            if len(names) != 1:
                self.fail("delete of several names from `. has no SQL form")
            target = table(self, names[0])
            # sqlglot moved DROP's table from `this` to a `tables` list in 30.x.
            if "tables" in exp.Drop.arg_types:
                return exp.Drop(tables=[target], kind="TABLE")
            return exp.Drop(this=target, kind="TABLE")
        self.next()
        tok = self.peek()
        if not (tok and tok.kind == "sym"):
            # delete from t where c, of a table value: t without the rows
            # where c holds, SELECT * WHERE NOT c (q's c is never unknown).
            source = self.expr(stop | {"where"})
            if isinstance(bare(source), exp.Column):
                source = table(self, bare(source).name)
            elif is_query(source):
                source = exp.Subquery(this=source)
            else:
                self.fail("delete from is read on a table or a query")
            node = exp.Select(expressions=[exp.Star()]).from_(source, copy=False)
            if self.is_("where"):
                self.next()
                node.set(
                    "where", exp.Where(this=exp.Not(this=exp.Paren(this=self.conditions(stop))))
                )
            return node
        target = self.table_symbol()
        where = None
        if self.is_("where"):
            self.next()
            where = exp.Where(this=self.conditions(stop))
        return exp.Delete(this=target, where=where)

    def table_symbol(self) -> exp.Table:
        tok = self.next()
        if tok.kind != "sym" or len(tok.value) != 1:
            self.fail("update and delete are read on a table named by symbol, `t")
        return table(self, tok.value[0])


# Nodes that only exist while reading --------------------------------------


class Rows(exp.Expression):
    """A q list: a vector literal or (a;b;c)."""

    arg_types = {"items": True}


class Index(exp.Expression):
    arg_types = {"this": True, "expression": True}


class Distinct(exp.Expression):
    arg_types = {"this": True}


class Where_(exp.Expression):
    arg_types = {"this": True}


class Exec(exp.Expression):
    arg_types = {"this": True}


class Keyed(exp.Expression):
    arg_types = {"this": True, "keys": True}


class Joined(exp.Expression):
    arg_types = {"this": True, "expression": True, "keys": True, "side": False, "method": False}


class QCall(exp.Expression):
    """A q function with no SQL counterpart outside a window: sums, til,
    xprev, msum and so on. windows_read.py turns them into windows."""

    arg_types = {"this": True, "fn": True, "n": False, "default": False}


class Renamed(exp.Expression):
    """update a:k from u: u with k copied to a, a join side."""

    arg_types = {"this": True, "pairs": True}


class TypedNull(exp.Expression):
    """v 0N: a null of v's type."""

    arg_types = {"this": True}


class QMin(exp.Expression):
    """x&y on values: q's min, which does not skip a null."""

    arg_types = {"this": True, "expression": True}


class Joins(exp.Expression):
    """Text joined with , ,' ,\\: or ,/: : SQL's CONCAT once `$ casts it."""

    arg_types = {"expressions": True}


class NoNulls(exp.Expression):
    """not any null S: S has no null."""

    arg_types = {"this": True}


class NullsAsNull(exp.Expression):
    arg_types = {"this": True}


class NonNull(exp.Expression):
    arg_types = {"this": True}


class EqEach(exp.Expression):
    arg_types = {"this": True, "items": True}


class CountRows(exp.Expression):
    arg_types = {"this": True}


class TextOrder(exp.Expression):
    arg_types = {"this": True, "nonnull": False}


class PerRow(exp.Expression):
    """(count i)#enlist v: the literal v on every row."""

    arg_types = {"this": True}


class Chars(exp.Expression):
    """A q string literal, kept apart from symbols until its use is known."""

    arg_types = {"this": True}


FBY_AGGREGATES = {
    "max": exp.Max,
    "min": exp.Min,
    "sum": exp.Sum,
    "avg": exp.Avg,
    "count": exp.Count,
}


class FbyPair(exp.Expression):
    """(agg;x), fby's left side."""

    arg_types = {"this": True, "fn": True}


class TableLiteral(exp.Expression):
    arg_types = {"columns": True, "values": True}


class Enlisted(exp.Expression):
    arg_types = {"this": True}


class TypedAs(exp.Expression):
    arg_types = {"this": True}


def temporal(parser: Statement, text: str) -> exp.Expr:
    """A q date, timestamp, time or timespan literal as SQL's typed literal."""
    if "D" in text and "." in text.split("D")[0]:
        day, clock = text.split("D")
        ns = clock.split(".")[1] if "." in clock else ""
        body = f"{day.replace('.', '-')} {clock.split('.')[0]}"
        frac = ns.rstrip("0")
        value = f"{body}.{frac}" if frac else body
        if len(clock.split(".")[0]) == 5:
            value = f"{body}:00"
        return exp.Cast(this=exp.Literal.string(value), to=exp.DataType.build("TIMESTAMP"))
    if "D" in text:
        ns = span_nanoseconds(text)
        if ns is None:
            parser.fail(f"cannot read the timespan {text}")
        return interval(ns)
    if re.match(r"\d{4}\.", text):
        return exp.Cast(
            this=exp.Literal.string(text.replace(".", "-")), to=exp.DataType.build("DATE")
        )
    clock, _, frac = text.partition(".")
    if len(clock) == 5:
        clock += ":00"
    frac = frac.rstrip("0")
    value = f"{clock}.{frac}" if frac else clock
    return exp.Cast(this=exp.Literal.string(value), to=exp.DataType.build("TIME"))


def chars(node: exp.Expr) -> bool:
    return isinstance(node, Chars)


def literal(node: exp.Expr) -> exp.Expr:
    """A string literal whose use did not decide otherwise is SQL text."""
    return exp.Literal.string(node.this) if isinstance(node, Chars) else node


def number(text: str) -> exp.Expr:
    if text in ("0N", "0n") or text[:2] in ("0N", "0n"):
        return exp.Null()
    negative = text.startswith("-")
    body = text[1:] if negative else text
    suffix = body[-1] if body[-1].isalpha() else ""
    if suffix:
        body = body[:-1]
    if suffix in ("f", "e") or "." in body or "e" in body:
        body = repr(float(body))
    elif suffix not in ("", "j", "i", "h"):
        raise ParseError(f"q: no SQL literal for {text}")
    lit = exp.Literal.number(body)
    return exp.Neg(this=lit) if negative else lit


def non_null(node: exp.Expr) -> bool:
    """x where not null x."""
    return (
        isinstance(node, Index)
        and isinstance(node.expression, Where_)
        and isinstance(node.expression.this, exp.Not)
        and isinstance(node.expression.this.this, exp.Is)
        and node.expression.this.this.this == node.this
    )


def exec_query(node: exp.Select) -> exp.Select:
    """exec x from (select x:max a from u), the generator's spelling of a
    one-column subquery, as SELECT MAX(a) FROM u."""
    col = node.expressions[0]
    source = node.args["from_"].this
    if (
        isinstance(col, exp.Column)
        and col.name == "x"
        and isinstance(source, exp.Subquery)
        and isinstance(source.this, exp.Select)
        and len(source.this.expressions) == 1
        and isinstance(source.this.expressions[0], exp.Alias)
        and source.this.expressions[0].alias == "x"
        and not any(node.args.get(k) for k in ("where", "joins", "group"))
    ):
        inner = source.this
        inner.set("expressions", [inner.expressions[0].this])
        return inner
    return node


def is_query(node: exp.Expr) -> bool:
    return isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except))


def conjunction(items: list[exp.Expr]) -> exp.Expr:
    node = items[0]
    for item in items[1:]:
        node = exp.And(this=node, expression=item)
    return node


def sql_pattern(q: str) -> str:
    """A q like pattern as SQL's; a % or _ that q matches as itself, and so
    a backslash, is escaped with a backslash (read with ESCAPE '\\')."""
    literal = any(ch in "%_" for ch in q)
    out, i = [], 0
    while i < len(q):
        ch = q[i]
        if ch == "[" and i + 2 < len(q) and q[i + 2] == "]":
            ch = q[i + 1]
            i += 3
        else:
            i += 1
            if ch in "*?":
                out.append({"*": "%", "?": "_"}[ch])
                continue
        out.append(f"\\{ch}" if literal and ch in "%_\\" else ch)
    return "".join(out)


def source_table(parser: Statement, source: exp.Expr) -> tuple[exp.Expr, list[exp.Join]]:
    """The FROM operand and its joins."""
    if isinstance(source, Joined):
        keys = source.args["keys"]
        side = source.args.get("side")
        this, right = source.this, source.expression
        # The generator drops null keys from the side matched against (a
        # null key matches nothing in SQL); that filter is the join's own.
        if side == "LEFT" and source.args.get("method") != "ASOF":
            right = without_null_keys(right, keys)
        elif not side:
            this = without_null_keys(this, keys)
        left, joins = source_table(parser, this)
        renamed = {}
        if isinstance(right, Renamed) and side == "LEFT":
            pairs = right.args["pairs"]
            names = [dict(pairs).get(k, k) for k in keys]
            right = Renamed(this=without_null_keys(right.this, names), pairs=pairs)
        if isinstance(right, Renamed):
            renamed = dict(right.args["pairs"])
            right = right.this
        right = exp.Subquery(this=right) if is_query(right) else as_table(parser, right)
        keys = source.args["keys"]
        side = source.args.get("side")
        if side == "CROSS":
            join = exp.Join(this=right, kind="CROSS")
        elif renamed:
            # ON t.a = u.k: the right side's key was copied to the left's name.
            if isinstance(right, exp.Subquery) and not right.alias:
                # A query joined on a renamed key: named, so ON can say whose.
                right.set("alias", exp.TableAlias(this=exp.to_identifier(f"q_s{len(joins)}")))
            lname, rname = left.alias_or_name, right.alias_or_name
            if not (lname and rname) or set(renamed) - set(keys):
                parser.fail("a renamed join side is read between two named tables")
            conds = [
                exp.EQ(
                    this=exp.column(k, table=lname),
                    expression=exp.column(renamed.get(k, k), table=rname),
                )
                for k in keys
            ]
            join = exp.Join(this=right, on=conjunction(conds), side=side or None)
        else:
            using = [exp.to_identifier(k) for k in keys]
            join = exp.Join(
                this=right, using=using, side=side or None, method=source.args.get("method")
            )
        return left, [*joins, join]
    if is_query(source):
        return exp.Subquery(this=source), []
    return as_table(parser, source), []


def table(parser: Statement, name: str) -> exp.Table:
    if "." in name or not name:
        parser.fail(f"`{name}: a namespaced table has no SQL form")
    if name in RESERVED:
        parser.fail(f"{name!r} is a q keyword, and the writer could not write it back")
    return exp.to_table(name)


def as_table(parser: Statement, node: exp.Expr) -> exp.Expr:
    if isinstance(node, exp.Paren) and isinstance(
        node.this, (exp.Column, TableLiteral, exp.Subquery)
    ):
        node = node.this
    if isinstance(node, exp.Subquery) and node.args.get("alias"):
        return node  # `a`b xcol q: q with its columns renamed
    if isinstance(node, TableLiteral):
        # ([] k:1 2;s:`a`b) as a source: (VALUES (1, 'a'), (2, 'b')) AS q_v(k, s).
        cols = [
            r.args["items"] if isinstance(r, Rows) else [r.this] if isinstance(r, Enlisted) else [r]
            for r in node.args["values"]
        ]
        cols = [[literal(v) for v in c] for c in cols]
        rows = [exp.Tuple(expressions=list(r)) for r in zip(*cols, strict=True)]
        alias = exp.TableAlias(
            this=exp.to_identifier("q_v"),
            columns=[exp.to_identifier(c) for c in node.args["columns"]],
        )
        return exp.Values(expressions=rows, alias=alias)
    if isinstance(node, exp.Column):
        found = table(parser, node.name)
        if node.meta.get("drops"):
            found.meta["drops"] = node.meta["drops"]
        return found
    parser.fail("a from operand is read as a table name, a join or a query")


def select(parser, cols, keys, source, where, distinct) -> exp.Select:
    order = None
    if isinstance(source, Sorted):
        # select ... from (`c xasc t): SQL's ORDER BY a column of the source.
        order, source = source.args["order"], source.this
    table, joins = source_table(parser, source)
    group_items = []
    for key in keys:
        group_items.append(key)
    expressions = [*group_items, *cols]
    if not expressions:
        expressions = [exp.Star()]
    node = exp.Select(expressions=expressions)
    node.set("from_", exp.From(this=table))
    if joins:
        node.set("joins", joins)
    if where is not None:
        node.set("where", exp.Where(this=where))
    if keys:
        node.set("group", exp.Group(expressions=[unalias(k) for k in keys]))
    if distinct:
        node.set("distinct", exp.Distinct())
    if order is not None:
        node.set("order", order)
    if joins:
        qualify_dropped(node)
    from sqlglot_q.windows_read import fold

    return fold(node)


def unalias(node: exp.Expr) -> exp.Expr:
    return node.this if isinstance(node, exp.Alias) else node


def qualify_dropped(node: exp.Select) -> None:
    """The generator's join sides left out the names another side's columns
    have (OTHERS): a name only one side keeps is that side's, t.n."""
    sides = [node.args["from_"].this, *(j.this for j in node.args["joins"])]

    def drops(side) -> list[str]:
        inner = side.this if isinstance(side, exp.Subquery) else side
        return side.meta.get("drops") or inner.meta.get("drops") or []

    names = {n for side in sides for n in drops(side)}
    owner = {}
    for name in names:
        keeping = [s for s in sides if name not in drops(s)]
        if len(keeping) != 1:
            continue
        side = keeping[0]
        if isinstance(side, exp.Subquery) and not side.alias:
            # A query joined: named, so its columns can be told apart.
            side.set("alias", exp.TableAlias(this=exp.to_identifier(f"q_s{sides.index(side)}")))
        if side.alias_or_name:
            owner[name] = side.alias_or_name
    if not owner:
        return
    for col in node.find_all(exp.Column):
        if col.table or col.find_ancestor(exp.Join) is not None or col.name not in owner:
            continue
        if col.find_ancestor(exp.Select) is node:
            col.set("table", exp.to_identifier(owner[col.name]))


def sorted_windows(parser, cols: list[exp.Expr], order: exp.Order) -> list[exp.Expr]:
    """select c:sums x from `o xasc t: q's running functions over the sorted
    rows, windows ordered as the sort (sums is ROWS UNBOUNDED PRECEDING)."""
    from sqlglot_q.windows_read import function

    def convert(node: exp.Expr) -> exp.Expr:
        if isinstance(node, QCall):
            found = function(parser, node, order, non_null, QCall)
            if found is not None:
                fn, spec, ordered = found
                win = exp.Window(this=fn, spec=spec, over="OVER")
                if ordered:
                    win.set("order", order.copy())
                return win
        return node

    return [c.transform(convert) for c in cols]


def whole_table_windows(cols: list[exp.Expr]) -> list[exp.Expr]:
    """select x:qty%sum qty from t: beside a column of the rows, an aggregate
    is the whole table's, on every row: SQL's aggregate OVER ()."""

    inside = (exp.AggFunc, exp.Filter, exp.Subquery, exp.Select, exp.Window)

    def rowwise(node: exp.Expr) -> bool:
        return any(c.find_ancestor(*inside) is None for c in node.find_all(exp.Column))

    def aggregates(node: exp.Expr) -> bool:
        return any(
            a.find_ancestor(exp.Subquery, exp.Select) is None for a in node.find_all(exp.AggFunc)
        )

    if not any(rowwise(c) for c in cols) or not any(aggregates(c) for c in cols):
        return cols

    def over(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Filter) or (
            isinstance(node, exp.AggFunc)
            and not isinstance(node.parent, (exp.Window, exp.Filter))
            and node.find_ancestor(exp.Subquery, exp.Select) is None
        ):
            return exp.Window(this=node.copy(), over="OVER")
        return node

    return [c.transform(over) for c in cols]


def keyed_of(node: exp.Expr):
    """(query, keys) of a keyed table whose keys are each once: a grouping,
    select ... by k (or `k xkey of one), keyed on its keys."""
    node = bare(node)
    if isinstance(node, Keyed):
        query, keys = node.this, node.args["keys"]
        inner = keyed_of(query)
        return (query, keys) if inner is not None and inner[1] == keys else None
    if isinstance(node, exp.Select) and node.args.get("group"):
        keys = [k.alias_or_name for k in node.args["group"].expressions]
        if all(isinstance(k, exp.Column) for k in node.args["group"].expressions):
            return node, keys
    return None


def first_last(item: exp.Expr) -> exp.Expr:
    def swap(node: exp.Expr) -> exp.Expr:
        if (
            isinstance(node, QCall)
            and node.args["fn"] in ("first", "last")
            and not node.args.get("n")
        ):
            kind = exp.First if node.args["fn"] == "first" else exp.Last
            return kind(this=node.this)
        return node

    return item.transform(swap)


def per_group(item: exp.Expr, keys: list[exp.Expr]) -> exp.Expr:
    """An item of select ... by with no aggregate is each group's values, a
    list: SQL's ARRAY_AGG. (A key, or a constant, is one value.)"""
    value = item.this if isinstance(item, exp.Alias) else item
    names = {(k.this if isinstance(k, exp.Alias) else k).sql() for k in keys}
    names |= {k.alias for k in keys if isinstance(k, exp.Alias)}
    columns = [c for c in value.find_all(exp.Column) if c.name not in names]
    if not columns or value.find(exp.AggFunc, exp.Window, exp.Select, exp.Subquery):
        return item
    if any(ANONYMOUS.get(a.name, ("", False))[1] for a in value.find_all(exp.Anonymous)):
        return item  # PRODUCT(x): an aggregate sqlglot does not know
    unwrapped = bare(value)
    if isinstance(unwrapped, QCall) and unwrapped.args["fn"] == "asc":
        unwrapped = unwrapped.this
    if isinstance(unwrapped, Distinct):
        # (asc distinct x) under by: ARRAY_AGG(DISTINCT x), sorted as written.
        return exp.alias_(exp.ArrayAgg(this=distinct_of(unwrapped)), item.alias_or_name)
    if any(isinstance(n, HELPERS) for n in value.walk()):
        return item  # a q helper the select reads itself
    if not isinstance(item, exp.Alias):
        return exp.alias_(exp.ArrayAgg(this=value), value.name or "x")
    return exp.alias_(exp.ArrayAgg(this=value), item.alias)


def is_literal(node: exp.Expr) -> bool:
    return isinstance(node, (exp.Literal, Chars)) or (
        isinstance(node, exp.Paren) and is_literal(node.this)
    )


def lifted(node: exp.Expr) -> bool:
    """A constant written over lists of one: no column, and a literal enlisted."""
    if node.find(exp.Column, exp.AggFunc, exp.Star, exp.Select):
        return False
    return any(is_literal(e.this) for e in node.find_all(Enlisted))


class Sorted(exp.Expression):
    arg_types = {"this": True, "order": True}


def ordered(parser: Statement, cols: exp.Expr, target: exp.Expr, desc: bool) -> exp.Expr:
    names = (
        [cols.this]
        if isinstance(cols, exp.Literal)
        else [c.this for c in cols.args["items"]]
        if isinstance(cols, Rows)
        else parser.fail("xasc takes a symbol list")
    )
    # q's own null order, said out loud: SQL dialects differ on the default.
    terms = [exp.Ordered(this=exp.column(n), desc=desc, nulls_first=not desc) for n in names]
    return sorted_by(parser, terms, target)


def by_position(parser: Statement, k: str, target: exp.Expr, desc: bool) -> exp.Expr:
    """{((cols x) k) xasc x} r: r sorted on its k-th column, ORDER BY k+1."""
    term = exp.Ordered(this=exp.Literal.number(int(k) + 1), desc=desc, nulls_first=not desc)
    target = target.this if isinstance(target, exp.Paren) else target
    if not is_query(target):
        parser.fail("a sort on a column's position is read on a query")
    return sorted_by(parser, [term], target)


def sorted_by(parser: Statement, terms: list[exp.Ordered], target: exp.Expr) -> exp.Expr:
    if isinstance(target, exp.Paren):
        target = target.this
    if isinstance(target, (exp.Column, Sorted, Joined)):
        # Sorting a table (or a join), not a query: kept until the select
        # reads it.
        inner = target.this if isinstance(target, Sorted) else target
        before = target.args["order"].expressions if isinstance(target, Sorted) else []
        return Sorted(this=inner, order=exp.Order(expressions=[*terms, *before]))
    if not is_query(target):
        parser.fail("xasc is read on a query or a table")
    if target.args.get("limit") or target.args.get("offset"):
        target = exp.select("*").from_(exp.Subquery(this=target))
    before = target.args["order"].expressions if target.args.get("order") else []
    target.set("order", exp.Order(expressions=[*terms, *before]))
    return target


def with_modifier(target: exp.Expr, **mods) -> exp.Expr:
    if any(target.args.get(k) for k in mods):
        target = exp.select("*").from_(exp.Subquery(this=target))
    for key, value in mods.items():
        target.set(
            key, exp.Limit(expression=value) if key == "limit" else exp.Offset(expression=value)
        )
    return target


def limited(parser: Statement, count: exp.Expr, target: exp.Expr) -> exp.Expr:
    if isinstance(bare(target), exp.Column):
        # 5 sublist t: t's first rows.
        target = exp.Select(expressions=[exp.Star()]).from_(
            table(parser, bare(target).name), copy=False
        )
    if isinstance(bare(target), Sorted) and isinstance(bare(target).this, exp.Column):
        sorted_ = bare(target)
        target = exp.Select(expressions=[exp.Star()]).from_(
            table(parser, sorted_.this.name), copy=False
        )
        target.set("order", sorted_.args["order"])
    if not is_query(target):
        parser.fail("sublist is read on a query")
    if isinstance(count, Rows) and len(count.args["items"]) == 2:
        offset, n = count.args["items"]
        return with_modifier(target, limit=n, offset=offset)
    return with_modifier(target, limit=count)


def renamed(parser: Statement, names: exp.Expr, target: exp.Expr) -> exp.Expr:
    while isinstance(names, exp.Paren):
        names = names.this
    new = (
        [names.this]
        if isinstance(names, exp.Literal)
        else [n.this for n in names.args["items"]]
        if isinstance(names, Rows)
        else parser.fail("xcol takes a symbol list")
    )
    if isinstance(bare(target), exp.SetOperation) or (
        isinstance(target, exp.Select) and any(isinstance(i, exp.Star) for i in target.expressions)
    ):
        # Columns not written out: a derived table's column list, FROM (...)
        # AS q_c(a, b), which renames its first columns as xcol does.
        alias = exp.TableAlias(
            this=exp.to_identifier("q_c"), columns=[exp.to_identifier(n) for n in new]
        )
        return exp.Subquery(this=bare(target), alias=alias)
    if not isinstance(target, exp.Select):
        parser.fail("xcol is read on a select")
    items = target.expressions
    if len(new) > len(items):
        parser.fail("xcol names more columns than the select has")
    target.meta["xcol"] = (new, [i.copy() for i in items])  # an insert's columns
    for i, name in enumerate(new):
        value = unalias(items[i])
        if isinstance(value, exp.Column) and value.name == name:
            target.expressions[i] = value  # its own name: the column as it is
            continue
        target.expressions[i] = exp.alias_(value, name, quoted=True)
    return target


TYPED_NULL = re.compile(r"first 0#[A-Za-z]\w*(?:`[A-Za-z]\w*| \(cols [A-Za-z]\w*\) \d+)")
NULL_FILL = re.compile(r"(\d+)#0N\b")
AMEND = re.compile(r"@\[([^;\[\]()]+);([\d ]+);:;0N\]")


def insert_forms(text: str) -> str:
    """The generator's insert, in q this reader reads: a column's typed null
    (first 0#t`c) is 0N, an amend of nulls into a vector is the vector with
    them, and (cols t) xcol q, a positional insert, is q."""
    text = text.replace(NULL_OF + " ", "first 0#")  # string mode's null of a column
    if re.match(r"\s*update ", text):
        # update c:first 0#c: SET c = NULL, the column's own null.
        return re.sub(r"first 0#[A-Za-z]\w*\b", "0N", text)
    if not re.match(r"\s*`[A-Za-z]\w* (insert|upsert) ", text):
        return text
    text = TYPED_NULL.sub("0N", text)

    def amend(m: re.Match) -> str:
        values, nulls = m[1].strip(), {int(i) for i in m[2].split()}
        if values.startswith("`"):
            items = values.split("`")[1:]
            return "`" + "`".join("" if i in nulls else v for i, v in enumerate(items))
        items = values.split()
        return "(" + ";".join("0N" if i in nulls else v for i, v in enumerate(items)) + ")"

    text = AMEND.sub(amend, text)
    text = NULL_FILL.sub(lambda m: "(" + ";".join(["0N"] * int(m[1])) + ")", text)
    return re.sub(r"\(cols [A-Za-z]\w*\) xcol ", "", text)


def inserted(parser: Statement, table: exp.Expr, values: exp.Expr) -> exp.Expr:
    if not (isinstance(table, exp.Literal) and table.is_string):
        parser.fail("insert is read into a table named by symbol")
    target = exp.to_table(table.this)
    if is_query(values) and values.meta.get("xcol"):
        # `t insert `a`b xcol q: INSERT INTO t (a, b) SELECT ...
        names, items = values.meta["xcol"]
        values = values.copy()
        values.set("expressions", items)
        schema = exp.Schema(this=target, expressions=[exp.to_identifier(n) for n in names])
        return exp.Insert(this=schema, expression=values)
    if is_query(values):
        return exp.Insert(this=target, expression=values)
    if isinstance(values, TableLiteral):
        cols = [r.args["items"] if isinstance(r, Rows) else [r] for r in values.args["values"]]
        rows = [exp.Tuple(expressions=list(r)) for r in zip(*cols, strict=True)]
        schema = exp.Schema(
            this=target, expressions=[exp.to_identifier(c) for c in values.args["columns"]]
        )
        return exp.Insert(this=schema, expression=exp.Values(expressions=rows))
    if isinstance(values, Enlisted) and isinstance(values.this, Rows):
        # `t insert enlist 1 2: one column, a row per value.
        rows = [exp.Tuple(expressions=[v]) for v in values.this.args["items"]]
        return exp.Insert(this=target, expression=exp.Values(expressions=rows))
    if not isinstance(values, Rows):
        parser.fail("insert takes a row or a list of columns")
    items = values.args["items"]
    if all(isinstance(i, Rows) for i in items):
        cols = [i.args["items"] for i in items]
        rows = [exp.Tuple(expressions=list(r)) for r in zip(*cols, strict=True)]
    else:
        rows = [exp.Tuple(expressions=items)]
    return exp.Insert(this=target, expression=exp.Values(expressions=rows))


HELPERS = (
    TextOrder,
    PerRow,
    EqEach,
    CountRows,
    NoNulls,
    NullsAsNull,
    NonNull,
    Renamed,
    TypedNull,
    QMin,
    Joins,
    QCall,
    Enlisted,
    Chars,
    Rows,
    Index,
    Distinct,
    Where_,
    Exec,
    Keyed,
    Joined,
    TableLiteral,
    TypedAs,
    Sorted,
)


def no_from(parser: Statement, table: TableLiteral) -> exp.Expr:
    """([] x:enlist v), the generator's SELECT v AS x with no FROM."""
    values = table.args["values"]
    if not all(isinstance(v, Enlisted) for v in values):
        parser.fail("a table literal is only read as a SELECT without FROM")
    names = table.args["columns"]
    return exp.Select(
        expressions=[exp.alias_(v.this, n) for n, v in zip(names, values, strict=True)]
    )


NULL_ORDER = re.compile(r"\{x (iasc|idesc) null x`([A-Za-z][A-Za-z0-9_]*)\}\Z")
# The generator's sort on an unnamed column, by its position k.
POSITION_SORT = re.compile(r"\{\(\(cols x\) (\d+)\) (xasc|xdesc) x\}\Z")
POSITION_KEYS = re.compile(
    r"\{x (iasc|idesc) ("
    + "|".join(re.escape(t) for t in (TEXT_KEY, TEXT_NULL, "null"))
    + r") x \(cols x\) (\d+)\}\Z"
)

STRING_AGG_TEXT = re.compile(
    r'\{\$\[all null x;`;`\$\("((?:[^"\\]|\\.)*)"\) sv string x where not null x\]\}\Z'
)

BOOLEAN = (
    exp.NullSafeEQ,
    exp.NullSafeNEQ,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Not,
    exp.Is,
    exp.In,
    exp.Between,
    exp.Like,
    exp.ILike,
    exp.And,
    exp.Or,
    exp.Boolean,
)


def boolean(node: exp.Expr) -> bool:
    """Whether a q operand is a condition, so | and & between two of them
    are OR and AND."""
    while isinstance(node, exp.Paren):
        node = node.this
    return isinstance(node, BOOLEAN)


def bare(node: exp.Expr) -> exp.Expr:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def least_of(cond, then, other):
    """?[null x;y;?[null y;x;x&y]], the generator's LEAST(x, y)."""
    if not (isinstance(cond, exp.Is) and isinstance(other, exp.Case) and other.this is None):
        return None
    x, y = cond.this, then
    ifs = other.args["ifs"]
    if len(ifs) != 1:
        return None
    inner = ifs[0]
    rest = bare(other.args.get("default")) if other.args.get("default") else None
    if not (
        isinstance(inner.this, exp.Is)
        and inner.this.this == y
        and inner.args["true"] == x
        and isinstance(rest, QMin)
        and rest.this == x
        and rest.expression == y
    ):
        return None
    first = bare(x)
    items = [*first.expressions] if isinstance(first, exp.Least) else []
    head = first.this if isinstance(first, exp.Least) else x
    return exp.Least(this=head, expressions=[*items, y], ignore_nulls=True)


def text_part(node: exp.Expr) -> bool:
    node = bare(node)
    return isinstance(node, (Chars, Joins)) or (
        isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.VARCHAR
    )


def concat(parser, joins) -> exp.Expr:
    parts = []
    for part in joins.expressions:
        part = bare(part)
        if isinstance(part, Joins):
            parts += concat(parser, part).expressions
            continue
        if isinstance(part, exp.Concat):
            parts += part.expressions
            continue
        if isinstance(part, exp.Cast) and part.to.this == exp.DataType.Type.VARCHAR:
            part = part.this  # string x
        if isinstance(part, Chars):
            part = exp.Literal.string(part.this)
        parts.append(part)
    return exp.Concat(expressions=parts, safe=True, coalesce=True)


def null_order(parser, target, key: str, first: bool):
    from sqlglot_q.parser import Sorted

    node = target.this if isinstance(target, exp.Paren) else target
    order = (
        node.args.get("order") if isinstance(node, (exp.Select, exp.SetOperation, Sorted)) else None
    )
    if order is None:
        parser.fail("a null sort is read only after a sort on its key")
    for term in order.expressions:
        if isinstance(term.this, exp.Column) and term.this.name == key:
            term.set("nulls_first", first)
            return node
        position = isinstance(term.this, exp.Literal) and not term.this.is_string
        if key.startswith("#") and position and int(term.this.name) == int(key[1:]) + 1:
            term.set("nulls_first", first)
            return node
    parser.fail(f"a null sort on {key} with no sort on {key} ahead of it")


def excluded(parser, names, source) -> exp.Select:
    cols = [exp.column(n) for n in names]
    plain = (
        isinstance(source, exp.Select)
        and len(source.expressions) == 1
        and isinstance(source.expressions[0], exp.Star)
        and not source.expressions[0].args.get("except_")
    )
    if plain:
        source = source.copy()
        source.expressions[0].set("except_", cols)
        return source
    node = exp.Select(expressions=[exp.Star(except_=cols)])
    node.set("from_", exp.From(this=exp.Subquery(this=source)))
    return node


# "J"$"5" and the like, read back as the SQL type each parse is.
PARSE_TYPES = {
    "B": exp.DataType.Type.BOOLEAN,
    "H": exp.DataType.Type.SMALLINT,
    "I": exp.DataType.Type.INT,
    "J": exp.DataType.Type.BIGINT,
    "E": exp.DataType.Type.FLOAT,
    "F": exp.DataType.Type.DOUBLE,
}


def aggregate(kind, arg):
    """An aggregate over x, or over x where c: SQL's FILTER (WHERE c)."""
    if isinstance(arg, Index) and isinstance(arg.expression, Where_):
        return exp.Filter(this=kind(this=arg.this), expression=exp.Where(this=arg.expression.this))
    return kind(this=distinct_of(arg))


def distinct_of(arg):
    """sum distinct x: SUM(DISTINCT x)."""
    return exp.Distinct(expressions=[arg.this]) if isinstance(arg, Distinct) else arg


def operands(node: exp.Expr) -> list[exp.Expr]:
    node = bare(node)
    if isinstance(
        node,
        (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ),
    ):
        return [bare(node.this), bare(node.expression)]
    if isinstance(node, exp.In):
        return [bare(node.this)]
    if isinstance(node, exp.Between):
        return [bare(node.this), bare(node.args["low"]), bare(node.args["high"])]
    if isinstance(node, (exp.Like, exp.ILike)):
        return [bare(node.this)]
    if isinstance(node, (exp.Not, exp.Escape)):
        return operands(node.this)
    return []


def guard_of(left: exp.Expr, right: exp.Expr) -> bool:
    """Whether left is `not null x` for an x the condition right compares."""
    left = bare(left)
    if not (isinstance(left, exp.Not) and isinstance(left.this, exp.Is)):
        return False
    subject = bare(left.this.this)
    return any(subject == o for o in operands(right))


def without_null_keys(node: exp.Expr, keys: list[str]) -> exp.Expr:
    """select from t where not null k, ... over a join's keys: t."""
    node = bare(node)
    if not (isinstance(node, exp.Select) and len(node.expressions) == 1):
        return node
    if not isinstance(node.expressions[0], exp.Star) or node.expressions[0].args.get("except_"):
        return node
    if any(node.args.get(k) for k in ("joins", "group", "order", "limit", "distinct")):
        return node
    where = node.args.get("where")
    if where is None:
        return node
    parts = []
    stack = [where.this]
    while stack:
        part = bare(stack.pop())
        if isinstance(part, exp.And):
            stack += [part.this, part.expression]
        else:
            parts.append(part)
    names = sorted(
        p.this.this.name
        for p in parts
        if isinstance(p, exp.Not)
        and isinstance(p.this, exp.Is)
        and isinstance(p.this.this, exp.Column)
    )
    if len(names) != len(parts) or names != sorted(keys):
        return node
    source = node.args["from_"].this
    if isinstance(source, exp.Subquery):
        return source.this
    column = exp.column(source.name)
    if source.meta.get("drops"):
        column.meta["drops"] = source.meta["drops"]
    return column


def nullable_side(node: exp.Expr) -> bool:
    node = bare(node)
    if isinstance(node, (exp.Literal, exp.Boolean, Chars)):
        return False
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return False
    return not (isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal))


PART_LAMBDAS = {DOW: "DOW", DOY: "DOY", QUARTER: "QUARTER", MILLISECOND: "MILLISECOND"}
PART_LAMBDAS |= {text: part for part, text in EXTRA_PARTS.items() if part != "ISODOW"}
# The local date and time, as SQL's CURRENT_DATE and CURRENT_TIMESTAMP.
NOW = {".z.D": exp.CurrentDate, ".z.P": exp.CurrentTimestamp, ".z.T": exp.CurrentTime}
TRUNC_LAMBDAS = {text: unit for unit, text in TRUNC.items()}


TEXT_MIN_FN = TEXT_MIN.removeprefix("first ")
TEXT_MAX_FN = TEXT_MAX.removeprefix("last ")
TEXT_SORT = re.compile(
    r"\{x (iasc|idesc) ("
    + re.escape(TEXT_KEY)
    + "|"
    + re.escape(TEXT_NULL)
    + r") x`([A-Za-z][A-Za-z0-9_]*)\}\Z"
)
TEXT_AGG_TEXT = re.compile(
    re.escape(TEXT_AGG.format(sep="SEP")).replace(re.escape("(SEP)"), r'\("((?:[^"\\]|\\.)*)"\)')
    + r"\Z"
)


def per_row(node):
    return node.this if isinstance(node, PerRow) else node


def is_empty_text(node) -> bool:
    node = bare(node) if node is not None else None
    return (
        isinstance(node, (Chars,))
        and node.this == ""
        or (isinstance(node, exp.Literal) and node.is_string and node.this == "")
    )


def dpipe_of(cond, then, other):
    """{$[x;y;z]}'[(null a)|null b;"";a,'b], string mode's a || b: NULL when
    a side is."""
    other = bare(other) if other is not None else None
    if not (is_empty_text(then) and isinstance(other, exp.Concat)):
        return None
    guards, stack, casts = [], [bare(cond)], {}
    while stack:
        part = bare(stack.pop())
        if isinstance(part, (exp.Or, exp.Greatest)):
            stack += [part.this, *([part.expression] if part.expression else part.expressions)]
        elif isinstance(part, exp.Is):
            guard = bare(part.this)
            if isinstance(guard, exp.Cast) and guard.to.this in (
                exp.DataType.Type.TEXT,
                exp.DataType.Type.VARCHAR,
            ):
                casts[repr(guard.this)] = guard  # read bare inside the join
                guard = guard.this
            guards.append(guard)
        else:
            return None
    columns = []
    for p in other.expressions:
        # A literal or a constant (UPPER('x')) is not guarded: never NULL.
        constant = not p.find(exp.Column, exp.AggFunc, exp.Select)
        if not isinstance(p, exp.Literal) and not constant and p not in columns:
            columns.append(p)
    if sorted(map(repr, guards)) != sorted(map(repr, columns)):
        return None
    # || does not cast its parts as CONCAT does: the guard's CAST stays.
    parts = [casts.get(repr(p), p) for p in other.expressions]
    node = parts[0]
    for p in parts[1:]:
        node = exp.DPipe(this=node, expression=p, safe=True)
    return node


def scalar_aggregates(parser, where: exp.Expr, source) -> exp.Expr:
    """where x>avg x: in q the aggregate is over the rows the constraints
    before it left; SQL's WHERE has no aggregates, so it is a subquery over
    the same rows."""
    parts, stack = [], [where]
    while stack:
        part = stack.pop()
        if isinstance(part, exp.And):
            stack += [part.expression, part.this]
        else:
            parts.append(part)
    done = []
    for part in parts:
        aggs = [a for a in part.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is None]
        if aggs:
            table, _ = source_table(parser, source)
            for agg in aggs:
                sub = exp.Select(expressions=[agg.copy()])
                sub.set("from_", exp.From(this=table.copy()))
                if done:
                    sub.set("where", exp.Where(this=conjunction([d.copy() for d in done])))
                agg.replace(exp.Subquery(this=sub))
        done.append(part)
    return conjunction(done)
