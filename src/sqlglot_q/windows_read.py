"""Reading windows.py's update stages back as SQL window functions.

    update s:sums x by g from `o xasc t

is SELECT *, SUM(x) OVER (PARTITION BY g ORDER BY o ROWS UNBOUNDED PRECEDING)
AS s FROM t. A stage over another stage adds its window to the same select,
and `update s:last s by g,o` over a running stage turns its frame into
RANGE, SQL's default, where tied rows share a value. Anything else an update
of a table value can do has no SQL form, and is refused.
"""

from __future__ import annotations

from sqlglot import exp

AGGREGATE = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count)
RUNNING = {"sums": exp.Sum, "avgs": exp.Avg, "maxs": exp.Max, "mins": exp.Min}
MOVING = {"msum": exp.Sum, "mavg": exp.Avg, "mmax": exp.Max, "mmin": exp.Min}


def window_stage(parser, items, keys, source) -> exp.Expr:
    from sqlglot_q.parser import QCall, Sorted, is_query, non_null

    if (
        not keys
        and items
        and all(isinstance(i, exp.Alias) and isinstance(i.this, exp.Column) for i in items)
    ):
        # update a:k from u: u with a copy of k named a, the generator's
        # right side of a join ON t.a = u.k.
        from sqlglot_q.parser import Renamed

        return Renamed(this=source, pairs=[(i.alias, i.this.name) for i in items])
    if (
        not keys
        and items
        and all(isinstance(i, exp.Alias) for i in items)
        and all(function(parser, i.this, None, non_null, QCall) is None for i in items)
        and not any(i.this.find(QCall) for i in items)
    ):
        # update a:e from t: SELECT *, e AS a, the generator's columns beside *.
        return star_select(parser, [exp.Star(), *items], source)
    if len(items) != 1 or not isinstance(items[0], exp.Alias):
        parser.fail("update of a table value is read as one window, name:value")
    name, value = items[0].alias, items[0].this
    parts = []
    for key in keys:
        if not isinstance(key, exp.Column):
            parser.fail("a window's by phrase is read as columns")
        parts.append(key)

    # The sort ahead of the stage is the window's ORDER BY.
    order = None
    if isinstance(source, Sorted):
        order, source = source.args["order"], source.this
    elif is_query(source) and source.args.get("order") and not source.args.get("limit"):
        order = source.args["order"]
        source = source.copy()
        source.set("order", None)

    # update s:last s by g,o over a running stage: SQL's RANGE frame.
    # (last of another column is LAST_VALUE over the whole partition.)
    tie_fix = (
        isinstance(value, QCall)
        and value.args["fn"] == "last"
        and isinstance(value.this, exp.Column)
        and value.this.name == name
        and windowed_item(source, name) is not None
    )
    if tie_fix:
        target = windowed_item(source, name)
        wins = list(target.this.find_all(exp.Window))
        peers = [c.name for c in parts]
        for win in wins:
            order_by = win.args.get("order")
            expected = [c.name for c in win.args.get("partition_by") or []] + [
                o.this.name for o in (order_by.expressions if order_by else [])
            ]
            if order_by is None or peers != expected:
                parser.fail("last ... by is read with the window's partition and order keys")
            if isinstance(win.this, exp.RowNumber):
                win.set("this", exp.Count(this=exp.Star()))
            win.set("spec", None)
        return source

    def window(found) -> exp.Window:
        fn, spec, ordered = found
        win = exp.Window(
            this=fn, partition_by=[p.copy() for p in parts] or None, spec=spec, over="OVER"
        )
        if ordered:
            if order is None:
                parser.fail("a running q function is read with the sort ahead of it")
            win.set("order", order.copy())
        return win

    found = function(parser, value, order, non_null, QCall)
    if found is not None:
        alias = exp.alias_(window(found), name)
    else:
        # (sums px*qty)%sums qty: each q window function in the expression is
        # a window over the same partition and order.
        hits = []

        def convert(node):
            match = function(parser, node, order, non_null, QCall)
            if match is not None:
                hits.append(match)
                return window(match)
            for key, child in list(node.args.items()):
                if isinstance(child, exp.Expr):
                    node.set(key, convert(child))
            return node

        body = convert(value.copy())
        if not hits:
            parser.fail("update of a table value is read only as window functions")
        alias = exp.alias_(body, name)

    if windowed(source):
        source.expressions.append(alias)
        return source
    return star_select(parser, [exp.Star(), alias], source)


def star_select(parser, expressions, source) -> exp.Expr:
    from sqlglot_q.parser import is_query

    node = exp.Select(expressions=expressions)
    if is_query(source):
        inner = source
        plain = (
            isinstance(inner, exp.Select)
            and len(inner.expressions) == 1
            and isinstance(inner.expressions[0], exp.Star)
            and not any(inner.args.get(k) for k in ("joins", "group", "order", "limit", "distinct"))
        )
        if plain:
            # update ... from (select from t where c): SQL's WHERE runs
            # before the windows anyway.
            node.set("from_", inner.args["from_"])
            if inner.args.get("where"):
                node.set("where", inner.args["where"])
        else:
            node.set("from_", exp.From(this=exp.Subquery(this=inner)))
    else:
        from sqlglot_q.parser import as_table

        node.set("from_", exp.From(this=as_table(parser, source)))
    node.meta["q_windows"] = True
    return node


