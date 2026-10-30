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
    exp.Sin: "sin",
    exp.Cos: "cos",
    exp.Tan: "tan",
    exp.Asin: "asin",
    exp.Acos: "acos",
    exp.Atan: "atan",
}
# Functions sqlglot does not know, by name: q lambdas (aggregates first
# enlisted, as scalars.py's).
ANONYMOUS = {
    "GCD": ("{{$[null[x]|null y;0N;{$[y=0;abs x;.z.s[y;x mod y]]}[x;y]]}'[x;y]}", False),
    "LCM": (
        "{{$[null[x]|null y;0N;0=x*y;0;abs[x*y] div {$[y=0;abs x;.z.s[y;x mod y]]}[x;y]]}'[x;y]}",
        False,
    ),
    "EVEN": ("{(signum x)*2*ceiling (abs x)%2}", False),
    "ISFINITE": ("{not x in -0w 0w}", False),
    "ROUND_EVEN": ("{v:x*10 xexp y;f:floor v+0.5;(f-(v=f-0.5)&1=f mod 2)%10 xexp y}", False),
    "SIGNBIT": ("{x<0}", False),
    "EPOCH_US": ("{n:(`long$`timestamp$x)-`long$1970.01.01D0;n div 1000}", False),
    "EPOCH_NS": ("{(`long$`timestamp$x)-`long$1970.01.01D0}", False),
    "BIT_COUNT": ("{{$[null x;0N;`long$sum 0b vs x]} each x}", False),
    "PRODUCT": ("{$[count w:x where not null x;prd w;x 0N]}", True),
    # ISO year * 100 + ISO week.
    "YEARWEEK": (
        "{t:3+`week$x;?[null x;0N;(100*`year$t)+1+(t-`date$`month$12*(`year$t)-2000) div 7]}",
        False,
    ),
}

