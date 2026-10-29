"""Window functions, as a chain of q updates.

SQL computes a select's window functions after WHERE and before the
projection, each over its partition in its own order. q does the same with
update ... by, one stage per window:

    select a, s from (update s:sums x by g from `o xasc (select from t where c))

- PARTITION BY is the by phrase, and ORDER BY a stable sort of the source,
  so each group's rows reach the update in order.
- A whole-partition aggregate is the aggregate (update broadcasts it); a
  running one is sums/avgs/maxs/mins; a sliding one msum/mavg/mmax/mmin.
- SQL's default frame with ORDER BY is RANGE: rows tied on the order key are
  peers and share the value at the last of them. q's running functions are
  ROWS, so a second stage gives each run of peers its last value:
  update s:last s by g,o from ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

if TYPE_CHECKING:
    from sqlglot_q.generator import QGenerator

RUNNING = {exp.Sum: "sums", exp.Avg: "avgs", exp.Max: "maxs", exp.Min: "mins"}
MOVING = {exp.Sum: "msum", exp.Avg: "mavg", exp.Max: "mmax", exp.Min: "mmin"}
WHOLE = {exp.Sum: "sum", exp.Avg: "avg", exp.Max: "max", exp.Min: "min"}


class Refused(Exception):
    pass


def has_window(items: list[exp.Expr]) -> bool:
    return any(i.find(exp.Window) for i in items)


def chain(gen: QGenerator, items: list[exp.Expr], source: str) -> tuple[list[exp.Expr], str] | None:
    """The select's items with each window replaced by the column a stage
    computes, and the q of the stages over source. None when refused.

    An item whose windows share one partition and order is one stage, its
    whole expression computed per group: SUM(px*qty) OVER w / SUM(qty) OVER w
    is update vwap:(sums px*qty)%sums qty by ... Windows that differ get a
    stage each, w0, w1, and the item refers to them."""
    from sqlglot_q.generator import QText

    items = [i.copy() for i in items]
    text = source
    made = 0
    try:
        for n, item in enumerate(items):
            wins = list(item.find_all(exp.Window))
            if not wins:
                continue
            for win in wins:
                if win.find_ancestor(exp.Window) is not None:
                    raise Refused("a window inside a window has no q form")
            body = item.this if isinstance(item, exp.Alias) else item
            shared = [plan(gen, w) for w in wins]
            same = all(p[:2] == shared[0][:2] for p in shared)
            mixed = any(p[3] for p in shared) and any(p[4] == "rows" for p in shared)
            if same and not mixed:
                name = item.alias if isinstance(item, exp.Alias) else f"w{made}"
                made += 0 if isinstance(item, exp.Alias) else 1
                # Held under a parent, so a window that is the whole body can
                # be replaced too.
                holder = exp.Paren(this=body.copy())
                for win, p in zip(list(holder.find_all(exp.Window)), shared, strict=True):
                    win.replace(QText(this=p[2]))
                value = gen.sql(holder.this)
                if isinstance(holder.this, QText):
                    value = holder.this.this
                text = stage(gen, shared[0], value, any(p[3] for p in shared), name, text)
                items[n] = exp.column(name)
                continue
            for win, p in zip(list(item.find_all(exp.Window)), shared, strict=True):
                name = f"w{made}"
                made += 1
                text = stage(gen, p, p[2], p[3], name, text)
                if win is item.this and isinstance(item, exp.Alias):
                    items[n] = exp.column(name)
                else:
                    win.replace(exp.column(name))
    except Refused as e:
        gen.refuse(str(e))
        return None
    return items, text


def keys(gen: QGenerator, nodes: list[exp.Expr], what: str) -> list[str]:
    out = []
    for node in nodes:
        if not isinstance(node, exp.Column):
            raise Refused(f"{what} is translated for columns only")
        out.append(gen.name(node.name))
    return out


def plan(gen: QGenerator, win: exp.Window):
    """(by keys, sort terms, q value, whether peers share, frame) for one
    window."""
    if win.args.get("alias") or win.args.get("first"):
        raise Refused("a named window or a window with FIRST has no q form")
    parts = keys(gen, win.args.get("partition_by") or [], "PARTITION BY")
    order = win.args.get("order")
    from sqlglot_q.query import null_fix

    orders = order.expressions if order else []
    order_cols = keys(gen, [o.this for o in orders], "ORDER BY in a window")
    terms = [
        (c, bool(o.args.get("desc")), null_fix(o)) for c, o in zip(order_cols, orders, strict=True)
    ]
    value, tie_fix, needs_order = function(gen, win, order_cols, bool(terms))
    if needs_order and not terms:
        raise Refused(f"{win.this.sql()} OVER needs an ORDER BY to have a q form")
    frame = frame_of(win.args.get("spec"), bool(terms))
    return tuple(parts), tuple(terms) if needs_order else (), value, tie_fix, frame


def stage(gen: QGenerator, plan, value: str, tie_fix: bool, name: str, source: str) -> str:
    from sqlglot_q.query import sort, wrap

    parts, terms = plan[0], plan[1]
    if not gen.symbols and (plan[4] == "whole" or value.startswith(("first ", "last "))):
        from sqlglot_q.generator import TEXT_SPREAD

        value = f"{TEXT_SPREAD}[i;{value}]"  # one string per partition
    src = sort(list(terms), wrap(source), gen) if terms else source
    by = f" by {','.join(parts)}" if parts else ""
    text = f"update {name}:{value}{by} from {wrap(src)}"
    if tie_fix:
        peers = ",".join([*parts, *[t[0] for t in terms]])
        text = f"update {name}:last {name} by {peers} from ({text})"
    return text


def function(gen: QGenerator, win: exp.Window, order: list[str], has_order: bool):
    """(q for the window's values in one partition, whether peers then share
    the last value, whether the rows must be in the window's order)."""
    fn = win.this
    spec = win.args.get("spec")
    frame = frame_of(spec, has_order)
    from sqlglot_q.generator import CUME_DIST, NTILE, PERCENT_RANK

    if isinstance(fn, exp.RowNumber):
        return "1+til count i", False, has_order
    if isinstance(fn, exp.FirstValue):
        if frame not in ("whole", "range", "rows"):
            raise Refused("FIRST_VALUE is translated from the partition's first row")
        return f"first {gen.operand(fn.this)}", False, has_order
    if isinstance(fn, exp.LastValue):
        if frame != "whole":
            raise Refused(
                "LAST_VALUE is translated over the whole partition (ROWS BETWEEN UNBOUNDED "
                "PRECEDING AND UNBOUNDED FOLLOWING); with the default frame it is the row itself"
            )
        return f"last {gen.operand(fn.this)}", False, has_order
    if isinstance(fn, exp.Ntile):
        if not order:
            raise Refused("NTILE needs an ORDER BY")
        return f"{NTILE}[{gen.sql(fn.this)};i]", False, True
    if isinstance(fn, (exp.PercentRank, exp.CumeDist)):
        if not order:
            raise Refused(f"{type(fn).__name__} needs an ORDER BY")
        key = order[0] if len(order) == 1 else f"flip({';'.join(order)})"
        if isinstance(fn, exp.PercentRank):
            return f"{PERCENT_RANK} {key}", False, True
        # The share of rows at or before this one, peers included.
        return f"{CUME_DIST} i", True, True
    if isinstance(fn, (exp.Rank, exp.DenseRank)):
        if not order:
            raise Refused("RANK needs an ORDER BY")
        key = order[0] if len(order) == 1 else f"flip({';'.join(order)})"
        if isinstance(fn, exp.Rank):
            return f"1+{key}?{key}", False, True
        return f"sums differ {key}", False, True
    if isinstance(fn, (exp.Lag, exp.Lead)):
        offset = fn.args.get("offset")
        n = int(offset.name) if isinstance(offset, exp.Literal) else 1 if offset is None else None
        if n is None:
            raise Refused("LAG/LEAD is translated with a literal offset")
        default = fn.args.get("default")
        if default is not None:
            # The default fills only past the partition's edge, not x's own
            # nulls, so not ^.
            from sqlglot_q.generator import LAG_DEFAULT, LEAD_DEFAULT

            fill = LAG_DEFAULT if isinstance(fn, exp.Lag) else LEAD_DEFAULT
            return f"{fill}[{gen.sql(fn.this)};{n};{gen.sql(default)}]", False, True
        n = n if isinstance(fn, exp.Lag) else -n
        return f"{n} xprev {gen.sql(fn.this)}", False, True
    if isinstance(fn, exp.Count):
        arg = fn.this
        if isinstance(arg, exp.Distinct):
            raise Refused("COUNT(DISTINCT ...) OVER has no q form here")
        star = arg is None or isinstance(arg, exp.Star)
        col = None if star else gen.operand(arg)
        if frame == "whole":
            return ("count i" if star else f"count {col} where not null {col}"), False, False
        if frame in ("range", "rows"):
            run = "1+til count i" if star else f"sums not null {col}"
            return run, frame == "range", True
        if isinstance(frame, tuple):
            from sqlglot_q.generator import FRAMES

            word = FRAMES["count_star" if star else "count"]
            return f"{word}[{frame[1]};{frame[2]};{'i' if star else col}]", False, True
        width = frame
        return f"{width} mcount {'i' if star else col}", False, True
    if type(fn) in WHOLE:
        if fn.args.get("distinct") or isinstance(fn.this, exp.Distinct):
            raise Refused("DISTINCT inside a window aggregate has no q form here")
        arg = gen.sql(fn.this)
        if frame == "whole":
            from sqlglot_q.generator import NULL_SAFE

            word = NULL_SAFE.get(type(fn), WHOLE[type(fn)])
            return f"{word} {arg}", False, False
        if frame in ("range", "rows"):
            from sqlglot_q.generator import RUNNING_MAX, RUNNING_MIN, RUNNING_SUM

            # sums of a leading run of nulls is 0; SQL's running SUM is NULL.
            word = {exp.Sum: RUNNING_SUM, exp.Max: RUNNING_MAX, exp.Min: RUNNING_MIN}.get(
                type(fn), RUNNING[type(fn)]
            )
            return f"{word} {arg}", frame == "range", True
        if isinstance(frame, tuple):
            from sqlglot_q.generator import FRAMES

            word = FRAMES[WHOLE[type(fn)]]
            return f"{word}[{frame[1]};{frame[2]};{arg}]", False, True
        from sqlglot_q.generator import MOVING_NULLS

        word = MOVING[type(fn)]
        if word in MOVING_NULLS:
            return f"{MOVING_NULLS[word]}[{frame};{arg}]", False, True
        return f"{frame} {word} {arg}", False, True
    raise Refused(f"{type(fn).__name__} OVER has no q translation")


def frame_of(spec: exp.WindowSpec | None, has_order: bool):
    """'whole', 'range' (running, peers share), 'rows' (running), or the
    width of a sliding frame ending at the current row."""
    if spec is None:
        return "range" if has_order else "whole"
    kind = (spec.args.get("kind") or "").upper()
    start, start_side = spec.args.get("start"), (spec.args.get("start_side") or "").upper()
    end, end_side = spec.args.get("end"), (spec.args.get("end_side") or "").upper()
    if start == "UNBOUNDED" and end == "UNBOUNDED" and end_side == "FOLLOWING":
        return "whole"
    if end is None and start_side == "PRECEDING":
        end = "CURRENT ROW"  # ROWS UNBOUNDED PRECEDING, ROWS 2 PRECEDING
    if end != "CURRENT ROW":
        if kind != "ROWS":
            raise Refused("a RANGE frame is translated when it ends at the current row")
        # A frame reaching ahead, or ending before the current row: each
        # row's slice, a and b rows off it.
        return ("frame", offset(start, start_side), offset(end, end_side))
    if start == "UNBOUNDED" and start_side == "PRECEDING":
        if not has_order:
            return "whole"
        return "range" if kind == "RANGE" else "rows"
    if kind == "ROWS" and isinstance(start, exp.Literal) and start_side == "PRECEDING":
        return int(start.name) + 1
    if kind == "ROWS" and (start == "CURRENT ROW" or start_side == "FOLLOWING"):
        return ("frame", offset(start, start_side), 0)
    raise Refused("a window frame is translated as UNBOUNDED or n PRECEDING to the current row")


def offset(bound, side: str) -> str:
    """A frame bound as rows off the current one, in q: 0N for UNBOUNDED."""
    if bound == "UNBOUNDED":
        return "0N"
    if bound == "CURRENT ROW":
        return "0"
    if not (isinstance(bound, exp.Literal) and bound.name.isdigit()):
        raise Refused("a window frame is translated with literal bounds")
    return f"-{bound.name}" if side == "PRECEDING" else bound.name