def windowed(node) -> bool:
    return isinstance(node, exp.Select) and node.meta.get("q_windows", False)


def windowed_item(node, name):
    if not windowed(node):
        return None
    for item in node.expressions:
        if isinstance(item, exp.Alias) and item.alias == name and item.this.find(exp.Window):
            return item
    return None


ROWS = dict(kind="ROWS", start="UNBOUNDED", start_side="PRECEDING", end="CURRENT ROW")


def function(parser, value, order, non_null, QCall):
    """(the SQL window function, its frame, whether it takes ORDER BY)."""

    def call(node, fn):
        return isinstance(node, QCall) and node.args["fn"] == fn

    if isinstance(value, AGGREGATE):
        return value, None, False
    if (
        isinstance(value, exp.Add)
        and isinstance(value.this, exp.Literal)
        and value.this.name == "1"
    ):
        right = value.expression
        if call(right, "til") and isinstance(right.this, exp.Count):
            return exp.RowNumber(), None, order is not None
        if call(right, "find"):
            return exp.Rank(), None, True
    if isinstance(value, QCall) and value.args["fn"] in ("lagd", "leadd", "lag0"):
        n = value.args.get("n") or exp.Literal.number(1)
        default = value.args.get("default") or exp.Literal.number(0)
        kind = exp.Lead if value.args["fn"] == "leadd" else exp.Lag
        return kind(this=value.this, offset=n, default=default), None, True
    if isinstance(value, QCall) and value.args["fn"] == "first":
        return exp.FirstValue(this=value.this), None, True
    if (
        isinstance(value, QCall)
        and value.args["fn"] == "last"
        and isinstance(value.this, exp.Column)
    ):
        whole = exp.WindowSpec(
            kind="ROWS",
            start="UNBOUNDED",
            start_side="PRECEDING",
            end="UNBOUNDED",
            end_side="FOLLOWING",
        )
        return exp.LastValue(this=value.this), whole, True
    if isinstance(value, QCall) and value.args["fn"] == "ntile":
        return exp.Ntile(this=value.args["n"]), None, True
    if isinstance(value, QCall) and value.args["fn"] == "percent_rank":
        return exp.PercentRank(), None, True
    if isinstance(value, QCall) and value.args["fn"] == "cume_dist":
        return exp.CumeDist(), exp.WindowSpec(**ROWS), True
    if isinstance(value, QCall):
        fn = value.args["fn"]
        if fn == "sums" and call(value.this, "differ"):
            return exp.DenseRank(), None, True
        if fn in RUNNING:
            arg = value.this
            if fn == "sums" and isinstance(arg, exp.Not) and isinstance(arg.this, exp.Is):
                return exp.Count(this=arg.this.this), exp.WindowSpec(**ROWS), True
            return RUNNING[fn](this=arg), exp.WindowSpec(**ROWS), True
        if fn == "xprev":
            n = value.args["n"]
            count = int(n.name) if isinstance(n, exp.Literal) else None
            if isinstance(n, exp.Neg) and isinstance(n.this, exp.Literal):
                count = -int(n.this.name)
            if count is None or count == 0:
                parser.fail("xprev is read with a literal count")
            kind = exp.Lag if count > 0 else exp.Lead
            return kind(this=value.this, offset=exp.Literal.number(abs(count))), None, True
        if fn in MOVING or fn == "mcount":
            n = value.args["n"]
            if not isinstance(n, exp.Literal) or int(n.name) < 1:
                parser.fail(f"{fn} is read with a literal width")
            spec = exp.WindowSpec(
                kind="ROWS",
                start=exp.Literal.number(int(n.name) - 1),
                start_side="PRECEDING",
                end="CURRENT ROW",
            )
            if fn == "mcount":
                arg = value.this
                star = isinstance(arg, exp.Column) and arg.name == "i"
                return exp.Count(this=exp.Star() if star else arg), spec, True
            return MOVING[fn](this=value.this), spec, True
    return None


def fold(node: exp.Select) -> exp.Select:
    """SELECT g, s FROM (SELECT *, SUM(x) OVER (...) AS s FROM t) as
    SELECT g, SUM(x) OVER (...) AS s FROM t. Only when the outer select does
    nothing but project: SQL runs WHERE, GROUP BY and DISTINCT before the
    windows, so folding any of them in would move them."""
    source = node.args["from_"].this
    inner = source.this if isinstance(source, exp.Subquery) else None
    if not windowed(inner):
        return node
    if any(node.args.get(k) for k in ("joins", "where", "group", "having", "distinct")):
        return node
    windows = {i.alias: i.this for i in inner.expressions if isinstance(i, exp.Alias)}
    if any(isinstance(i, exp.Star) for i in node.expressions):
        return node

    def swap(n: exp.Expr) -> exp.Expr:
        if isinstance(n, exp.Column) and n.name in windows:
            return windows[n.name].copy()
        return n

    items = []
    for item in node.expressions:
        if isinstance(item, exp.Column) and item.name in windows:
            items.append(exp.alias_(windows[item.name].copy(), item.name))
        else:
            items.append(item.transform(swap))
    folded = inner.copy()
    folded.set("expressions", items)
    folded.meta["q_windows"] = False
    for key in ("order", "limit", "offset"):
        if node.args.get(key):
            folded.set(key, node.args[key])
    return folded