# Spelled as lambdas because q has no word for them; parser.py reads these
# exact texts back.
ROUND = "{signum[x]*floor 0.5+abs x}"  # half away from zero, as SQL
ROUND_TO = "{(signum x)*(floor 0.5+abs[x]*10 xexp y)%10 xexp y}"
# x // y: toward zero for integers (q's div floors), and DuckDB's // of a
# float is its plain division.
INT_DIV = "{$[any 8 9h in abs type each (x;y);x%y;signum[x*y]*div[abs x;abs y]]}"
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
# string_literals="string": text is a list of char vectors, which null tests
# character by character and q sorts element by element. q has no null
# string, so a null text is the empty one, and text sorts as symbols do.
TEXT_NULL = "{$[0h=type x;0=count each x;null x]}"
TEXT_KEY = "{$[0h=type x;`$x;x]}"
# (v, not y: a lambda naming y takes two arguments)
TEXT_MIN = f"first {{v iasc {TEXT_KEY} v:x where not {TEXT_NULL} x}}"
TEXT_MAX = f"last {{x iasc {TEXT_KEY} x}}"
TEXT_PICK = "{$[x;y;z]}'"  # CASE over text: ?[...] cannot pick strings
TEXT_LENGTH = "{?[0=count each x;0N;count each x]}"
TEXT_FILL = "{$[0=count x;y;x]}'"  # COALESCE over text
# Text as a boolean, as SQL reads it.
TEXT_BOOL = '{any (lower x)~/:("true";"t";"1";"yes";"y";"on")}'
# TRY_CAST of a column to a number: q's parse ("J"$) of the text, null where
# it is no number. L is the type's letter.
TRY_PARSE = '{$[11h=abs t:type x;"L"$string x;0h=t;"L"$x;"L"$string x]}'
# TIME_BUCKET: x xbar y, but a date (of a width dividing a day) as it is.
BUCKET = "{$[14h=abs type y;y;x xbar y]}"
BUCKET_FROM = "{[w;x;o] o+w xbar (`timestamp$x)-o}"
# String mode, where a column's type is not known: strings (and a string)
# as symbols, which compare in text order; anything else as it is. And
# COALESCE of two columns: the text fill for strings, ^ for the rest.
TEXT_CMP = "{$[(type x) in 0 10h;`$x;x]}"
ANY_FILL = "{$[0h=type x;{$[0=count x;y;x]}'[x;y];y^x]}"
# A window's one value per partition, over its rows: update spreads an atom,
# but one string is a list of characters.
TEXT_SPREAD = "{$[10h=type y;(count x)#enlist y;y]}"
COUNT_ROWS = (
    "{n:{$[0h=type x;0=count each x;null x]};count distinct (flip x) where not max n each x}"
)
# REGEXP_REPLACE of plain text, its first match only (no 'g' flag): q's ss
# finds it.
REPLACE_FIRST = (
    "{`${$[count w:x ss y;((first w)#x),z,(count[y]+first w)_x;x]}[;y;z] each string x}",
    "{{$[count w:x ss y;((first w)#x),z,(count[y]+first w)_x;x]}[;y;z] each x}",
)
# The null of x's type, on each of its rows for a column of strings.
NULL_LIKE = '{$[0h=type x;(count x)#enlist "";x 0N]}'
# A column's null, of its type: "" for a column of strings, whose 0# is ().
NULL_OF = '{$[0h=type x;"";first 0#x]}'
# NULLIF(a, b) of two columns in string mode: a, its null (an index past
# the end, "" for strings) where y holds.
NULLIF_ANY = "{@[x;w;:;(count w:where y)#enlist x count x]}"
# GREATEST and LEAST in string mode: compared as symbols, a null skipped.
TEXT_GREATEST = (
    "{n:{$[0h=type x;0=count each x;null x]};c:{$[(type x) in 0 10h;`$x;x]};"
    "s:{$[10h=type x;(count y)#enlist x;x]};x:s[x;y];y:s[y;x];"
    "?[n x;y;?[n y;x;?[(c x)>=c y;x;y]]]}"
)
TEXT_LEAST = TEXT_GREATEST.replace(">=", "<=")
PARSE = {"boolean": "B", "short": "H", "int": "I", "long": "J", "real": "E", "float": "F"}
# TRY_CAST of text to a date, a timestamp or a time of day: q's parse of it.
TEMPORAL_PARSE = {"date": "D", "timestamp": "P", "time": "T"}
TEXT_AGG = '{{$[all {null} x;"";({sep}) sv x where not {null} x]}}'.replace(
    "{null}", TEXT_NULL.replace("{", "{{").replace("}", "}}")
)
# Calendar parts and truncations q has no single word for. q's day 0,
# 2000.01.01, is a Saturday; SQL's DOW counts from Sunday.
DOW = "{(6+`int$`date$x) mod 7}"
DOY = "{1+(`date$x)-`date$`month$12*(`year$x)-2000}"
QUARTER = "{1+((`mm$x)-1) div 3}"
MILLISECOND = "{((`ss$x)*1000)+((`long$x) mod 1000000000) div 1000000}"
# More of EXTRACT: ISO weekday (Monday 1) and year (its Thursday's), the
# microseconds with the seconds, decade, century and millennium (1-based).
ISODOW = "{1+(5+`int$`date$x) mod 7}"
EXTRA_PARTS = {
    "ISODOW": ISODOW,
    "ISOYEAR": "{`year$3+`week$x}",
    "MICROSECOND": "{((`ss$x)*1000000)+((`long$x) mod 1000000000) div 1000}",
    "DECADE": "{(`year$x) div 10}",
    "CENTURY": "{1+((`year$x)-1) div 100}",
    "MILLENNIUM": "{1+((`year$x)-1) div 1000}",
}
TRUNC = {
    "MONTH": "{`timestamp$`date$`month$x}",
    # 3 xbar 0Nm is 2000.01m: a null is kept out of the bucket.
    "QUARTER": "{?[null x;0Np;`timestamp$`date$3 xbar `month$x]}",
    "YEAR": "{`timestamp$`date$`month$12*(`year$x)-2000}",
    "WEEK": "{`timestamp$`week$x}",  # the Monday, as SQL
    "DAY": "{`timestamp$`date$x}",  # 1D xbar of a date is not a day
    "DECADE": "{`timestamp$`date$`month$12*(10*(`year$x) div 10)-2000}",
    "CENTURY": "{`timestamp$`date$`month$12*(100*(`year$x) div 100)-2000}",
    "MILLENNIUM": "{`timestamp$`date$`month$12*(1000*(`year$x) div 1000)-2000}",
}
# Text functions, each over the rows: symbols go through their strings.
REPLACE = ("{`$ssr[;y;z] each string x}", "{ssr[;y;z] each x}")
SUBSTRING = ("{`$sublist[(y-1;z)] each string x}", "{sublist[(y-1;z)] each x}")
SUBSTRING_FROM = ("{`$(y-1)_' string x}", "{(y-1)_' x}")
REVERSE = ("{`$reverse each string x}", "{reverse each x}")
RUNNING_SUM = "{?[0=sums not null x;x 0N;sums x]}"
# Running and sliding MAX and MIN through ranks (iasc iasc x), which order
# symbols too, where maxs and mmax raise 'type. A null ranks lowest, and for
# MIN is set past every rank: an index past the end is a null again.
RUNNING_MAX = "{x (iasc x) maxs iasc iasc x}"
RUNNING_MIN = f"{{x (iasc x) mins @[iasc iasc x;where {TEXT_NULL} x;:;0W]}}"
# Sliding frames with nulls: msum counts a null as 0 and mmin takes it as the
# least; SQL skips nulls, and a frame of only nulls is NULL. (mavg skips them.)
MOVING_NULLS = {
    "msum": "{?[0=x msum not null y;y 0N;x msum y]}",
    "mmax": "{y (iasc y) x mmax iasc iasc y}",
    "mmin": f"{{y (iasc y) x mmin @[iasc iasc y;where {TEXT_NULL} y;:;0W]}}",
}
# SIGN(NULL) is NULL; q's signum of a null is -1, a null being below zero.
SIGN = "{?[null x;0Ni;signum x]}"
# LAG/LEAD with a default: the default only past the partition's edge, in the
# column's type (q refuses an amend of another type).
# (A column of strings has no type to cast the default to: it goes in as is.)
LAG_DEFAULT = (
    "{v:y xprev x;w:til y&count x;@[v;w;:;$[0h=type v;(count w)#enlist z;(abs type v)$z]]}"
)
LEAD_DEFAULT = (
    "{v:neg[y] xprev x;w:(count x)-1+til y&count x;"
    "@[v;w;:;$[0h=type v;(count w)#enlist z;(abs type v)$z]]}"
)
# Window functions with no q word, applied to a stage's rows (windows.py).
# NTILE as SQL defines it: the first n mod k buckets get the extra rows.
NTILE = "{n:count y;s:n div x;r:n mod x;j:til n;1+?[j<r*s+1;j div s+1;r+(j-r*s+1) div s]}"
PERCENT_RANK = "{$[1=count x;(count x)#0f;(x?x)%-1+count x]}"
CUME_DIST = "{(1+til count x)%count x}"
# LEFT(s, n) and RIGHT(s, n): sublist, not #, which repeats a short string.
# LEFT(s, -n) is all but the last n characters, RIGHT(s, -n) all but the first.
LEFT = (
    "{`${$[x<0;x _ y;x sublist y]}[x] each string y}",
    "{{$[x<0;x _ y;x sublist y]}[x] each y}",
)
RIGHT = (
    "{`${$[x<0;(neg x)_ y;(neg x) sublist y]}[x] each string y}",
    "{{$[x<0;(neg x)_ y;(neg x) sublist y]}[x] each y}",
)

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
# CAST(x AS VARCHAR): text as SQL writes it, a date 2024-01-31 (not
# 2024.01.31), a boolean true (not 1), a timestamp 2024-01-31 09:30:01.25
# (its fraction's trailing zeros dropped), a whole float 9.0 (not 9);
# anything else as q's string.
TEXT_TS = (
    '{$[null x;"";(ssr[10#s;".";"-"])," ",'
    '{$[all "0"=-9#x;-10_x;(neg sum mins reverse "0"=x)_x]} 11_s:string x]}'
)
TEXT_FLOAT = '{$[null x;"";any (s:string x) in ".en";s;s,".0"]}'
TEXT_OF = (
    '{$[-14h=t:type x;ssr[string x;".";"-"];14h=t;ssr[;".";"-"] each string x;'
    '-1h=t;$[x;"true";"false"];1h=t;("false";"true") x;'
    f"-12h=t;{TEXT_TS} x;12h=t;{TEXT_TS} each x;-9h=t;{TEXT_FLOAT} x;9h=t;{TEXT_FLOAT} each x;"
    "string x]}"
)
# A join's side without the columns the query reads from another side (and
# that it may have too): q's joined table keeps the right side's.
OTHERS = "{(cols[x] except (),y)#x}"
# LEFT JOIN: ej's matches, and the left rows with none (uj gives them the
# right's columns, null).
LEFT_JOIN = "{k:(),x;ej[k;y;z] uj y where not (k#y) in k#z}"
# FULL JOIN: the matches of the rows with no null key, then each side's rows
# with no match (a null key matches nothing), the other side's columns null.
FULL_JOIN = (
    "{k:(),x;n:{max value {$[0h=type x;0=count each x;null x]} each flip x#y}[k];"
    "a:y where not n y;b:z where not n z;"
    "(ej[k;a;b] uj y where not (k#y) in k#b) uj z where not (k#z) in k#a}"
)
HALF_EVEN = "{f:floor x+0.5;f-(x=f-0.5)&1=f mod 2}"
# x IN (SELECT ...): q's in, but a long against floats (two number types)
# compared as floats, where in raises 'type.
IN_ANY = (
    "{$[(t:abs type x)=u:abs type y;x in y;(t in 5 6 7 8 9h)&u in 5 6 7 8 9h;"
    "(`float$x) in `float$y;x in y]}"
)
# (a, b) IN (SELECT ...): the rows as a table, in the subquery's rows that
# have no null; a row with a null part matches nothing, as in SQL.
TUPLE_IN = (
    "{n:{$[0h=type x;0=count each x;null x]};f:{$[(abs type x) in 5 6 7 8 9h;`float$x;x]};"
    'k:`$"x",\'string til count x;r:k xcol y;r:r where not max value n each flip r;'
    "(not max n each x)&(flip k!f each x) in flip f each flip r}"
)
# x LIKE y with y a pattern per row: each made q's (% *, _ ?, q's own
# wildcards bracketed) and matched row by row.
LIKE_EACH = '{x like\' {raze {$[x="%";"*";x="_";"?";x in "*?[]";"[",x,"]";x]} each x} each y}'
# A ROWS frame from a to b rows off the current one (0N: unbounded), each
# row's slice of its partition aggregated without its nulls; NULL over none.
# Windows: LAST_VALUE to the current row is the row's own (its last peer's
# under RANGE); NTH_VALUE the partition's k-th, NULL before the frame has
# it (and over the whole partition, the k-th on every row).
LAST_ROW = "{x}"
NTH = "{{$[x;y;z]}[;x y-1;x count x] each (til count x)>=y-1}"
NTH_WHOLE = "{x y-1}"
# An aggregate's values in an order (STRING_AGG(s, ',' ORDER BY o)): x by
# the key y, its nulls first or last as asked.
_N = "{$[0h=type x;0=count each x;null x]}"
ORDERED = {
    (False, False): f"{{x i iasc {_N} y i:iasc y}}",  # ASC NULLS LAST
    (False, True): "{x iasc y}",  # ASC NULLS FIRST
    (True, False): "{x idesc y}",  # DESC NULLS LAST
    (True, True): f"{{x i idesc {_N} y i:idesc y}}",  # DESC NULLS FIRST
}
# BOOL_AND and BOOL_OR over where the condition is true (x) and where it is
# known (y): SQL skips the unknown rows. NULL over none is false in q, which
# has no null boolean.
BOOL_AND = "{(all x where y)&any y}"
# INTERSECT ALL and EXCEPT ALL: each row numbered among its copies, so the
# n-th copy in x is kept when y has an n-th too (or, for EXCEPT, has not).
MULTISET = (
    "{o:{g:group x;@[count[x]#0;raze value g;:;raze til each count each value g]};"
    "a:x,'([]q_n:o x);(cols x)#a where WHERE a in y,'([]q_n:o y)}"
)
INTERSECT_ALL = MULTISET.replace("WHERE ", "")
EXCEPT_ALL = MULTISET.replace("WHERE ", "not ")
BOOL_OR = "{any x where y}"
FRAME_SLICES = (
    "{[a;b;y] n:count y;i:til n;l:$[null a;n#0;0|i+a];h:$[null b;n#n-1;(n-1)&i+b];"
    "AGG each {x y+til 0|1+z-y}[y]'[l;h]}"
)
# A RANGE frame of time, w back to the current row: the rows whose order
# key t is within w before this row's (its peers too), found by bin.
RANGE_SLICES = "{[w;t;y] s:t binr t-w;h:t bin t;AGG each {x y+til 0|1+z-y}[y]'[s;h]}"
FRAMES = {
    name: FRAME_SLICES.replace("AGG", agg)
    for name, agg in (
        ("sum", "{$[count w:x where not null x;sum w;x 0N]}"),
        ("avg", "avg"),
        ("max", f"{{$[count w:x where not {TEXT_NULL} x;last asc w;x 0N]}}"),
        ("min", f"{{$[count w:x where not {TEXT_NULL} x;first asc w;x 0N]}}"),
        ("count", "{sum not null x}"),
        ("count_star", "count"),
    )
}
RANGE_FRAMES = {
    name: RANGE_SLICES.replace("AGG", agg)
    for name, agg in (
        ("sum", "{$[count w:x where not null x;sum w;x 0N]}"),
        ("avg", "avg"),
        ("max", f"{{$[count w:x where not {TEXT_NULL} x;last asc w;x 0N]}}"),
        ("min", f"{{$[count w:x where not {TEXT_NULL} x;first asc w;x 0N]}}"),
        ("count", "{sum not null x}"),
        ("count_star", "count"),
    )
}

TEXT = {
    exp.DataType.Type.VARCHAR,
    exp.DataType.Type.CHAR,
    exp.DataType.Type.TEXT,
    exp.DataType.Type.NVARCHAR,
    exp.DataType.Type.NCHAR,
}


UNLIFTED = (
    exp.Literal,
    exp.Paren,
    exp.Alias,
    exp.Cast,
    exp.Interval,
    exp.Null,
    exp.Boolean,
    exp.Tuple,
    exp.Array,
)


def computed(node: exp.Expr) -> bool:
    """Whether node holds q text a stage computed (a window's QText): a value
    per row, though it has no column under it."""
    return any(type(n).__name__ == "QText" for n in node.walk())


def constant(node: exp.Expr) -> bool:
    """A value that is the same on every row: no column, aggregate or query."""
    return (
        not isinstance(node, exp.Literal)
        and not node.find(exp.Column, exp.AggFunc, exp.Star, exp.Subquery, exp.Select, exp.Window)
        and not computed(node)
    )


