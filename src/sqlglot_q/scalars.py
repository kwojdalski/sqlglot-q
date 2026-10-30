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
# The edit distance of x to y, row by row.
LEV = (
    "{m:count x;n:count y;d:til n+1;i:0;"
    "while[i<m;e:d;d:(i+1),n#0;j:0;"
    "while[j<n;d[j+1]:min(e[j+1]+1;d[j]+1;e[j]+not x[i]=y[j]);j+:1];i+:1];last d}"
)
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def iso_format(fmt) -> str | None:
    """Why STRPTIME's format has no q parse, or None: the ISO forms "P"$ reads."""
    forms = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
    if isinstance(fmt, exp.Literal) and fmt.is_string and fmt.this in forms:
        return None
    return "STRPTIME is translated for %Y-%m-%d and %Y-%m-%d %H:%M:%S"


DAYS = ("Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
# x plus y months, the day clamped to the month's last (Jan 31 + 1 is Feb 29),
# the time of day kept; a timestamp, as SQL's date + interval is.
ADD_MONTHS = (
    "{m:y+`month$x;f:`date$m;(`timestamp$f+((-1+`date$m+1)-f)&(`dd$x)-1)"
    "+(`timestamp$x)-`timestamp$`date$x}"
)


# The z-th part of x split on y (on each character when y is ''), from 1 or,
# when negative, from the end; '' past either end.
SPLIT = 'p:$[count y;y vs x;enlist each x];i:$[z<0;z+count p;z-1];$[i within 0,-1+count p;p i;""]'
STRFTIME = (
    'c:"YmdHMSj"!({{string`year$x}};{{-2#"0",string`mm$x}};{{-2#"0",string`dd$x}};'
    '{{-2#"0",string`hh$x}};{{-2#"0",string`uu$x}};{{-2#"0",string`ss$x}};'
    '{{-3#"00",string 1+(`date$x)-`date$`month$12*(`year$x)-2000}});p:"%" vs y;'
    '{result}{{[c;p;x] $[null x;"";(first p),raze {{[c;x;s] (c[first s] x),1_s}}[c;x] each 1_p]}}'
    "[c;p] each `timestamp$x"
)


def strftime_codes(fmt) -> str | None:
    """Why the format has no q form, or None: %Y %m %d %H %M %S %j only."""
    if not (isinstance(fmt, exp.Literal) and fmt.is_string):
        return "STRFTIME is translated with a literal format"
    parts = fmt.this.split("%")[1:]
    if any(not p or p[0] not in "YmdHMSj" for p in parts):
        return "STRFTIME is translated for %Y, %m, %d, %H, %M, %S and %j"
    return None


@dataclass(frozen=True)
class Call:
    kind: type
    symbol: str
    text: str | None = None  # None: the same lambda in both modes
    keys: tuple[str, ...] = ("this",)
    chars: tuple[str, ...] = ()  # keys written as q strings
    fixed: dict = field(default_factory=dict)  # args the node must have
    aggregate: bool = False
    check: object = None  # node -> why it has no q form, or None
    ignored: tuple[str, ...] = ()  # args that say what the lambda does already

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
        # sqlglot 30.26 marks DuckDB's DATE_DIFF as counting boundaries.
        ignored=("date_part_boundary",),
    ),
    Call(
        exp.DateDiff,
        "{(`year$x)-`year$y}",
        keys=("this", "expression"),
        fixed={"unit": "YEAR"},
        # sqlglot 30.26 marks DuckDB's DATE_DIFF as counting boundaries.
        ignored=("date_part_boundary",),
    ),
    # TRIM(s, 'ab'), LTRIM, RTRIM: any of the characters, off either end.
    *(
        Call(
            exp.Trim,
            f"{{`${{{body}}}[;y] each string x}}",
            f"{{{{{body}}}[;y] each x}}",
            keys=("this", "expression"),
            chars=("expression",),
            fixed={"position": position},
        )
        for position, body in (
            ("LEADING", "(sum mins x in y)_x"),
            ("TRAILING", "neg[sum mins reverse x in y]_x"),
            ("BOTH", "a:(sum mins x in y)_x;neg[sum mins reverse a in y]_a"),
            (None, "a:(sum mins x in y)_x;neg[sum mins reverse a in y]_a"),
        )
    ),
    # SPLIT_PART: the part from 1, or from the end when negative; '' past it.
    Call(
        exp.SplitPart,
        "{`${" + SPLIT + "}[;y;z] each string x}",
        "{{" + SPLIT + "}[;y;z] each x}",
        keys=("this", "delimiter", "part_index"),
        chars=("delimiter",),
    ),
    # ASCII: the first character's code, 0 for ''.
    Call(
        exp.Ascii,
        "{?[null x;0N;{$[count x;`long$first x;0]} each string x]}",
        "{{$[count x;`long$first x;0N]} each x}",
    ),
    # STRFTIME: the format read at run time, each %X replaced by its part.
    Call(
        exp.TimeToStr,
        "{" + STRFTIME.format(result="`$") + "}",
        "{" + STRFTIME.format(result="") + "}",
        keys=("this", "format"),
        chars=("format",),
        check=lambda node: strftime_codes(node.args["format"]),
    ),
    # CHR: the character of a code point.
    Call(
        exp.Chr,
        '{`${$[null x;"";enlist "c"$x]} each x}',
        '{{$[null x;"";enlist "c"$x]} each x}',
        keys=("expressions",),
    ),
    # DATE_DIFF in hours, minutes and seconds: the boundaries crossed, each
    # side truncated first; and in quarters, of the months' quarters.
    *(
        Call(
            exp.DateDiff,
            f"{{((`long$({span} xbar `timestamp$x)-{span} xbar `timestamp$y)) div {ns}}}",
            keys=("this", "expression"),
            fixed={"unit": unit},
            ignored=("date_part_boundary",),
        )
        for unit, span, ns in (
            ("HOUR", "0D01:00:00", 3_600_000_000_000),
            ("MINUTE", "0D00:01:00", 60_000_000_000),
            ("SECOND", "0D00:00:01", 1_000_000_000),
        )
    ),
    Call(
        exp.DateDiff,
        "{((`int$`month$x) div 3)-(`int$`month$y) div 3}",
        keys=("this", "expression"),
        fixed={"unit": "QUARTER"},
        ignored=("date_part_boundary",),
    ),
    # EPOCH: seconds since 1970, a float as SQL's; whole seconds and the rest
    # apart, as 1.7e18 nanoseconds are past a float's exact integers.
    Call(
        exp.TimeToUnix,
        "{n:(`long$`timestamp$x)-`long$1970.01.01D0;(n div 1000000000)+(n mod 1000000000)%1e9}",
    ),
    # Math. ATAN2(y, x) by the half-angle form; 0 at the origin, as SQL.
    Call(exp.Atan2, "{?[(x=0)&y=0;0f;2*atan x%(sqrt (x*x)+y*y)+y]}", keys=("this", "expression")),
    Call(exp.Degrees, "{x*180%3.141592653589793}"),
    Call(exp.Radians, "{x*3.141592653589793%180}"),
    Call(exp.Cbrt, "{(signum x)*xexp[abs x;1%3]}"),
    Call(exp.Factorial, "{{$[null x;0N;prd 1+til x]} each x}"),
    Call(exp.IsInf, "{x in -0w 0w}"),
    # Bits, through q's 64-bit boolean vectors: 0b vs x and 2 sv.
    *(
        Call(
            kind,
            f"{{{{$[null[x]|null y;0N;2 sv (0b vs x){glyph}0b vs y]}}'[x;y]}}",
            keys=("this", "expression"),
        )
        for kind, glyph in ((exp.BitwiseAnd, "&"), (exp.BitwiseOr, "|"), (exp.BitwiseXor, "<>"))
    ),
    Call(
        exp.BitwiseLeftShift,
        "{{$[null[x]|null y;0N;x*`long$2 xexp y]}'[x;y]}",
        keys=("this", "expression"),
    ),
    Call(
        exp.BitwiseRightShift,
        "{{$[null[x]|null y;0N;x div `long$2 xexp y]}'[x;y]}",
        keys=("this", "expression"),
    ),
    Call(exp.BitwiseNot, "{{$[null x;0N;2 sv not 0b vs x]} each x}"),
    # Text: MD5's hex digest; TRANSLATE drops what has no counterpart.
    Call(exp.MD5, "{`${raze string md5 x} each string x}", "{{raze string md5 x} each x}"),
    Call(
        exp.Translate,
        '{`${k:y?x;h:k<count y;r:@[x;where h;:;((count y)#z,(count y)#" ") k where h];'
        "r where not h&k>=count z}[;y;z] each string x}",
        '{{k:y?x;h:k<count y;r:@[x;where h;:;((count y)#z,(count y)#" ") k where h];'
        "r where not h&k>=count z}[;y;z] each x}",
        keys=("this", "from_", "to"),
        chars=("from_", "to"),
    ),
    # Aggregates of moments and bits; sample skewness and excess kurtosis.
    Call(
        exp.Skewness,
        "{v:x where not null x;n:count v;m:avg v;s:sdev v;"
        "$[n<3;0n;(n%(n-1)*n-2)*(sum xexp[v-m;3])%s xexp 3]}",
        aggregate=True,
    ),
    Call(
        exp.Kurtosis,
        "{v:x where not null x;n:count v;d:v-avg v;"
        "$[n<4;0n;(((n+1)*(-3+(avg d*d*d*d)%xexp[avg d*d;2]))+6)*(n-1)%(n-2)*n-3]}",
        aggregate=True,
    ),
    *(
        Call(
            kind,
            f"{{$[count w:x where not null x;2 sv ({glyph}/) 0b vs' w;x 0N]}}",
            aggregate=True,
        )
        for kind, glyph in (
            (exp.BitwiseAndAgg, "&"),
            (exp.BitwiseOrAgg, "|"),
            (exp.BitwiseXorAgg, "<>"),
        )
    ),
    # QUANTILE_DISC: the first value whose share of the rows reaches y.
    Call(
        exp.PercentileDisc,
        "{v:asc x where not null x;$[count v;v 0|-1+ceiling y*count v;x 0N]}",
        keys=("this", "expression"),
        aggregate=True,
    ),
    # APPROX_COUNT_DISTINCT, counted exactly.
    Call(exp.ApproxDistinct, "{count distinct x where not null x}", aggregate=True),
    # Dates: ISO weekday, the month's name, a timestamp of its parts, and
    # text parsed in the ISO forms q reads ("P"$).
    Call(exp.DayOfWeekIso, "{1+(5+`int$`date$x) mod 7}"),
    Call(
        exp.Monthname,
        "{`" + "`".join(MONTHS) + " -1+`mm$x}",
        "{(" + ";".join(f'"{m}"' for m in MONTHS) + ") -1+`mm$x}",
    ),
    Call(
        exp.TimestampFromParts,
        "{[y;m;d;h;i;s](`timestamp$-1+d+`date$2000.01m+(12*y-2000)+m-1)"
        "+`timespan$`long$1e9*s+(60*i)+3600*h}",
        keys=("year", "month", "day", "hour", "min", "sec"),
    ),
    Call(
        exp.StrToTime,
        '{[x;f]"P"$string x}',
        '{[x;f]"P"$x}',
        keys=("this", "format"),
        chars=("format",),
        check=lambda node: iso_format(node.args["format"]),
    ),
    # DATE_DIFF in weeks: the days apart, truncated (not week boundaries);
    # in milliseconds and microseconds, the boundaries crossed.
    Call(
        exp.DateDiff,
        "{d:(`date$x)-`date$y;(signum d)*(abs d) div 7}",
        keys=("this", "expression"),
        fixed={"unit": "WEEK"},
        ignored=("date_part_boundary",),
    ),
    *(
        Call(
            exp.DateDiff,
            f"{{((`long$({span} xbar `timestamp$x)-{span} xbar `timestamp$y)) div {ns}}}",
            keys=("this", "expression"),
            fixed={"unit": unit},
            ignored=("date_part_boundary",),
        )
        for unit, span, ns in (
            ("MILLISECOND", "0D00:00:00.001", 1_000_000),
            ("MICROSECOND", "0D00:00:00.000001", 1_000),
        )
    ),
    # More functions: the ISO week (as WEEK), a time of its parts, the first
    # character's code (-1 for ''), the edit distance to a literal, the bits
    # of text, text split into a list, TRUNC to a number of places.
    Call(
        exp.WeekOfYear,
        "{t:3+`week$x;?[null x;0Ni;1+(t-`date$`month$12*(`year$t)-2000) div 7]}",
    ),
    Call(
        exp.TimeFromParts, "{[h;m;s]`time$`long$1000*s+(60*m)+3600*h}", keys=("hour", "min", "sec")
    ),
    Call(
        exp.Unicode,
        "{?[null x;0N;{$[count x;`long$first x;-1]} each string x]}",
        "{{$[count x;`long$first x;0N]} each x}",
    ),
    Call(
        exp.Levenshtein,
        "{" + LEV + "[;y] each string x}",
        "{" + LEV + "[;y] each x}",
        keys=("this", "expression"),
        chars=("expression",),
    ),
    Call(
        exp.BitLength,
        "{?[null x;0N;8*count each string x]}",
        "{{$[count x;8*count x;0N]} each x}",
    ),
    Call(
        exp.Split,
        "{{`$y vs x}[;y] each string x}",
        "{{y vs x}[;y] each x}",
        keys=("this", "expression"),
        chars=("expression",),
    ),
    Call(exp.Trunc, "{(signum x)*(floor abs[x]*10 xexp y)%10 xexp y}", keys=("this", "decimals")),
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
        if key == "unit":
            have = unit_of(node)
        elif isinstance(value, bool):
            have = bool(node.args.get(key))
        else:
            # A word (TRIM's position), or None: the argument absent.
            have = node.args.get(key)
            have = str(have).upper() if have else None
        if have != value:
            return False
    if not all(node.args.get(k) is not None for k in call.keys):
        return False
    # An argument the lambda does not take (TRUNC(x, 1), STRPOS from a
    # position) would be dropped: no match, and so refused.
    return not any(
        value not in (None, False)
        for key, value in node.args.items()
        if key not in call.keys and key not in call.fixed and key not in call.ignored
    )


def find(node: exp.Expr) -> Call | None:
    for call in CALLS:
        if isinstance(node, call.kind) and matches(call, node):
            return call
    return None


def write(gen, node: exp.Expr, call: Call) -> str:
    if call.check is not None and (why := call.check(node)):
        return gen.refuse(why)
    fn = call.symbol if gen.symbols or call.text is None else call.text
    args = []
    for key in call.keys:
        arg = node.args[key]
        if isinstance(arg, list):
            # CHR(n): one argument, held as a list.
            if len(arg) != 1:
                return gen.refuse(f"{node.key.upper()} is translated with one argument")
            arg = arg[0]
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
    if "expressions" in values:
        values["expressions"] = [values["expressions"]]
    for key, value in call.fixed.items():
        if value is not None:
            values[key] = exp.var(value) if key == "unit" else value
    return call.kind(**values)
