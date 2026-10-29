"""SQL functions q has no word for, each as one q lambda applied to its
arguments, f[a;b]. The table is read both ways: the generator writes a node
of the class as the lambda, and the parser reads the lambda back as the node.

An aggregate is written first enlist f[...]: q's select takes a lambda's
result as one row only when an aggregate is outermost.

Text functions have two lambdas, one for symbols (the default) and one for
char vectors (string_literals="string", where an empty string is NULL). A
text argument marked "chars" is written as a q string whatever the mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from sqlglot_q.generator import TEXT_KEY, TEXT_NULL

# The first occurrence of y in x, from 1; 0 if none.
FIND = "{n:count y;$[count w:where y~/:n sublist/:(til count x)_\\:x;1+first w;0]}"
DAYS = ("Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
# x plus y months, the day clamped to the month's last (Jan 31 + 1 is Feb 29),
# the time of day kept; a timestamp, as SQL's date + interval is.
ADD_MONTHS = "{m:y+`month$x;f:`date$m;`timestamp$(x-`date$x)+f+((-1+`date$m+1)-f)&(`dd$x)-1}"


@dataclass(frozen=True)
class Call:
    kind: type
    symbol: str
    text: str | None = None  # None: the same lambda in both modes
    keys: tuple[str, ...] = ("this",)
    chars: tuple[str, ...] = ()  # keys written as q strings
    fixed: dict = field(default_factory=dict)  # args the node must have
    aggregate: bool = False

    def lambdas(self) -> tuple[str, ...]:
        return (self.symbol,) if self.text is None else (self.symbol, self.text)


CALLS = [
    # TRUNC(x): toward zero.
    Call(exp.Trunc, "{(signum x)*a-(a:abs x) mod 1}"),
    Call(
        exp.Pad,
        "{?[null x;`;`${$[y<=count x;y#x;((y-count x)#z),x]}[;y;(),z] each string x]}",
        "{{$[0=count x;x;y<=count x;y#x;((y-count x)#z),x]}[;y;(),z] each x}",
        keys=("this", "expression", "fill_pattern"),
        chars=("fill_pattern",),
        fixed={"is_left": True},
    ),
    Call(
        exp.Pad,
        "{?[null x;`;`${$[y<=count x;y#x;x,(y-count x)#z]}[;y;(),z] each string x]}",
        "{{$[0=count x;x;y<=count x;y#x;x,(y-count x)#z]}[;y;(),z] each x}",
        keys=("this", "expression", "fill_pattern"),
        chars=("fill_pattern",),
        fixed={"is_left": False},
    ),
    Call(
        exp.Repeat,
        "{`${raze y#enlist x}[;y] each string x}",
        "{{raze y#enlist x}[;y] each x}",
        keys=("this", "times"),
    ),
    Call(
        exp.StrPosition,
        f"{{?[null x;0N;{FIND}[;(),y] each string x]}}",
        f"{{?[0=count each x;0N;{FIND}[;(),y] each x]}}",
        keys=("this", "substr"),
        chars=("substr",),
    ),
    Call(exp.LastDay, "{-1+`date$1+`month$x}"),
    Call(
        exp.DateFromParts,
        "{-1+z+`date$2000.01m+(12*x-2000)+y-1}",
        keys=("year", "month", "day"),
    ),
    Call(
        exp.Dayname,
        "{`" + "`".join(DAYS) + " (`date$x) mod 7}",
        "{(" + ";".join(f'"{d}"' for d in DAYS) + ") (`date$x) mod 7}",
    ),
    # ISO week: the week of the year its Thursday falls in.
    Call(exp.Week, "{t:3+`week$x;?[null x;0Ni;1+(t-`date$`month$12*(`year$t)-2000) div 7]}"),
    # DATE_DIFF counts the boundaries crossed: months, or years, apart.
    Call(
        exp.DateDiff,
        "{(`month$x)-`month$y}",
        keys=("this", "expression"),
        fixed={"unit": "MONTH"},
    ),
    Call(
        exp.DateDiff,
        "{(`year$x)-`year$y}",
        keys=("this", "expression"),
        fixed={"unit": "YEAR"},
    ),
    # Aggregates, each over the non-null values and NULL over none.
    Call(
        exp.ArgMax,
        "{w:where not null[x]|null y;first x w idesc y w}",
        f"{{w:where not ({TEXT_NULL} x)|{TEXT_NULL} y;"
        f'$[count w;x w first idesc {TEXT_KEY} y w;""]}}',
        keys=("this", "expression"),
        aggregate=True,
    ),
    Call(
        exp.ArgMin,
        "{w:where not null[x]|null y;first x w iasc y w}",
        f"{{w:where not ({TEXT_NULL} x)|{TEXT_NULL} y;"
        f'$[count w;x w first iasc {TEXT_KEY} y w;""]}}',
        keys=("this", "expression"),
        aggregate=True,
    ),
    Call(
        exp.Mode,
        "{$[count v:x where not null x;first key desc count each group v;x 0N]}",
        '{$[count v:x where 0<count each x;first key desc count each group v;""]}',
        aggregate=True,
    ),
    # QUANTILE_CONT: interpolated between the sorted values around y.
    Call(
        exp.PercentileCont,
        "{v:asc x where not null x;r:y*-1+count v;f:floor r;(v f)+(r-f)*(v(-1+count v)&f+1)-v f}",
        keys=("this", "expression"),
        aggregate=True,
    ),
]

READ = {text: call for call in CALLS for text in call.lambdas()}


def unit_of(node: exp.Expr) -> str:
    unit = node.args.get("unit")
    return (unit.name if isinstance(unit, exp.Expr) else str(unit or "DAY")).upper().rstrip("S")


def matches(call: Call, node: exp.Expr) -> bool:
    for key, value in call.fixed.items():
        have = unit_of(node) if key == "unit" else bool(node.args.get(key))
        if have != value:
            return False
    if not all(node.args.get(k) is not None for k in call.keys):
        return False
    # An argument the lambda does not take (TRUNC(x, 1), STRPOS from a
    # position) would be dropped: no match, and so refused.
    return not any(
        value not in (None, False)
        for key, value in node.args.items()
        if key not in call.keys and key not in call.fixed
    )


def find(node: exp.Expr) -> Call | None:
    for call in CALLS:
        if isinstance(node, call.kind) and matches(call, node):
            return call
    return None


def write(gen, node: exp.Expr, call: Call) -> str:
    fn = call.symbol if gen.symbols or call.text is None else call.text
    args = []
    for key in call.keys:
        arg = node.args[key]
        if key in call.chars:
            if not (isinstance(arg, exp.Literal) and arg.is_string):
                return gen.refuse(f"{node.key.upper()} is translated with literal text")
            args.append(gen.chars(arg.this))
        else:
            args.append(gen.sql(arg))
    text = f"{fn}[{';'.join(args)}]"
    return f"first enlist {text}" if call.aggregate else text


def read(call: Call, args: list[exp.Expr]) -> exp.Expr | None:
    if len(args) != len(call.keys):
        return None
    values = dict(zip(call.keys, args, strict=True))
    for key, value in call.fixed.items():
        values[key] = exp.var(value) if key == "unit" else value
    return call.kind(**values)