def lifted(node: exp.Expr) -> bool:
    """A text expression over constants only: UPPER('ab') || 'c'. Its string
    literals are written as lists of one (literal_sql), and the value is the
    first of the result."""
    if isinstance(node, UNLIFTED) or not isinstance(node, (exp.Func, exp.Binary, exp.Unary)):
        return False
    if isinstance(node, (exp.Predicate, exp.Connector)) and not isinstance(node, exp.Like):
        return False  # a comparison of atoms is an atom already
    if node.find(exp.Column, exp.AggFunc, exp.Star, exp.Subquery, exp.Select, exp.Window):
        return False
    if computed(node):
        return False
    return any(
        lit.is_string and not isinstance(lit.parent, (exp.Interval, exp.Cast))
        for lit in node.find_all(exp.Literal)
    )


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
        node,
        (
            exp.Column,
            exp.Literal,
            exp.Null,
            exp.Boolean,
            exp.Paren,
            exp.Subquery,
            exp.Pi,
            exp.CurrentDate,
            exp.CurrentTimestamp,
            exp.CurrentTime,
        ),
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
        if not skip_nulls:
            return f"{word} {arg}"
        # A string's null is the empty one; null of a string tests its chars.
        null = "null" if self.symbols else TEXT_NULL
        return f"{word} {arg} where not {null} {arg}"

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
        if not getattr(self, "_lifting", False) and lifted(expression):
            # Text functions run over the rows: each string x of a symbol
            # atom walks its characters. A constant one runs over a list of
            # one, its literals enlisted, and first takes the value.
            self._lifting, self._enlisted = True, False
            try:
                text = handler(self, expression)
                enlisted = self._enlisted
            finally:
                self._lifting = False
            if enlisted:
                return f"(first {text})"
            # Its literals were written as q strings, not through
            # literal_sql: it runs on atoms as it is.
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
            text = self.symbol(node.this) if self.symbols else self.chars(node.this)
            if getattr(self, "_lifting", False):
                self._enlisted = True
                return f"(enlist {text})"
            return text
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
        if text != text.strip(" "):
            # `$ trims a symbol's ends: ' x' would be `x, and s = ' x' would
            # find the x rows.
            return self.refuse(
                'a symbol cannot start or end with a space: use string_literals="string"'
            )
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
        months = calendar_months(node)
        if months is not None:
            # d + INTERVAL 1 MONTH: a month is no fixed timespan, so a lambda.
            from sqlglot_q.scalars import ADD_MONTHS

            return f"{ADD_MONTHS}[{self.sql(left)};{months}]"
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
            if isinstance(left, exp.Subquery):
                left, right = right, left
            if isinstance(right, exp.Subquery):
                # One value, a string or not: matched against each row.
                match = f"{self.operand(left)}~\\:{self.operand(right)}"
                return match if isinstance(node, exp.EQ) else f"not {match}"
            if atomic_value(left) or atomic_value(right):
                # A number, a date: no string, q's = (~' would pair it per row).
                return f"{self.operand(left)}{glyph}{self.operand(right)}"
            # Two columns, of text or not: match row by row.
            match = f"{self.operand(left)}~'{self.operand(right)}"
            return match if isinstance(node, exp.EQ) else f"not {match}"
        if not self.symbols and (is_text(left) or is_text(right)):
            # < and > on strings compare character by character; as symbols
            # they compare in the text's order.
            sides = [f"(`${self.operand(x)})" for x in (left, right)]
            return f"{sides[0]}{glyph}{sides[1]}"
        if not self.symbols and isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
            if not (atomic_value(left) or atomic_value(right)):
                # Two values of unknown type: as symbols if they are strings.
                sides = [f"({TEXT_CMP} {self.operand(x)})" for x in (left, right)]
                return f"{sides[0]}{glyph}{sides[1]}"
        return f"{self.operand(left)}{glyph}{self.operand(right)}"

    eq_sql = neq_sql = gt_sql = gte_sql = lt_sql = lte_sql = binary
    add_sql = sub_sql = mul_sql = div_sql = binary

    def connector(self, node: exp.Expr) -> str:
        sides = (node.this, node.expression)
        if all(condition_like(x) for x in sides):
            return self.binary(node)
        # f AND a > 2 with f a column: q's and/or, which are & and |, but
        # say a column is a condition when read back (f&x reads as LEAST).
        word = "and" if isinstance(node, exp.And) else "or"
        return f"{self.operand(sides[0])} {word} {self.operand(sides[1])}"

    and_sql = or_sql = connector

    def nullsafeeq_sql(self, node) -> str:
        # x IS NOT DISTINCT FROM NULL is x IS NULL.
        null, other = null_side(node)
        if null is not None:
            return self.sql(exp.Is(this=other.copy(), expression=exp.Null()))
        # q's = is already null-safe: 0N=0N is true, 0N=1 false.
        return self.sql(exp.EQ(this=node.this.copy(), expression=node.expression.copy()))

    def nullsafeneq_sql(self, node) -> str:
        null, other = null_side(node)
        if null is not None:
            return self.sql(exp.Not(this=exp.Is(this=other.copy(), expression=exp.Null())))
        return self.sql(exp.NEQ(this=node.this.copy(), expression=node.expression.copy()))

    def regexpreplace_sql(self, node: exp.RegexpReplace) -> str:
        pattern, replacement = node.expression, node.args.get("replacement")
        flags = node.args.get("modifiers")
        plain = (
            isinstance(pattern, exp.Literal)
            and pattern.is_string
            and pattern.this
            and not any(c in pattern.this for c in ".*+?()[]{}|^$\\")
        )
        if not plain or not (isinstance(replacement, exp.Literal) and replacement.is_string):
            return self.refuse("REGEXP_REPLACE is translated for a plain-text pattern")
        if "\\" in replacement.this or any(node.args.get(k) for k in ("position", "occurrence")):
            return self.refuse("REGEXP_REPLACE is translated without backreferences")
        if flags is not None and flags.name != "g":
            return self.refuse("REGEXP_REPLACE is translated with no flags or 'g'")
        if not node.args.get("single_replace") or flags is not None:
            # Every match (sqlglot's REGEXP_REPLACE, DuckDB's with 'g').
            return self.sql(
                exp.Replace(
                    this=node.this.copy(), expression=pattern.copy(), replacement=replacement.copy()
                )
            )
        fn = REPLACE_FIRST[0] if self.symbols else REPLACE_FIRST[1]
        args = (self.sql(node.this), self.chars(pattern.this), self.chars(replacement.this))
        return f"{fn}[{';'.join(args)}]"

    def groupconcat_sql(self, node) -> str:
        sep = node.args.get("separator")
        if sep is None:
            sep = exp.Literal.string(",")
        if not (isinstance(sep, exp.Literal) and sep.is_string):
            return self.refuse("STRING_AGG is translated with a literal separator")
        values = self.aggregated(node.this)
        if values.startswith("'"):
            return values
        sep_text = self.chars(sep.this).replace("enlist ", "")
        if not self.symbols:
            fn = TEXT_AGG.format(sep=sep_text)
            # first enlist: first of the joined string would be its first char
            return f"first enlist {fn}[{values}]"
        # Skips nulls as SQL does; all null (or none) is the null symbol.
        fn = STRING_AGG.format(sep=sep_text)
        return f"first {fn}[{values}]"

    def aggregated(self, arg: exp.Expr) -> str:
        """An aggregate's values: x, distinct x, or x in an ORDER BY's order."""
        if isinstance(arg, exp.Distinct):
            if len(arg.expressions) != 1:
                return self.refuse("DISTINCT of several values has no q translation here")
            return f"distinct {self.operand(arg.expressions[0])}"
        if isinstance(arg, exp.Order):
            terms = arg.expressions
            if len(terms) != 1:
                return self.refuse("an aggregate's ORDER BY is translated with one key")
            term = terms[0]
            desc = bool(term.args.get("desc"))
            first = term.args.get("nulls_first")
            first = (not desc) if first is None else bool(first)  # q's own order
            return f"{ORDERED[(desc, first)]}[{self.sql(arg.this)};{self.sql(term.this)}]"
        return self.sql(arg)

    def dayofweek_sql(self, node) -> str:
        return f"{DOW} {self.operand(node.this)}"

    def dayofyear_sql(self, node) -> str:
        return f"{DOY} {self.operand(node.this)}"

    def quarter_sql(self, node) -> str:
        return f"{QUARTER} {self.operand(node.this)}"

    def datediff_sql(self, node: exp.DateDiff) -> str:
        from sqlglot_q import scalars

        call = scalars.find(node)
        if call is not None:
            return scalars.write(self, node, call)
        unit = (node.text("unit") or "DAY").upper()
        if unit not in ("DAY", "DAYS"):
            return self.refuse(f"DATE_DIFF in {unit} has no q translation here")
        # Day boundaries between the two, as SQL counts them.
        end, start = (f"(`date${self.operand(e)})" for e in (node.this, node.expression))
        return f"{end}-{start}"

    def replace_sql(self, node: exp.Replace) -> str:
        args = (node.expression, node.args.get("replacement"))
        if not all(isinstance(a, exp.Literal) and a.is_string for a in args):
            return self.refuse("REPLACE is translated with literal text to find and put")
        fn = REPLACE[0] if self.symbols else REPLACE[1]
        find, put = (self.chars(a.this).replace("enlist ", "") for a in args)
        return f"{fn}[{self.sql(node.this)};({find});({put})]"

    def substring_sql(self, node: exp.Substring) -> str:
        start, length = node.args.get("start"), node.args.get("length")
        if start is None:
            return self.refuse("SUBSTRING without a start has no q translation")
        if length is None:
            fn = SUBSTRING_FROM[0] if self.symbols else SUBSTRING_FROM[1]
            return f"{fn}[{self.sql(node.this)};{self.sql(start)}]"
        fn = SUBSTRING[0] if self.symbols else SUBSTRING[1]
        return f"{fn}[{self.sql(node.this)};{self.sql(start)};{self.sql(length)}]"

    def reverse_sql(self, node: exp.Reverse) -> str:
        fn = REVERSE[0] if self.symbols else REVERSE[1]
        return f"{fn} {self.operand(node.this)}"

    def exists_sql(self, node: exp.Exists) -> str:
        # Uncorrelated: whether the subquery has a row. A subquery reading the
        # outer query's columns is refused by q when it runs ('col).
        from sqlglot_q.correlated import exists

        query = node.this
        if isinstance(query, exp.Subquery):
            query = query.this
        correlated = exists(self, query)
        if correlated is not None:
            return correlated
        return f"0<count {self.operand_query(query)}"

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
            return self.is_null(self.sql(node.this))
        if isinstance(node.expression, exp.Boolean):
            # c IS TRUE: where c is true, never unknown; IS FALSE where false.
            from sqlglot_q.truth import fails, holds

            return holds(self, node.this) if node.expression.this else fails(self, node.this)
        return self.refuse("IS is only translated for IS NULL, IS TRUE and IS FALSE")

    def in_sql(self, node: exp.In) -> str:
        items = node.expressions
        kinds = {
            "." in i.this or "e" in i.this.lower()
            for i in items
            if isinstance(i, exp.Literal) and not i.is_string
        }
        if len(kinds) > 1 and node.args.get("query") is None:
            # IN (1, 2.5): q's in refuses a long column against a float list
            # ('type), where = compares across them.
            return f"any {self.operand(node.this)}=/:{self.vector(items)}"
        query = node.args.get("query")
        subject = "" if isinstance(node.this, exp.Tuple) else self.operand(node.this)
        if query is not None:
            from sqlglot_q.correlated import refuse_correlated

            select = query.this if isinstance(query, exp.Subquery) else query
            refused = refuse_correlated(self, select, "IN (SELECT ...)")
            if refused is not None:
                return refused
            if isinstance(node.this, exp.Tuple):
                parts = node.this.expressions
                if len(select.expressions) != len(parts):
                    return self.refuse("(a, b) IN (SELECT ...) needs as many columns as parts")
                values = ";".join(self.sql(p) for p in parts)
                return f"{TUPLE_IN}[({values});({self.sql(select)})]"
            if len(select.expressions) != 1:
                return self.refuse("IN (SELECT ...) needs exactly one column")
            return f"{IN_ANY}[{self.sql(node.this)};{self.in_values(node)}]"
        return f"{subject} in {self.vector(node.expressions)}"

    def in_values(self, node: exp.In) -> str:
        """The subquery of x IN (SELECT c ...) as q's list of its values."""
        query = node.args["query"]
        select = query.this if isinstance(query, exp.Subquery) else query
        inner = select.copy()
        inner.set("expressions", [exp.alias_(select.expressions[0], "x")])
        # Parenthesised: in a where list, exec ... from would take the
        # constraints after it as part of its from.
        return f"(exec x from ({self.sql(inner)}))"

    def between_sql(self, node: exp.Between) -> str:
        bounds = [node.args["low"], node.args["high"]]
        if not self.symbols:
            # within of strings compares character by character.
            low, high = (f"{TEXT_CMP} {self.operand(b)}" for b in bounds)
            return f"({TEXT_CMP} {self.operand(node.this)}) within ({low};{high})"
        return f"{self.operand(node.this)} within {self.vector(bounds)}"

    def like_sql(self, node: exp.Like) -> str:
        pattern = node.expression
        if isinstance(pattern, exp.Any) and isinstance(pattern.this, exp.Tuple):
            # s LIKE ANY ('a%', 'b%'): any of the likes.
            return self.any_like(node, type(node))
        if not (isinstance(pattern, exp.Literal) and pattern.is_string):
            # A pattern per row: each made q's when the query runs.
            text = f"string {self.operand(pattern)}" if self.symbols else self.sql(pattern)
            return f"{LIKE_EACH}[{self.sql(node.this)};{text}]"
        return f"{self.operand(node.this)} like {self.chars(like_pattern(pattern.this))}"

    def escape_sql(self, node: exp.Escape) -> str:
        test, escape = node.this, node.expression
        while isinstance(test, exp.Paren):
            test = test.this
        if isinstance(test, exp.Not):
            # NOT LIKE ... ESCAPE: DuckDB puts the NOT inside the ESCAPE.
            inner = exp.Escape(this=test.this, expression=escape)
            return f"not {self.sql(inner)}"
        if not (
            isinstance(test, (exp.Like, exp.ILike))
            and isinstance(test.expression, exp.Literal)
            and test.expression.is_string
            and isinstance(escape, exp.Literal)
            and len(escape.this) == 1
        ):
            return self.refuse("LIKE ... ESCAPE is translated with a literal pattern and character")
        sql = test.expression.this
        if isinstance(test, exp.ILike):
            text = like_pattern(sql.lower(), escape.this.lower())
            return f"(lower {self.sql(test.this)}) like {self.chars(text)}"
        return f"{self.operand(test.this)} like {self.chars(like_pattern(sql, escape.this))}"

    def any_like(self, node: exp.Expr, kind) -> str:
        tests = [
            kind(this=node.this.copy(), expression=p.copy())
            for p in node.expression.this.expressions
        ]
        either = tests[0]
        for test in tests[1:]:
            either = exp.Or(this=either, expression=test)
        return self.sql(either)

    def ilike_sql(self, node: exp.ILike) -> str:
        pattern = node.expression
        if isinstance(pattern, exp.Any) and isinstance(pattern.this, exp.Tuple):
            return self.any_like(node, type(node))
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
            if len(arg.expressions) > 1:
                # COUNT(DISTINCT a, b): the distinct rows with no null part.
                items = ";".join(self.sql(e) for e in arg.expressions)
                return f"first enlist {COUNT_ROWS}[({items})]"
            if len(arg.expressions) != 1:
                return self.refuse("COUNT(DISTINCT ...) is only translated for one column")
            col = self.operand(arg.expressions[0])
            return f"count distinct {col} where not {self.is_null(col)}"
        col = self.operand(arg)
        # COUNT(x) counts the rows where x is not null.
        return f"count {col} where not {self.is_null(col)}"

    def aggregate(self, node: exp.Expr) -> str:
        arg = node.this
        if isinstance(arg, exp.Distinct):
            if len(arg.expressions) != 1:
                return self.refuse(f"{node.sql()}: DISTINCT is translated for one column")
            arg = arg.expressions[0]
            if not isinstance(node, (exp.Min, exp.Max)):
                # SUM(DISTINCT x): over x's distinct values, a null among them
                # skipped as before. MIN and MAX are the same either way.
                word = NULL_SAFE.get(type(node), AGGREGATES[type(node)])
                return f"{word} distinct {self.operand(arg)}"
        elif node.args.get("distinct"):
            return self.refuse(f"{node.sql()}: DISTINCT here has no q translation")
        word = NULL_SAFE.get(type(node), AGGREGATES[type(node)])
        if not self.symbols and isinstance(node, (exp.Min, exp.Max)):
            word = TEXT_MIN if isinstance(node, exp.Min) else TEXT_MAX
        return f"{word} {self.sql(arg)}"

    sum_sql = avg_sql = min_sql = max_sql = aggregate

    def monadic(self, node: exp.Expr) -> str:
        if node.args.get("expression") is not None:
            return self.refuse(f"{node.key.upper()} of two arguments has no q translation here")
        return f"{MONADIC[type(node)]} {self.sql(node.this)}"

    sin_sql = cos_sql = tan_sql = asin_sql = acos_sql = atan_sql = monadic

    def concatws_sql(self, node: exp.ConcatWs) -> str:
        # CONCAT_WS(sep, a, b): the non-null parts joined by sep.
        sep, *parts = node.expressions
        if not (isinstance(sep, exp.Literal) and sep.is_string):
            return self.refuse("CONCAT_WS is translated with a literal separator")
        items = ";".join(self.sql(p) for p in parts)
        sep_text = f"({self.chars(sep.this)})"
        if self.symbols:
            body = f"{{`${sep_text} sv string x where not null x}}"
        else:
            body = f"{{{sep_text} sv x where 0<count each x}}"
        if len(parts) == 1:
            return f"{body} each enlist each {self.operand(parts[0])}"
        return f"{body} each flip ({items})"

    def trycast_sql(self, node: exp.TryCast) -> str:
        # q's parse of text ("J"$) is null where SQL's TRY_CAST is NULL.
        kind = node.to.this
        letters = PARSE | TEMPORAL_PARSE
        if is_text(node.this) or kind not in TYPES or TYPES[kind][0] not in letters:
            return self.cast_sql(exp.Cast(this=node.this.copy(), to=node.to.copy()))
        # A column: parsed when it holds text, cast as it is otherwise.
        letter = letters[TYPES[kind][0]]
        return f"{TRY_PARSE.replace('L', letter)} {self.operand(node.this)}"

    def withingroup_sql(self, node: exp.WithinGroup) -> str:
        # PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY x): QUANTILE_CONT(x, q).
        fn, order = node.this, node.expression
        terms = order.expressions if isinstance(order, exp.Order) else []
        if (
            isinstance(fn, (exp.PercentileCont, exp.PercentileDisc))
            and len(terms) == 1
            and not terms[0].args.get("desc")
        ):
            kind = type(fn)
            return self.sql(kind(this=terms[0].this.copy(), expression=fn.this.copy()))
        return self.refuse("WITHIN GROUP is translated for PERCENTILE_CONT and PERCENTILE_DISC")

    def sign_sql(self, node) -> str:
        return f"{SIGN} {self.operand(node.this)}"

    abs_sql = upper_sql = lower_sql = floor_sql = ceil_sql = sqrt_sql = exp_sql = ln_sql = monadic

    def round_sql(self, node: exp.Round) -> str:
        places = node.args.get("decimals")
        if places is None:
            return f"{ROUND} {self.operand(node.this)}"
        return f"{ROUND_TO}[{self.sql(node.this)};{self.sql(places)}]"

    def pow_sql(self, node: exp.Pow) -> str:
        return f"{self.operand(node.this)} xexp {self.operand(node.expression)}"

    def intdiv_sql(self, node: exp.IntDiv) -> str:
        return f"{INT_DIV}[{self.sql(node.this)};{self.sql(node.expression)}]"

    def text_extreme(self, node: exp.Expr) -> str | None:
        items = [node.this, *node.expressions]
        if self.symbols or any(atomic_value(i) for i in items):
            return None
        word = TEXT_GREATEST if isinstance(node, exp.Greatest) else TEXT_LEAST
        text = self.sql(items[0])
        for item in items[1:]:
            text = f"{word}[{text};{self.sql(item)}]"
        return text

    def greatest_sql(self, node: exp.Greatest) -> str:
        text = self.text_extreme(node)
        if text is not None:
            return text  # string mode, columns of unknown type
        # | is q's max, and like GREATEST it skips a null: 1|0N is 1.
        return "|".join(self.operand(i) for i in [node.this, *node.expressions])

    def least_sql(self, node: exp.Least) -> str:
        text = self.text_extreme(node)
        if text is not None:
            return text
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
        if not self.symbols and not any(
            is_text(x) or atomic_value(x) for x in (node.this, node.expression)
        ):
            from sqlglot_q.truth import holds

            same = holds(self, exp.EQ(this=node.this.copy(), expression=node.expression.copy()))
            return f"{NULLIF_ANY}[{self.sql(node.this)};{same}]"
        case = exp.Case(
            ifs=[
                exp.If(
                    this=exp.EQ(this=node.this.copy(), expression=node.expression.copy()),
                    true=exp.Null(),
                )
            ],
            default=node.this.copy(),
        )
        if not self.symbols and any(is_text(x) for x in (node.this, node.expression)):
            case.meta["text"] = True  # NULLIF(w, ''): the branches are text
        return self.case_sql(case)

    def list_sql(self, node: exp.List) -> str:
        # sqlglot before 30.x reads DuckDB's LIST(n) as List, not ArrayAgg.
        if len(node.expressions) != 1:
            return self.refuse("LIST is translated as the aggregate of one value")
        return self.arrayagg_sql(exp.ArrayAgg(this=node.expressions[0].copy()))

    def dateadd_sql(self, node: exp.DateAdd) -> str:
        # DATE_ADD(d, INTERVAL 3 DAY): d + INTERVAL 3 DAY.
        span = node.expression
        if not isinstance(span, exp.Interval):
            span = exp.Interval(this=span.copy(), unit=exp.var(node.text("unit") or "DAY"))
        kind = exp.Sub if isinstance(node, exp.DateSub) else exp.Add
        return self.sql(kind(this=node.this.copy(), expression=span.copy()))

    datesub_sql = dateadd_sql

    def first_sql(self, node: exp.First) -> str:
        # FIRST and LAST (DuckDB's): the first and last row's value, in the
        # table's order, nulls and all: q's first and last.
        return f"first {self.operand(node.this)}"

    def last_sql(self, node: exp.Last) -> str:
        return f"last {self.operand(node.this)}"

    def arrayagg_sql(self, node: exp.ArrayAgg) -> str:
        if node.args.get("order"):
            return self.refuse("ARRAY_AGG with ORDER BY outside has no q translation here")
        # Under by, a group's values are a list already; over all rows, the
        # one list enlisted, a row.
        value = self.aggregated(node.this)
        if value.startswith("'"):
            return value
        if isinstance(node.this, exp.Distinct):
            value = f"asc {value}"  # SQL leaves the order open; sorted, as DuckDB's
        value = value if " " not in value else f"({value})"
        return value if getattr(self, "_by", False) else f"enlist {value}"

    def logical(self, node: exp.Expr) -> str:
        from sqlglot_q.truth import fails, holds

        cond = node.this
        while isinstance(cond, exp.Cast) and cond.to.this == exp.DataType.Type.BOOLEAN:
            cond = cond.this  # DuckDB writes BOOL_AND(CAST(c AS BOOLEAN))
        word = BOOL_AND if isinstance(node, exp.LogicalAnd) else BOOL_OR
        true, false = holds(self, cond), fails(self, cond)
        # An aggregate outermost, so select ... by takes it as one value.
        return f"first enlist {word}[{true};({true})|{false}]"

    logicaland_sql = logicalor_sql = logical

    def filter_sql(self, node: exp.Filter) -> str:
        agg, cond = node.this, node.expression.this
        where = self.condition(cond)
        if isinstance(agg, exp.Count) and (agg.this is None or isinstance(agg.this, exp.Star)):
            return f"count i where {where}"
        if type(agg) in AGGREGATES and not agg.args.get("distinct"):
            word = NULL_SAFE.get(type(agg), AGGREGATES[type(agg)])
            return f"{word} {self.operand(agg.this)} where {where}"
        if agg.find(exp.Window, exp.Select, exp.Subquery):
            return self.refuse(f"{agg.sql()} FILTER (...) has no q translation here")
        # Any other aggregate: each of its columns kept to the rows of the
        # filter, (c where cond), its literal arguments as they are.
        filtered = agg.copy().transform(
            lambda n: (
                QText(this=f"({self.sql(n)} where {where})") if isinstance(n, exp.Column) else n
            )
        )
        return self.sql(filtered)

    def trim_sql(self, node: exp.Trim) -> str:
        if node.expression is not None and not node.args.get("collation"):
            from sqlglot_q import scalars

            call = scalars.find(node)
            if call is not None:
                return scalars.write(self, node, call)
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
        if any(isinstance(p, exp.Null) for p in flatten_dpipe(node)):
            return self.refuse("x || NULL is NULL on every row: write NULL")
        text = self.joined(flatten_dpipe(node))
        columns = []
        for part in flatten_dpipe(node):
            if not isinstance(part, exp.Literal) and not constant(part) and part not in columns:
                columns.append(part)
        if not columns:
            return text
        # || is NULL when any side is; CONCAT skips nulls.
        if not self.symbols:
            # || is NULL when a side is; a null string is the empty one.
            guards = [self.is_null(self.sql(c)) for c in columns]
            guard = guards[0] if len(guards) == 1 else "|".join(f"({g})" for g in guards)
            return f'{TEXT_PICK}[{guard};(count i)#enlist "";{text}]'
        guards = [f"null {self.sql(c)}" for c in columns]
        guard = guards[0] if len(guards) == 1 else "|".join(f"({g})" for g in guards)
        empty = "`" if self.symbols else '""'
        return f"?[{guard};{empty};{text}]"

    def joined(self, parts: list[exp.Expr]) -> str:
        """Text parts joined row by row: ,' between two columns, ,\\: and ,/:
        where one side is a literal, so a literal string is not paired item
        by item with the rows."""
        # A NULL adds nothing to CONCAT, and || is guarded on it: its text is
        # the empty one, where string 0N would be paired with the rows.
        parts = [exp.Literal.string("") if isinstance(p, exp.Null) else p for p in parts]
        pieces = []
        for part in parts:
            if isinstance(part, exp.Literal) and part.is_string:
                pieces.append((self.chars(part.this), True))
            elif constant(part) and not getattr(self, "_lifting", False):
                # One value, as a literal is: joined to each row, not paired.
                text = self.operand(part)
                pieces.append((f"string {text}" if self.symbols else text, True))
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
        for n, (piece, lit) in enumerate(pieces[1:]):
            # q reads right to left: a,\:"-",'b would join "-" to b first.
            left = text if " " not in text and not n else f"({text})"
            right = piece if " " not in piece else f"({piece})"
            glyph = "," if atom and lit else ",\\:" if lit else ",/:" if atom else ",'"
            text, atom = f"{left}{glyph}{right}", atom and lit
        # Parenthesised: a select phrase is cut at its commas.
        return f"`$({text})" if self.symbols else f"({text})"

    def length_sql(self, node: exp.Length) -> str:
        if self.symbols:
            # LENGTH(NULL) is NULL, where count each string ` is 0.
            return f"{LENGTH} {self.operand(node.this)}"
        # q has no null string: the empty one stands for NULL, of no length.
        return f"{TEXT_LENGTH} {self.operand(node.this)}"

    def coalesce_sql(self, node: exp.Coalesce) -> str:
        # x^y is y with its nulls filled from x, so the first argument goes last.
        items = [node.this, *node.expressions]
        q_like = q_comparison(node)
        if q_like is not None:
            # COALESCE(n > 3, FALSE) is what q's n>3 is, nulls and all.
            return self.sql(q_like)
        if len(items) == 2 and isinstance(items[1], exp.Boolean) and condition_like(items[0]):
            # COALESCE(c, FALSE): where c is true; COALESCE(c, TRUE): where it
            # is not false. (q's comparison is never null: ^ would not fill.)
            from sqlglot_q.truth import fails, holds

            if items[1].this:
                return f"not {fails(self, items[0])}"  # q reads not of all of it
            return holds(self, items[0])
        if (
            not self.symbols
            and len(items) == 2
            and not any(atomic_value(i) or is_text(i) or isinstance(i, exp.Null) for i in items)
        ):
            # Two columns, strings or not: chosen when the query runs.
            return f"{ANY_FILL}[{self.sql(items[0])};{self.sql(items[1])}]"
        if not self.symbols and any(is_text(i) for i in items):
            # ^ cannot fill strings: the first non-empty, row by row.
            text = self.text_rows(items[-1])
            for item in reversed(items[:-1]):
                text = f"{TEXT_FILL}[{self.text_rows(item)};{text}]"
            return text
        return "^".join(self.operand(i) for i in reversed(items))

    def cast_sql(self, node: exp.Cast) -> str:
        kind = node.to.this
        if is_text(node.this) and kind in TEMPORAL:
            # DATE '2024-01-15' is a literal: q writes it 2024.01.15.
            return self.temporal(node.this.this, kind)
        if is_text(node.this) and kind in TYPES and TYPES[kind][0] in PARSE:
            # CAST('5' AS BIGINT) parses the text: q's "J"$"5". `long$ of
            # the symbol `5 is 'type.
            if TYPES[kind][0] == "boolean":
                # SQL's true/t/yes/1 (any case); q's "B"$ reads only t and 1.
                return f"{TEXT_BOOL} {self.chars(node.this.this)}"
            return f'"{PARSE[TYPES[kind][0]]}"${self.chars(node.this.this)}'
        arg = self.operand(node.this)
        if kind in TEXT:
            return f"`${TEXT_OF} {arg}" if self.symbols else f"{TEXT_OF} {arg}"
        if kind not in TYPES:
            return self.refuse(f"CAST to {node.to.sql()} has no q type here")
        name = TYPES[kind][0]
        literal = node.this.this if isinstance(node.this, exp.Neg) else node.this
        if name in INTEGERS and isinstance(literal, exp.Literal) and not literal.is_string:
            # 2.5 is a DECIMAL, which SQL casts rounding half away from
            # zero, as q's `long$ does (2.5e0, a DOUBLE, is cast half even).
            if "e" not in literal.this.lower():
                return f"`{name}${arg}"
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
        lambdas = {"DOW": DOW, "DAYOFWEEK": DOW, "DOY": DOY, "DAYOFYEAR": DOY}
        lambdas |= {"QUARTER": QUARTER, "MILLISECOND": MILLISECOND, "MILLISECONDS": MILLISECOND}
        lambdas |= {k: v for k, v in EXTRA_PARTS.items() if k != "ISODOW"}
        lambdas |= {"MICROSECONDS": EXTRA_PARTS["MICROSECOND"]}
        if part in lambdas:
            return f"{lambdas[part]} {self.operand(node.expression)}"
        if part in ("EPOCH", "WEEK", "ISODOW"):
            # As EPOCH(x), WEEK(x) and ISODOW(x), the lambdas of scalars.py.
            kind = {"EPOCH": exp.TimeToUnix, "WEEK": exp.Week, "ISODOW": exp.DayOfWeekIso}[part]
            return self.sql(kind(this=node.expression.copy()))
        if part not in PARTS:
            return self.refuse(f"EXTRACT({part}) has no q cast")
        return f"`{PARTS[part]}${self.operand(node.expression)}"

    def date_part(self, node: exp.Expr) -> str:
        from sqlglot_q.temporal import PARTS

        part = {
            exp.Year: "YEAR",
            exp.Month: "MONTH",
            exp.Day: "DAY",
            exp.Hour: "HOUR",
            exp.Minute: "MINUTE",
            exp.Second: "SECOND",
            exp.DayOfMonth: "DAY",
        }[type(node)]
        return f"`{PARTS[part]}${self.operand(node.this)}"

    year_sql = month_sql = day_sql = hour_sql = minute_sql = second_sql = date_part
    dayofmonth_sql = date_part

    def datebin_sql(self, node: exp.DateBin) -> str:
        from sqlglot_q.temporal import DAY, nanoseconds

        origin = node.args.get("origin") or node.args.get("unit")  # DuckDB's, in unit
        if node.args.get("zone"):
            return self.refuse("TIME_BUCKET with a time zone has no q translation")
        width = node.this
        if origin is not None and isinstance(origin, (exp.Cast, exp.Literal)):
            # From an origin: its buckets, origin + w xbar x - origin, of any
            # fixed width.
            if not (isinstance(width, exp.Interval) and nanoseconds(width)):
                return self.refuse("TIME_BUCKET from an origin is translated with a fixed width")
            args = (self.sql(width), self.sql(node.expression), self.sql(origin))
            return f"{BUCKET_FROM}[{';'.join(args)}]"
        if origin is not None:
            return self.refuse("TIME_BUCKET with an origin or offset has no q xbar")
        ns = nanoseconds(width) if isinstance(width, exp.Interval) else None
        if ns is None or DAY % ns:
            # q counts buckets from 2000.01.01, DuckDB from 2000-01-03: they
            # agree only for widths that divide a day.
            return self.refuse("TIME_BUCKET is translated for widths that divide a day")
        # A date is its own bucket for a width that divides a day; xbar of a
        # date by a timespan counts its days as nanoseconds.
        return f"{BUCKET}[{self.sql(width)};{self.sql(node.expression)}]"

    def datetrunc_sql(self, node: exp.DateTrunc) -> str:
        # DATE_TRUNC of a DATE (sqlglot's DateTrunc): DuckDB's answer is the
        # TIMESTAMP it gives a timestamp, so the same truncation.
        unit = node.args.get("unit")
        name = unit.name if isinstance(unit, exp.Expr) else str(unit)
        return self.timestamptrunc_sql(
            exp.TimestampTrunc(this=node.this.copy(), unit=exp.var(name.upper()))
        )

    def timestamptrunc_sql(self, node: exp.TimestampTrunc) -> str:
        from sqlglot_q.temporal import UNITS, span_text

        unit = node.text("unit").upper().rstrip("S")
        if unit in TRUNC:
            # A month is no fixed length: truncated through q's month type.
            return f"{TRUNC[unit]} {self.operand(node.this)}"
        if unit not in UNITS or UNITS[unit] > UNITS["DAY"]:
            return self.refuse(f"DATE_TRUNC to {unit} has no q xbar")
        arg = node.this
        timestamps = (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.DATETIME)
        if not (isinstance(arg, exp.Cast) and arg.to.this in timestamps):
            # A timespan xbar of a date counts in days from 2000.01.01 as if
            # they were nanoseconds: a timestamp first.
            arg = exp.Cast(this=arg.copy(), to=exp.DataType.build("TIMESTAMP"))
        return f"{span_text(UNITS[unit])} xbar {self.sql(arg)}"

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
            if not self.symbols:
                # A column of strings: "" on each row, where x 0N is one "".
                return f"{NULL_LIKE} {self.operand(known)}"
            return f"{self.operand(known)} 0N"

        subject = node.this
        text_branches = not self.symbols and (
            node.meta.get("text")
            or any(is_text(b) for b in branches if b is not None)
            or any(b in text_columns(node) for b in branches if b is not None)
        )
        if text_branches:
            # ?[...] cannot pick between strings: each row picks with $[...].
            text = self.text_rows(default) if default is not None else '(count i)#enlist ""'
        else:
            text = value(default)
        for when in reversed(node.args["ifs"]):
            cond = when.this
            if subject is not None:
                cond = exp.EQ(this=subject.copy(), expression=cond.copy())
            if text_branches:
                then = self.text_rows(when.args["true"])
                text = f"{TEXT_PICK}[{self.condition(cond)};{then};{text}]"
            else:
                text = f"?[{self.condition(cond)};{value(when.args['true'])};{text}]"
        return text

    def text_rows(self, node: exp.Expr) -> str:
        """A text value as one per row: a literal is repeated for each."""
        if is_text(node) or isinstance(node, exp.Null):
            lit = '""' if isinstance(node, exp.Null) else self.chars(node.this)
            return f"(count i)#enlist {lit}"
        return self.operand(node)

    def is_null(self, text: str) -> str:
        """q true where the value is null; for text in string mode, empty."""
        return f"null {text}" if self.symbols else f"{TEXT_NULL} {text}"

    def if_sql(self, node: exp.If) -> str:
        otherwise = node.args.get("false")
        if otherwise is None:
            return self.refuse("IF without an else value has no q form")
        return f"?[{self.condition(node.this)};{self.sql(node.args['true'])};{self.sql(otherwise)}]"

    def anonymous_sql(self, node: exp.Anonymous) -> str:
        name, args = node.name.upper(), node.expressions
        if name == "DATE_SUB" and len(args) == 2 and isinstance(args[1], exp.Interval):
            return self.sql(exp.Sub(this=args[0].copy(), expression=args[1].copy()))
        if name == "DATE_PART" and len(args) == 2 and isinstance(args[0], exp.Literal):
            return self.sql(
                exp.Extract(this=exp.var(args[0].name.upper()), expression=args[1].copy())
            )
        if name in ("PREFIX", "SUFFIX") and len(args) == 2:
            kind = exp.StartsWith if name == "PREFIX" else exp.EndsWith
            return self.sql(kind(this=args[0].copy(), expression=args[1].copy()))
        if name == "WEEKDAY" and len(args) == 1:
            return f"{DOW} {self.operand(args[0])}"  # Sunday 0, as DAYOFWEEK
        if node.name.upper() in ANONYMOUS and node.name.upper() not in self.FUNCTIONS:
            fn, aggregate = ANONYMOUS[node.name.upper()]
            text = f"{fn}[{';'.join(self.sql(a) for a in node.expressions)}]"
            return f"first enlist {text}" if aggregate else text
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
        from sqlglot_q.correlated import refuse_correlated

        select = node.this
        if not isinstance(select, exp.Select) or len(select.expressions) != 1:
            return self.refuse("a scalar subquery needs exactly one column")
        refused = refuse_correlated(self, select, "a scalar subquery")
        if refused is not None:
            return refused
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
        from sqlglot_q.query import inline_ctes, limited, order_terms, output_names, sort

        if node.args.get("with_"):
            # WITH c AS (...) SELECT ... UNION SELECT ...: c written in, as a
            # select's own CTEs are.
            return self.sql(inline_ctes(node))

        if node.args.get("by_name"):
            if node.args.get("distinct"):
                return self.refuse("UNION BY NAME without ALL has no q translation here")
            # UNION ALL BY NAME: q's uj, the columns matched by name.
            both = f"{self.operand_query(node.this)} uj {self.operand_query(node.expression)}"
            return limited(self, node, both)
        both = f"{self.operand_query(node.this)},{self.operand_query(aligned(node))}"
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
            text = sort(terms, text, self)
        return limited(self, node, text)

    def set_operation(self, node: exp.Expr, word: str) -> str:
        from sqlglot_q.query import inline_ctes, limited, order_terms, output_names, sort

        if node.args.get("with_"):
            return self.sql(inline_ctes(node))

        def ordered(text: str) -> str:
            # ORDER BY after it names the columns of its first query.
            order = node.args.get("order")
            if order:
                first = node.this
                while isinstance(first, (exp.SetOperation, exp.Subquery)):
                    first = first.this
                items = list(first.expressions)
                terms, _ = order_terms(self, order, items, output_names(items), allow_source=False)
                text = sort(terms, text, self)
            return limited(self, node, text)

        if not node.args.get("distinct"):
            word = INTERSECT_ALL if word == "inter" else EXCEPT_ALL
            left, right = self.operand_query(node.this), self.operand_query(aligned(node))
            return ordered(f"{word}[{left};{right}]")
        # The rows of the first in (or not in) the second, each once, as
        # SQL's set operations; q's except and inter compare whole rows.
        left, right = self.operand_query(node.this), self.operand_query(aligned(node))
        return ordered(f"distinct {left} {word} {right}")

    def intersect_sql(self, node: exp.Intersect) -> str:
        return self.set_operation(node, "inter")

    def except_sql(self, node: exp.Except) -> str:
        return self.set_operation(node, "except")

    def operand_query(self, node: exp.Expr) -> str:
        """A query used as a table: FROM (...), a join side, a UNION operand."""
        if isinstance(node, exp.Subquery):
            node = node.this
        return f"({self.sql(node)})"

    # Statements -----------------------------------------------------------

    def insert_sql(self, node: exp.Insert) -> str:
        if node.args.get("with_"):
            # WITH c AS (...) INSERT INTO t SELECT ... FROM c: c written in.
            from sqlglot_q.query import inline_ctes

            return self.sql(inline_ctes(node))
        if node.args.get("returning"):
            return self.refuse("RETURNING has no q translation")
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
        name = self.table_sql(target)
        if isinstance(source, exp.Values):
            rows = [r.expressions for r in source.expressions]

            def column_of(i: int) -> str:
                # The target's column: a NULL in it is that column's null.
                return f"{name}`{columns[i]}" if columns else f"{name} (cols {name}) {i}"

            def cell(i: int) -> str:
                values = [row[i] for row in rows]
                nulls = [n for n, v in enumerate(values) if isinstance(v, exp.Null)]
                null = f"first 0#{column_of(i)}" if self.symbols else f"{NULL_OF} {column_of(i)}"
                known = [v for v in values if not isinstance(v, exp.Null)]
                if nulls and known and not any(is_text(v) for v in known):
                    nulls = []  # 70 0N: a number's null is q's 0N already
                if nulls and known and self.symbols and len(rows) > 1:
                    # `e`: a null symbol is the empty one, in the list itself.
                    values = [
                        exp.Literal.string("") if isinstance(v, exp.Null) else v for v in values
                    ]
                    nulls = []
                if not nulls:
                    return self.vector(values) if len(rows) > 1 else self.sql(values[0])
                if len(rows) == 1:
                    return null
                if not known:
                    return f"{len(rows)}#{null}"
                filled = self.vector([known[0] if isinstance(v, exp.Null) else v for v in values])
                return f"@[{filled};{' '.join(map(str, nulls))};:;{null}]"

            width = len(columns) if columns else len(rows[0])
            cells = [cell(i) for i in range(width)]
            if width == 1 and len(rows) > 1 and not columns:
                # One column of several rows: (1 2) would be a row of two.
                return f"{table} {verb} enlist {cells[0]}"
            if columns:
                body = ";".join(f"{c}:{v}" for c, v in zip(columns, cells, strict=True))
                if len(rows) == 1:
                    body = ";".join(f"{c}:enlist {v}" for c, v in zip(columns, cells, strict=True))
                return f"{table} {verb} ([] {body})"
            return f"{table} {verb} ({';'.join(cells)})"
        # SQL inserts by position (or into the columns listed): the query's
        # columns renamed to the target's, as q's insert matches names.
        names = "".join(f"`{c}" for c in columns) if columns else f"(cols {name})"
        return f"{table} {verb} {names} xcol {self.operand_query(source)}"

    def update_sql(self, node: exp.Update) -> str:
        if node.args.get("with_"):
            from sqlglot_q.query import inline_ctes

            return self.sql(inline_ctes(node))  # its CTEs written in
        if node.args.get("returning"):
            return self.refuse("RETURNING has no q translation")
        if node.args.get("from_") or node.args.get("joins"):
            # UPDATE ... FROM u: each row's value from a row of u. Written as
            # a plain update, t.g = u.g would read g=g, true on every row.
            return self.refuse("UPDATE ... FROM has no q translation here")
        where = self.where(node.args.get("where"))
        sets = []
        for e in node.expressions:
            col = self.name(e.this.name)
            if isinstance(e.expression, exp.Null):
                # The column's own null: 0N is a long, 'type into symbols.
                if self.symbols:
                    sets.append(f"{col}:first 0#{col}")
                else:
                    sets.append(f"{col}:{TEXT_SPREAD}[i;{NULL_OF} {col}]")
                continue
            if not self.symbols and (is_text(e.expression) or constant(e.expression)):
                # A string is a list of characters: one value per row.
                sets.append(f"{col}:{TEXT_SPREAD}[i;{self.sql(e.expression)}]")
                continue
            lookup = correlated_lookup(self, node, e.expression)
            value = lookup if lookup is not None else self.sql(e.expression)
            if not (not self.symbols and is_text(e.expression)):
                # A column keeps its type, as SQL's: q refuses 0 into a
                # float column ('type) where SQL converts, and v%3 would make
                # a long column float where SQL rounds it back.
                value = f"(type {col})${self.operand_text(e.expression, value)}"
            sets.append(f"{col}:{value}")
        sets = ",".join(sets)
        return f"update {sets} from `{self.table_sql(node.this)}{where}"

    def values_sql(self, node: exp.Values) -> str:
        # (VALUES (1, 'a'), (2, 'b')) AS v(k, s): the table ([] k:1 2;s:`a`b).
        alias = node.args.get("alias")
        names = [c.name for c in alias.columns] if alias is not None else []
        rows = [r.expressions if isinstance(r, exp.Tuple) else [r] for r in node.expressions]
        width = len(rows[0])
        if len(names) != width or any(len(r) != width for r in rows):
            return self.refuse("VALUES is translated as a table with its columns named")
        cols = []
        for i, name in enumerate(names):
            values = [r[i] for r in rows]
            text = self.vector(values) if len(values) > 1 else f"enlist {self.operand(values[0])}"
            cols.append(f"{self.name(name)}:{text}")
        return f"([] {';'.join(cols)})"

    def alter_sql(self, node: exp.Alter) -> str:
        if (node.args.get("kind") or "").upper() != "TABLE" or len(node.args["actions"]) != 1:
            return self.refuse("ALTER is translated for one change to a table")
        name = self.table_sql(node.this)
        action = node.args["actions"][0]
        if isinstance(action, exp.ColumnDef):
            # ADD COLUMN c T: c of T's null on every row.
            kind = action.args.get("kind")
            if kind is not None and kind.this in TEXT:
                empty = "`" if self.symbols else '(count i)#enlist ""'
                return f"update {self.name(action.name)}:{empty} from `{name}"
            if kind is None or kind.this not in TYPES:
                return self.refuse("ALTER TABLE ADD COLUMN needs a type q has")
            return f"update {self.name(action.name)}:first {TYPES[kind.this][1]} from `{name}"
        if isinstance(action, exp.RenameColumn):
            old, new = self.name(action.this.name), self.name(action.args["to"].name)
            return f"`{name} set @[cols {name};where (cols {name})=`{old};:;`{new}] xcol {name}"
        if isinstance(action, exp.Drop) and (action.args.get("kind") or "").upper() == "COLUMN":
            dropped = action.args.get("tables") or [action.this]
            return f"delete {','.join(self.name(c.name) for c in dropped)} from `{name}"
        if isinstance(action, exp.AlterRename):
            return f"{self.table_sql(action.this)}:{name};delete {name} from `."
        return self.refuse(f"ALTER TABLE {action.key.upper()} has no q translation here")

    def delete_sql(self, node: exp.Delete) -> str:
        if node.args.get("with_"):
            from sqlglot_q.query import inline_ctes

            return self.sql(inline_ctes(node))  # its CTEs written in
        if node.args.get("using"):
            # DELETE ... USING u WHERE t.k = u.k: written as a plain delete,
            # t.k = u.k would read k=k, every row.
            return self.refuse("DELETE ... USING has no q translation here")
        if node.args.get("returning"):
            return self.refuse("RETURNING has no q translation")
        return f"delete from `{self.table_sql(node.this)}{self.where(node.args.get('where'))}"

    def create_sql(self, node: exp.Create) -> str:
        text = self.created(node)
        if node.args.get("exists") and not text.startswith("'"):
            # IF NOT EXISTS: an existing table is kept as it is.
            name = self.table_sql(
                node.this.this if isinstance(node.this, exp.Schema) else node.this
            )
            return f"if[not `{name} in tables[];{text}]"
        return text

    def created(self, node: exp.Create) -> str:
        schema = node.this
        query = node.expression
        if node.args.get("kind") == "VIEW" and isinstance(schema, exp.Table) and query:
            # A q view: its query, computed when it is read.
            return f"{self.table_sql(schema)}::{self.sql(query)}"
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


