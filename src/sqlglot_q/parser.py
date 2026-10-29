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

from sqlglot_q.canonical import canonical
from sqlglot_q.generator import HALF_EVEN, NAME, RESERVED, TYPES
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
    "xkey",
    "cross",
    "xcol",
    "insert",
    "xbar",
}
MONADS = (
    {"enlist", "not", "null", "neg", "count", "distinct", "string", "first", "where"}
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
  | (?P<name>[A-Za-z][A-Za-z0-9_.]*)
  | (?P<op>~\\:|<>|<=|>=|[=<>+\-*%&|^#~,!@$?_])
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
            tokens.append(Token("lambda", text[i:j], spaced=spaced))
            i = j
            continue
        word = m.group()
        prev = tokens[-1] if tokens else None
        noun_before = prev is not None and (prev.kind in NOUN_END or prev.text in (")", "]"))
        if kind == "op" and word == "-" and not noun_before and m.end() < len(text):
            # -1 after an operator or at the start is a negative literal;
            # a-1 is subtraction.
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

    def __init__(self, **opts):
        self.opts = opts

    def parse(self, text: str) -> list[exp.Expr]:
        return [Statement(tokenize(s), s).statement() for s in split_statements(text)]


class Statement:
    def __init__(self, tokens: list[Token], text: str):
        self.t = tokens
        self.i = 0
        self.text = text

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
        node = canonical(node.transform(literal))
        leftover = node.find(*HELPERS) if not isinstance(node, HELPERS) else node
        if leftover is not None:
            self.fail(f"{type(leftover).__name__.lower()} has no SQL form here")
        return node

    def create(self, name: str) -> exp.Expr:
        self.i += 2
        self.expect("(")
        self.expect("[")
        self.expect("]")
        cols = []
        while not self.is_(")"):
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
        self.expect(")")
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
        if isinstance(left, exp.Column) and self.starts_term():
            # Juxtaposition: x where not null x indexes x.
            return Index(this=left, expression=self.expr(stop))
        self.fail(f"cannot apply {tok.text!r} here")

    def starts_term(self) -> bool:
        tok = self.peek()
        return tok is not None and (tok.kind in NOUN_END or tok.text in ("(",))

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
            return Chars(this=tok.value)
        if tok.kind == "sym":
            if self.is_("$"):
                self.next()
                return self.cast(tok.value[0], stop)
            if len(tok.value) == 1:
                return exp.Literal.string(tok.value[0])
            return Rows(items=[exp.Literal.string(v) for v in tok.value])
        if tok.kind == "lambda":
            return self.lambda_call(tok.text)
        if tok.text == "(":
            return self.parenthesised()
        if tok.text == "?" and self.is_("["):
            return self.conditional()
        if tok.kind == "name":
            return self.word(tok.text, stop)
        self.fail(f"unexpected {tok.text!r}")

    def word(self, word: str, stop: set[str]) -> exp.Expr:
        if word in QUERY_WORDS:
            return self.query(word, stop)
        if word == "ej" and self.is_("["):
            return self.equijoin()
        if word == "aj" and self.is_("["):
            return self.asof_join()
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

    def monad(self, word: str, arg: exp.Expr) -> exp.Expr:
        if word == "not":
            return exp.Not(this=arg)
        if word == "null":
            return exp.Is(this=arg, expression=exp.Null())
        if word == "neg":
            return exp.Neg(this=arg)
        if word == "string":
            return exp.Cast(this=arg, to=exp.DataType.build("VARCHAR"))
        if word in AGGREGATES:
            return AGGREGATES[word](this=arg)
        if word in MONADIC:
            return MONADIC[word](this=arg)
        if word == "count":
            if isinstance(arg, exp.Column) and arg.name == "i":
                return exp.Count(this=exp.Star())
            if isinstance(arg, Distinct) and non_null(arg.this):
                return exp.Count(this=exp.Distinct(expressions=[arg.this.this]))
            if non_null(arg):
                return exp.Count(this=arg.this)
            self.fail("count is only read as COUNT(*), COUNT(x) and COUNT(DISTINCT x)")
        if word == "enlist":
            # enlist "a" is the one-character string; ([] x:enlist v) is a
            # one-row table.
            return arg if isinstance(arg, Chars) else Enlisted(this=arg)
        if word == "distinct":
            if is_query(arg):
                arg.set("distinct", exp.Distinct())
                return arg
            return Distinct(this=arg)
        if word == "where":
            return Where_(this=arg)
        if word == "first" and isinstance(arg, Exec):
            return exp.Subquery(this=arg.this)
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
        if type_name in PART_CASTS:
            return exp.Extract(this=exp.var(PART_CASTS[type_name]), expression=arg)
        kind = CASTS.get(type_name)
        if kind is None:
            self.fail(f"no SQL type for `{type_name}")
        if type_name == "symbol":
            kind = exp.DataType.Type.VARCHAR
        return exp.Cast(this=arg, to=exp.DataType(this=kind))

    def lambda_call(self, text: str) -> exp.Expr:
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
        items = [self.expr(set())]
        while self.is_(";"):
            self.next()
            items.append(self.expr(set()))
        self.expect(")")
        if len(items) > 1:
            return Rows(items=items)
        inner = items[0]
        if is_query(inner) or isinstance(inner, (exp.Subquery, Sorted)):
            return inner
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

    def conditional(self) -> exp.Expr:
        self.expect("[")
        cond = self.expr(set())
        self.expect(";")
        then = self.expr(set())
        self.expect(";")
        other = self.expr(set())
        self.expect("]")
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
        if op in ("=", "<>") and any(chars(n) for n in (left, right)):
            # = on a string compares it character by character; SQL's = on
            # strings is q's ~\: (match each).
            self.fail("= on a string compares characters: SQL equality is ~\\:")
        if op in GLYPHS:
            return GLYPHS[op](this=left, expression=right)
        if op == "$" and isinstance(left, exp.Paren) and isinstance(left.this, Index):
            fn = left.this.this
            if isinstance(fn, exp.Column) and fn.name == "type":
                return TypedAs(this=right)
        if op == "xbar":
            if isinstance(left, exp.Interval):
                return exp.DateBin(this=left, expression=right)
            # n xbar x is x rounded down to a multiple of n.
            return exp.Mul(
                this=exp.Floor(this=exp.Div(this=right, expression=left)), expression=left
            )
        if op == "mod":
            return exp.Mod(this=left, expression=right)
        if op == "^":
            # x^y is y with its nulls filled from x: COALESCE(y, x).
            items = right.expressions if isinstance(right, exp.Coalesce) else []
            first = right.this if isinstance(right, exp.Coalesce) else right
            return exp.Coalesce(this=first, expressions=[*items, left])
        if op == "~\\:":
            return exp.EQ(this=left, expression=literal(right))
        if op == "in":
            if isinstance(right, exp.Subquery):
                return exp.In(this=left, query=right)
            if isinstance(right, Exec):
                return exp.In(this=left, query=exp.Subquery(this=right.this))
            if isinstance(right, Enlisted):
                right = Rows(items=[right.this])
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
                return exp.ILike(this=left.this.this, expression=exp.Literal.string(pattern))
            return exp.Like(this=left, expression=exp.Literal.string(pattern))
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
        if op == "lj" and isinstance(right, Keyed):
            return Joined(this=left, expression=right.this, keys=right.args["keys"], side="LEFT")
        if op == "cross":
            return Joined(this=left, expression=right, keys=[], side="CROSS")
        if op == "xcol":
            return renamed(self, left, right)
        if op == "," and is_query(left) and is_query(right):
            return exp.Union(this=left, expression=right, distinct=False)
        if op == "insert":
            return inserted(self, left, right)
        self.fail(f"{op!r} has no SQL form here")

    # Queries ----------------------------------------------------------------

    def query(self, word: str, stop: set[str]) -> exp.Expr:
        if word == "update":
            return self.update(stop)
        if word == "delete":
            return self.delete(stop)
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
        node = select(self, cols, keys, source, where, distinct)
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
        sets = []
        for item in self.phrase({"from"}):
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
            name = self.next()
            self.expect("from")
            ns = self.next()
            if ns.kind != "sym" or ns.value != ["."]:
                self.fail("delete t from `. is the only delete of a name read")
            target = table(self, name.text)
            # sqlglot moved DROP's table from `this` to a `tables` list in 30.x.
            if "tables" in exp.Drop.arg_types:
                return exp.Drop(tables=[target], kind="TABLE")
            return exp.Drop(this=target, kind="TABLE")
        self.next()
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


class Chars(exp.Expression):
    """A q string literal, kept apart from symbols until its use is known."""

    arg_types = {"this": True}


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
    return isinstance(node, (exp.Select, exp.Union))


def conjunction(items: list[exp.Expr]) -> exp.Expr:
    node = items[0]
    for item in items[1:]:
        node = exp.And(this=node, expression=item)
    return node


def sql_pattern(q: str) -> str:
    out, i = [], 0
    while i < len(q):
        ch = q[i]
        if ch == "[" and i + 2 < len(q) and q[i + 2] == "]":
            out.append(q[i + 1])
            i += 3
            continue
        out.append({"*": "%", "?": "_"}.get(ch, ch))
        i += 1
    return "".join(out)


def source_table(parser: Statement, source: exp.Expr) -> tuple[exp.Expr, list[exp.Join]]:
    """The FROM operand and its joins."""
    if isinstance(source, Joined):
        left, joins = source_table(parser, source.this)
        right = source.expression
        right = exp.Subquery(this=right) if is_query(right) else as_table(parser, right)
        keys = source.args["keys"]
        side = source.args.get("side")
        if side == "CROSS":
            join = exp.Join(this=right, kind="CROSS")
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
    if isinstance(node, exp.Column):
        return table(parser, node.name)
    if isinstance(node, exp.Paren) and isinstance(node.this, exp.Column):
        return table(parser, node.this.name)
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
    return node


def unalias(node: exp.Expr) -> exp.Expr:
    return node.this if isinstance(node, exp.Alias) else node


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
    terms = [exp.Ordered(this=exp.column(n), desc=desc) for n in names]
    if isinstance(target, exp.Paren):
        target = target.this
    if isinstance(target, (exp.Column, Sorted)):
        # Sorting a table, not a query: kept until the select reads it.
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
    if not is_query(target):
        parser.fail("sublist is read on a query")
    if isinstance(count, Rows) and len(count.args["items"]) == 2:
        offset, n = count.args["items"]
        return with_modifier(target, limit=n, offset=offset)
    return with_modifier(target, limit=count)


def renamed(parser: Statement, names: exp.Expr, target: exp.Expr) -> exp.Expr:
    if not isinstance(target, exp.Select):
        parser.fail("xcol is read on a select")
    new = (
        [names.this]
        if isinstance(names, exp.Literal)
        else [n.this for n in names.args["items"]]
        if isinstance(names, Rows)
        else parser.fail("xcol takes a symbol list")
    )
    items = target.expressions
    if len(new) > len(items):
        parser.fail("xcol names more columns than the select has")
    for i, name in enumerate(new):
        target.expressions[i] = exp.alias_(unalias(items[i]), name, quoted=True)
    return target


def inserted(parser: Statement, table: exp.Expr, values: exp.Expr) -> exp.Expr:
    if not (isinstance(table, exp.Literal) and table.is_string):
        parser.fail("insert is read into a table named by symbol")
    target = exp.to_table(table.this)
    if is_query(values):
        return exp.Insert(this=target, expression=values)
    if isinstance(values, TableLiteral):
        cols = [r.args["items"] if isinstance(r, Rows) else [r] for r in values.args["values"]]
        rows = [exp.Tuple(expressions=list(r)) for r in zip(*cols, strict=True)]
        schema = exp.Schema(
            this=target, expressions=[exp.to_identifier(c) for c in values.args["columns"]]
        )
        return exp.Insert(this=schema, expression=exp.Values(expressions=rows))
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