def text_columns(node: exp.Case) -> list[exp.Expr]:
    """Columns the CASE's conditions compare with text: they are text."""
    found = []
    for when in node.args["ifs"]:
        for cmp in when.this.find_all(exp.EQ, exp.NEQ, exp.Like):
            sides = (cmp.this, cmp.expression)
            if any(is_text(x) for x in sides):
                found += [x for x in sides if isinstance(x, exp.Column)]
    if node.this is not None and any(is_text(w.this) for w in node.args["ifs"]):
        found.append(node.this)
    return found


def q_comparison(node: exp.Coalesce):
    """The comparison in COALESCE(cmp, b) when b is what q's cmp gives on a
    null (qnulls.py's reading of q's comparisons as values)."""
    from sqlglot_q.qnulls import CONTAINS, ORDERED, null_result

    if len(node.expressions) != 1 or not isinstance(node.expressions[0], exp.Boolean):
        return None
    cmp = node.this
    while isinstance(cmp, exp.Paren):
        cmp = cmp.this
    want = node.expressions[0].this
    if isinstance(cmp, (*ORDERED, exp.EQ)) and null_result(cmp) is want:
        return cmp
    if isinstance(cmp, CONTAINS) and want is False:
        return cmp
    return None


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


def correlated_lookup(gen, update: exp.Update, value: exp.Expr) -> str | None:
    """SET c = (SELECT AGG(x) FROM u WHERE u.k = t.k ...): the subquery
    grouped by its key, looked up by each row's key; NULL (COUNT's 0) where
    no row matches."""
    from sqlglot_q.correlated import outer_columns, semi_join

    query = value.this if isinstance(value, exp.Subquery) else None
    if not isinstance(query, exp.Select) or not outer_columns(query):
        return None
    found = semi_join(query)
    if found is None or len(query.expressions) != 1:
        return None
    outer, inner, rest = found
    if outer.table not in ("", update.this.alias_or_name):
        return None
    agg = query.expressions[0]
    agg = agg.this if isinstance(agg, exp.Alias) else agg
    if not isinstance(agg, exp.AggFunc):
        return None
    grouped = exp.Select(
        expressions=[exp.alias_(inner.copy(), "q_k"), exp.alias_(agg.copy(), "q_c")]
    ).from_(query.args["from_"].this.copy(), copy=False)
    if rest:
        grouped.set("where", exp.Where(this=exp.and_(*(c.copy() for c in rest), copy=False)))
    grouped.set("group", exp.Group(expressions=[inner.copy()]))
    keyed = gen.sql(grouped).removeprefix("0!")
    text = f"(({keyed})([] q_k:{gen.name(outer.name)}))`q_c"
    return f"0^{text}" if isinstance(agg, exp.Count) else text


def null_side(node: exp.Expr):
    """(the NULL, the other side) of x IS [NOT] DISTINCT FROM NULL, or Nones."""
    if isinstance(node.expression, exp.Null):
        return node.expression, node.this
    if isinstance(node.this, exp.Null):
        return node.this, node.expression
    return None, None


def condition_like(node: exp.Expr) -> bool:
    """A comparison, a test or a connective of them: true or false as it reads."""
    while isinstance(node, exp.Paren):
        node = node.this
    return isinstance(
        node,
        (
            exp.Predicate,
            exp.Connector,
            exp.Not,
            exp.Boolean,
            exp.Exists,
            exp.Escape,
            exp.StartsWith,
            exp.EndsWith,
            exp.Contains,
            exp.RegexpLike,
            exp.LogicalAnd,
            exp.LogicalOr,
        ),
    )


def atomic_value(node: exp.Expr) -> bool:
    """A literal number, date or boolean: not text, whatever the columns hold."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Neg):
        node = node.this
    if isinstance(node, exp.Literal):
        return not node.is_string
    if isinstance(node, exp.Cast) and is_text(node.this):
        return node.to.this in TEMPORAL  # DATE '...'
    return isinstance(node, (exp.Boolean, exp.Interval, exp.CurrentDate, exp.CurrentTimestamp))


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


def like_pattern(sql: str, escape: str | None = None) -> str:
    """A SQL LIKE pattern as a q like pattern: % is *, _ is ?, and q's own
    wildcard characters are bracketed so they match themselves. After the
    ESCAPE character, % and _ are themselves (q's like has no escape)."""
    out = []
    chars = iter(sql)
    for ch in chars:
        if escape is not None and ch == escape:
            ch = next(chars, "")
            out.append(f"[{ch}]" if ch in "*?[]" else ch)
        elif ch == "%":
            if not (out and out[-1] == "*"):
                out.append("*")  # q's like raises 'nyi on **; %% is one %
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

    from sqlglot_q.rewrites import (
        correlated_scalars,
        expand_group_by_all,
        grouping_sets,
        join_conditions,
        like_any,
        named_windows,
        order_by_expression,
        q_names,
        quantified_in,
        regex_like,
        row_values,
        table_star,
        unnest_list,
        window_over_group,
    )

    # sqlglot's own rewrites, as its dialects use them: QUALIFY and DISTINCT
    # ON become a window in a subquery, which the window stages translate.
    return transforms.preprocess(
        [
            unnest_list,
            like_any,
            row_values,
            named_windows,
            grouping_sets,
            window_over_group,
            transforms.eliminate_qualify,
            transforms.eliminate_distinct_on,
            expand_group_by_all,
            table_star,
            quantified_in,
            correlated_scalars,
            join_conditions,
            regex_like,
            order_by_expression,
            q_names,
        ]
    )


def _scalar_calls():
    from sqlglot_q.scalars import CALLS

    return CALLS


def scalar(self, node: exp.Expr) -> str:
    from sqlglot_q import scalars

    call = scalars.find(node)
    if call is None:
        return self.refuse(f"{node.key.upper()} with these arguments has no q translation")
    return scalars.write(self, node, call)


def null_of(self, node) -> str:
    # A grouping set's missing key: its column's null, typed as the column.
    # A string's null is the empty one, a cell under by, a row of one else.
    if not self.symbols:
        return '""' if getattr(self, "_by", False) else 'enlist ""'
    return f"first 0#{self.sql(node.this)}"


from sqlglot_q.rewrites import NullOf  # noqa: E402

QGenerator.TRANSFORMS = {
    QText: qtext,
    NullOf: null_of,
    exp.Pi: lambda self, node: "3.141592653589793",
    # The local date and time: .z.d and .z.p are UTC's.
    exp.CurrentDate: lambda self, node: ".z.D",
    exp.CurrentTimestamp: lambda self, node: ".z.P",
    exp.CurrentTime: lambda self, node: ".z.T",
    # The functions in scalars.py, each a q lambda.
    **{call.kind: scalar for call in _scalar_calls() if call.kind not in (exp.DateDiff, exp.Trim)},
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
    # COUNT_IF(c) is COUNT(*) FILTER (WHERE c): count i where c.
    exp.CountIf: lambda self, node: f"count i where {self.condition(node.this)}",
    exp.AnyValue: monad("first", skip_nulls=True),
    # DuckDB parses ANY_VALUE(x) as IGNORE NULLS around it: first skips them.
    exp.IgnoreNulls: lambda self, node: (
        self.sql(node.this)
        if isinstance(node.this, exp.AnyValue)
        else self.refuse("IGNORE NULLS is translated for ANY_VALUE only")
    ),
}


def aligned(node: exp.Expr) -> exp.Expr:
    """The second query of a UNION (INTERSECT, EXCEPT) with the first's
    column names: SQL names the result's columns by the first query, and q's
    , (and inter, except) wants the same names on both sides."""
    from sqlglot_q.query import output_names

    first = node.this
    while isinstance(first, (exp.SetOperation, exp.Subquery)):
        first = first.this
    second = node.expression
    inner = second.this if isinstance(second, exp.Subquery) else second
    if not (isinstance(first, exp.Select) and isinstance(inner, exp.Select)):
        return second
    names, theirs = output_names(first.expressions), output_names(inner.expressions)
    # An unnamed item of the second (SELECT 'zz') takes the first's name too.
    if None in names or names == theirs or len(names) != len(theirs):
        return second
    inner = inner.copy()
    inner.set(
        "expressions",
        [
            exp.alias_(unalias(i), n) if t != n else i
            for i, n, t in zip(inner.expressions, names, theirs, strict=True)
        ],
    )
    return inner


CALENDAR = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}


def calendar_months(node: exp.Expr) -> int | None:
    """x + INTERVAL n MONTH (or QUARTER, YEAR), and x - ...: the months added."""
    right = node.expression
    if not (isinstance(node, (exp.Add, exp.Sub)) and isinstance(right, exp.Interval)):
        return None
    unit = (right.text("unit") or "").upper().rstrip("S")
    value = right.this
    if unit not in CALENDAR or not isinstance(value, exp.Literal):
        return None
    try:
        n = int(value.this)
    except ValueError:
        return None
    n *= CALENDAR[unit]
    return -n if isinstance(node, exp.Sub) else n


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
