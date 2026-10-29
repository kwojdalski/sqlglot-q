"""SELECT, which in q is several expressions composed rather than one.

A SQL query evaluates its clauses in a fixed order (FROM, WHERE, GROUP BY,
HAVING, SELECT, DISTINCT, ORDER BY, LIMIT), and so does the q this builds,
from the inside out:

    (offset;limit) sublist `k xasc distinct
        select ... from (0!select ... by ... from src where ...) where having

Each layer is emitted only when the query needs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from sqlglot_q.generator import NAME, RESERVED, SYMBOL
from sqlglot_q.windows import chain, has_window

if TYPE_CHECKING:
    from sqlglot_q.generator import QGenerator


def conjuncts(node: exp.Expr) -> list[exp.Expr]:
    return list(node.flatten()) if isinstance(node, exp.And) else [node]


def unalias(node: exp.Expr) -> exp.Expr:
    return node.this if isinstance(node, exp.Alias) else node


def inline_ctes(node: exp.Select) -> exp.Select:
    """WITH c AS (...) SELECT ... FROM c, with each reference to c replaced by
    its query: q has no statement-local names inside one expression."""
    with_ = node.args.get("with_")
    if not with_:
        return node
    node = node.copy()
    ctes = {}
    for cte in node.args["with_"].expressions:
        query = cte.this
        for name, body in ctes.items():
            query = substitute(query, name, body)
        ctes[cte.alias] = query
    node.set("with_", None)
    for name, body in ctes.items():
        node = substitute(node, name, body)
    return node


def substitute(node: exp.Expr, name: str, body: exp.Expr) -> exp.Expr:
    def swap(n: exp.Expr) -> exp.Expr:
        if isinstance(n, exp.Table) and n.name == name and not n.args.get("db"):
            return exp.Subquery(this=body.copy())
        return n

    return node.transform(swap)


def source(gen: QGenerator, node: exp.Select) -> str:
    from_ = node.args.get("from_")
    left = from_.this
    text = gen.operand_query(left) if isinstance(left, exp.Subquery) else gen.sql(left)
    for join in node.args.get("joins") or []:
        text = joined(gen, text, join)
    return text


def joined(gen: QGenerator, left: str, join: exp.Join) -> str:
    right_node = join.this
    right = (
        gen.operand_query(right_node)
        if isinstance(right_node, exp.Subquery)
        else gen.sql(right_node)
    )
    side = (join.args.get("side") or "").upper()
    kind = (join.args.get("kind") or "").upper()
    plain = not any(join.args.get(k) for k in ("on", "using", "method"))
    if kind == "CROSS" or (plain and not side and kind in ("", "INNER")):
        # FROM t, u is a cross join too.
        return f"{left} cross {right}"
    if (join.args.get("method") or "").upper() == "ASOF":
        return asof(gen, left, right, join)
    pairs = join_keys(gen, join, right_node.alias_or_name)
    if pairs is None:
        return gen.refuse(
            "a join is only translated on equality of columns (ON a.k = b.j, or USING), "
            "with the two sides told apart by their table"
        )
    if side == "RIGHT":
        # A RIGHT JOIN is the LEFT JOIN of the other way round.
        left, right = right, left
        pairs = [(r, lft) for lft, r in pairs]
        side = "LEFT"
    symbols = "".join(f"`{k}" for k, _ in pairs)
    if side or kind not in ("", "INNER"):
        if side != "LEFT":
            return gen.refuse(f"{side or kind} JOIN has no q translation here")
    # A null key matches nothing in SQL; in q 0N=0N, so ej and lj would pair
    # null keys up. A LEFT JOIN's right side loses its null keys (each left
    # row still appears); an inner join's left side does.
    if side == "LEFT":
        right = f"(select from {wrap(right)} where {non_null([j for _, j in pairs])})"
    else:
        left = f"(select from {wrap(left)} where {non_null([k for k, _ in pairs])})"
    renames = [(k, j) for k, j in pairs if k != j]
    if renames:
        # ON t.a = u.b: u gets a copy of b named a to join on, and keeps b.
        sets = ", ".join(f"{k}:{j}" for k, j in renames)
        right = f"(update {sets} from {wrap(right)})"
    if side == "LEFT":
        # lj takes the first match on the right, which is SQL's LEFT JOIN when
        # the right side is unique on the keys.
        return f"{left} lj {symbols} xkey {right}"
    return f"ej[{symbols};{left};{right}]"


def non_null(keys: list[str]) -> str:
    return ", ".join(f"not null {k}" for k in keys)


def asof(gen: QGenerator, left: str, right: str, join: exp.Join) -> str:
    """ASOF LEFT JOIN ... USING (k, time), or ON k = k AND l.time >= r.time:
    for each left row the last right row with its keys at or before its time,
    which is q's aj. aj wants the right table sorted on the keys."""
    if (join.args.get("side") or "").upper() != "LEFT":
        return gen.refuse("only ASOF LEFT JOIN has a q form (aj); ASOF JOIN drops rows aj keeps")
    using = join.args.get("using")
    if using:
        keys = [gen.name(u.name) for u in using]
    else:
        on = join.args.get("on")
        conds = conjuncts(on) if on is not None else []
        *eqs, last = conds or [None]
        same = lambda c, kind: (  # noqa: E731
            isinstance(c, kind)
            and isinstance(c.this, exp.Column)
            and isinstance(c.expression, exp.Column)
            and c.this.name == c.expression.name
            and c.this.table != c.expression.table
        )
        if not (same(last, exp.GTE) and all(same(c, exp.EQ) for c in eqs)):
            return gen.refuse("ASOF JOIN is translated on k = k AND left.time >= right.time")
        keys = [gen.name(c.this.name) for c in [*eqs, last]]
    symbols = "".join(f"`{k}" for k in keys)
    return f"aj[{symbols};{left};{symbols} xasc {right}]"


def join_keys(gen: QGenerator, join: exp.Join, right: str) -> list[tuple[str, str]] | None:
    """(left column, right column) for each key of an equi-join."""
    using = join.args.get("using")
    if using:
        return [(gen.name(u.name), gen.name(u.name)) for u in using]
    on = join.args.get("on")
    if on is None:
        return None
    pairs = []
    for c in conjuncts(on):
        if not (
            isinstance(c, exp.EQ)
            and isinstance(c.this, exp.Column)
            and isinstance(c.expression, exp.Column)
        ):
            return None
        a, b = c.this, c.expression
        if a.table == right and b.table != right:
            a, b = b, a
        elif not (b.table == right or a.name == b.name):
            return None  # unqualified, differently named: which is whose?
        pairs.append((gen.name(a.name), gen.name(b.name)))
    return pairs


def resolve(node: exp.Expr, items: list[exp.Expr]) -> exp.Expr:
    """A GROUP BY or ORDER BY term written as a position or a select alias,
    as the select item it names."""
    if isinstance(node, exp.Literal) and not node.is_string and node.this.isdigit():
        index = int(node.this) - 1
        if 0 <= index < len(items):
            return items[index]
    if isinstance(node, exp.Column) and not node.table:
        for item in items:
            if isinstance(item, exp.Alias) and item.alias == node.name:
                return item
    return node


def select(gen: QGenerator, node: exp.Select, outermost: bool = False) -> str:
    node = inline_ctes(node)
    renamed = None
    if outermost:
        node, renamed = rename_outputs(gen, node)
    text = query(gen, node)
    if renamed:
        # Columns q cannot spell in a select phrase are renamed afterwards:
        # xcol takes any symbol.
        text = f"{symbols(gen, renamed)} xcol {wrap(text)}"
    return text


def rename_outputs(gen: QGenerator, node: exp.Select) -> tuple[exp.Select, list[str] | None]:
    """Give each output alias that is not a q name (orbital's
    `output_probability.0`) a placeholder, returning the real names."""
    items = node.expressions
    aliases = [i.alias if isinstance(i, exp.Alias) else None for i in items]
    if all(a is None or (NAME.match(a) and a not in RESERVED) for a in aliases):
        return node, None
    names = output_names(items)
    if None in names:
        gen.refuse("an output column needs a name before another can be renamed")
        return node, None
    node = node.copy()
    for i, item in enumerate(node.expressions):
        if isinstance(item, exp.Alias) and not (
            NAME.match(item.alias) and item.alias not in RESERVED
        ):
            item.set("alias", exp.to_identifier(f"c{i}"))
    return node, names


def symbols(gen: QGenerator, names: list[str]) -> str:
    if all(SYMBOL.match(n) for n in names):
        return "".join(f"`{n}" for n in names)
    return "`$(" + ";".join(gen.chars(n) for n in names) + ")"


def query(gen: QGenerator, node: exp.Select) -> str:
    items = list(node.expressions)
    if not node.args.get("from_"):
        return no_from(gen, items)
    src = source(gen, node)
    where = gen.where(node.args.get("where"))
    group = node.args.get("group")
    star = len(items) == 1 and isinstance(items[0], exp.Star)

    order = node.args.get("order")
    sort_source: list[tuple[str, bool]] = []
    sort_result: list[tuple[str, bool]] = []

    if group:
        if star:
            return gen.refuse("SELECT * with GROUP BY has no q translation")
        if has_window(items):
            return gen.refuse("a window over a grouped result has no q translation here")
        text, names = grouped(gen, node, items, src, where)
    else:
        if node.args.get("having"):
            return gen.refuse("HAVING without GROUP BY is not translated")
        names = output_names(items)
        if order:
            sort_result, sort_source = order_terms(gen, order, items, names, allow_source=True)
        if sort_source:
            src = f"({sort(sort_source, src, gen)})"
        distinct = " distinct" if node.args.get("distinct") else ""
        excluded = items[0].args.get("except_") if star else None
        if excluded:
            # SELECT * EXCLUDE (a): every column but a.
            dropped = ",".join(gen.name(c.name) for c in excluded)
            inner = f"select{distinct} from {src}{where}"
            text = f"delete {dropped} from {wrap(inner)}"
            if sort_result:
                text = sort(sort_result, text, gen)
            return limited(gen, node, text)
        if has_window(items):
            return windowed(gen, node, items, src, where, sort_result, sort_source, distinct)
        columns = "" if star else " " + ", ".join(projection(gen, items))
        text = f"select{distinct}{columns} from {src}{where}"

    if group:
        if node.args.get("distinct"):
            text = f"distinct {text}"
        if order:
            sort_result, _ = order_terms(gen, order, items, names, allow_source=False)

    if sort_result:
        text = sort(sort_result, text, gen)
    return limited(gen, node, text)


def projection(gen: QGenerator, items: list[exp.Expr]) -> list[str]:
    """The select phrase. SELECT 1 AS one FROM t has a row per row of t, but
    a q select whose columns are all atoms is refused ('rank), so then each
    constant is spread over the rows: q 5 runs {(count x)#y}[i;1] per row."""
    if any(i.find(exp.Column, exp.AggFunc, exp.Star) for i in items):
        return [gen.sql(i) for i in items]
    out = []
    for item in items:
        value = f"{{(count x)#y}}[i;{gen.sql(unalias(item))}]"
        out.append(f"{gen.name(item.alias)}:{value}" if isinstance(item, exp.Alias) else value)
    return out


def windowed(gen, node, items, src, where, sort_result, sort_source, distinct) -> str:
    """A select with window functions: WHERE, then each window as an update
    stage over what it left, then the projection (windows.py)."""
    if sort_source:
        return gen.refuse("ORDER BY a column the windowed result does not include")
    base = f"select from {src}{where}" if where else src
    built = chain(gen, items, base)
    if built is None:
        return "'unsupported"
    items, text = built
    # SELECT *, <window> AS w: the stages are the whole query.
    star_then_windows = isinstance(items[0], exp.Star) and all(
        isinstance(i, exp.Column) and i.name not in (base,) for i in items[1:]
    )
    if not (star_then_windows and not distinct):
        columns = " " + ", ".join(projection(gen, items))
        text = f"select{distinct}{columns} from {wrap(text)}"
    if sort_result:
        text = sort(sort_result, text, gen)
    return limited(gen, node, text)


def no_from(gen: QGenerator, items: list[exp.Expr]) -> str:
    cols = []
    for i, item in enumerate(items):
        name = item.alias if isinstance(item, exp.Alias) else f"x{i}" if i else "x"
        cols.append(f"{gen.name(name)}:enlist {gen.operand(unalias(item))}")
    return f"([] {';'.join(cols)})"


def output_names(items: list[exp.Expr]) -> list[str | None]:
    names: list[str | None] = []
    for item in items:
        if isinstance(item, exp.Alias):
            names.append(item.alias)
        elif isinstance(item, exp.Column):
            names.append(item.name)
        else:
            names.append(None)
    return names


def grouped(
    gen: QGenerator, node: exp.Select, items: list[exp.Expr], src: str, where: str
) -> tuple[str, list[str | None]]:
    keys = [resolve(k, items) for k in node.args["group"].expressions]
    key_exprs = [unalias(k) for k in keys]

    # Name each by key: a column keeps its name, an expression takes the
    # alias it has in the select list, or a made-up one.
    key_names = []
    for key, expr in zip(keys, key_exprs, strict=True):
        # The name the select list gives the key wins over its own: SELECT
        # pid AS id ... GROUP BY pid has a column id.
        alias = next(
            (it.alias for it in items if isinstance(it, exp.Alias) and it.this == expr), None
        )
        if isinstance(key, exp.Alias):
            key_names.append(key.alias)
        elif alias:
            key_names.append(alias)
        elif isinstance(expr, exp.Column):
            key_names.append(expr.name)
        else:
            # Left unnamed, q names it (0D00:05 xbar time is time); a name
            # is only made up below if a reprojection has to refer to it.
            key_names.append(None)

    # Each select item is either a by key, which q puts in the result itself,
    # or a column computed per group.
    names: list[str | None] = []
    computed: list[tuple[str | None, exp.Expr]] = []
    key_slots: dict[int, int] = {}  # position in names -> key index
    for item in items:
        expr = unalias(item)
        if expr in key_exprs:
            key_slots[len(names)] = key_exprs.index(expr)
            names.append(key_names[key_exprs.index(expr)])
            continue
        name = item.alias if isinstance(item, exp.Alias) else None
        if name is None and isinstance(expr, exp.Column):
            name = expr.name
        names.append(name)
        computed.append((name, expr))

    having = node.args.get("having")
    hidden: list[tuple[str, exp.Expr]] = []
    condition = None
    if having is not None:
        condition, hidden = having_condition(having.this, computed, names)

    keys_first = list(key_names) + [n for n, _ in computed]
    picked_keys = sorted(key_slots.values())
    reorder = bool(hidden) or names != keys_first or picked_keys != list(range(len(key_exprs)))
    if reorder:
        # The outer select picks columns by name, so every column needs one.
        for i, name in enumerate(key_names):
            if name is None:
                key_names[i] = f"k{i}"
        for pos, k in key_slots.items():
            names[pos] = key_names[k]
        fresh_items = iter(i for i, (n, _) in enumerate(computed) if n is None)
        for pos, name in enumerate(names):
            if name is None:
                i = next(fresh_items)
                computed[i] = (f"x{i}", computed[i][1])
                names[pos] = f"x{i}"

    def column(name: str | None, expr: exp.Expr) -> str:
        return f"{gen.name(name)}:{gen.sql(expr)}" if name else gen.sql(expr)

    cols = [column(n, e) for n, e in computed] + [column(n, e) for n, e in hidden]
    by = ", ".join(
        gen.sql(e)
        if n is None or (isinstance(e, exp.Column) and e.name == n)
        else f"{gen.name(n)}:{gen.sql(e)}"
        for n, e in zip(key_names, key_exprs, strict=True)
    )
    inner = f"0!select {', '.join(cols)} by {by} from {src}{where}"
    if not reorder and condition is None:
        return inner, names
    picked = "" if not reorder else " " + ", ".join(gen.name(n) for n in names)
    filt = f" where {gen.condition(condition)}" if condition is not None else ""
    return f"select{picked} from ({inner}){filt}", names


def having_condition(
    cond: exp.Expr, computed: list[tuple[str | None, exp.Expr]], names: list[str | None]
) -> tuple[exp.Expr, list[tuple[str, exp.Expr]]]:
    """HAVING over the grouped result: each aggregate in it becomes the name of
    the column that holds it, adding a hidden column where none does."""
    hidden: list[tuple[str, exp.Expr]] = []
    cond = cond.copy()

    def swap(n: exp.Expr) -> exp.Expr:
        if not isinstance(n, exp.AggFunc):
            return n
        for i, (name, expr) in enumerate(computed):
            if expr == n:
                if name is None:
                    name = f"x{i}"
                    computed[i] = (name, expr)
                    names[names.index(None)] = name
                return exp.column(name)
        for name, expr in hidden:
            if expr == n:
                return exp.column(name)
        name = f"h{len(hidden)}"
        hidden.append((name, n.copy()))
        return exp.column(name)

    return cond.transform(swap), hidden


def order_terms(
    gen: QGenerator,
    order: exp.Order,
    items: list[exp.Expr],
    names: list[str | None],
    allow_source: bool,
) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """ORDER BY as (column, descending) pairs, sorting the result when every
    term names an output column and the source table otherwise."""
    terms = []
    for ordered in order.expressions:
        target = resolve(ordered.this, items)
        desc, nulls = bool(ordered.args.get("desc")), null_fix(ordered)
        if target in items:
            name = names[items.index(target)]
            if name is None:
                gen.refuse("ORDER BY an unnamed expression: give it an alias")
                return [], []
            terms.append((name, desc, nulls, True))
        elif isinstance(target, exp.Column) and target.name in names:
            terms.append((target.name, desc, nulls, True))
        elif isinstance(target, exp.Column):
            terms.append((target.name, desc, nulls, False))
        else:
            gen.refuse("ORDER BY is only translated for columns, aliases and positions")
            return [], []
    if all(t[3] for t in terms):
        return [(gen.name(n), d, f) for n, d, f, _ in terms], []
    if allow_source:
        # A select keeps its source's order, so sorting the table first sorts
        # the result by columns the result does not include.
        return [], [(gen.name(n), d, f) for n, d, f, _ in terms]
    gen.refuse("ORDER BY a column the grouped result does not include")
    return [], []


def null_fix(ordered: exp.Ordered) -> str | None:
    """Where the SQL wants nulls that q's sort does not put there: q sorts a
    null first ascending and last descending. 'last' or 'first' when the two
    disagree (DuckDB's ORDER BY x is NULLS LAST), None when they agree or
    the SQL does not say."""
    first = ordered.args.get("nulls_first")
    if first is None:
        return None
    desc = bool(ordered.args.get("desc"))
    if not desc and not first:
        return "last"
    if desc and first:
        return "first"
    return None


def sort(terms: list[tuple], text: str, gen=None) -> str:
    # xasc and xdesc are stable, so the least significant key sorts first and
    # each more significant one re-sorts what it left. A key whose nulls go
    # elsewhere is then re-sorted, stably, on whether it is null.
    runs: list[tuple[list[str], bool, str | None]] = []
    for name, desc, *rest in terms:
        fix = rest[0] if rest else None
        if runs and runs[-1][1] == desc and runs[-1][2] is None and fix is None:
            runs[-1][0].append(name)
        else:
            runs.append(([name], desc, fix))
    strings = gen is not None and not gen.symbols
    for cols, desc, fix in reversed(runs):
        if strings:
            # A list of strings does not sort in text order; its symbols do.
            from sqlglot_q.generator import TEXT_KEY, TEXT_NULL

            for col in reversed(cols):
                grade = "idesc" if desc else "iasc"
                text = f"{{x {grade} {TEXT_KEY} x`{col}}} {wrap(text)}"
            if fix:
                grade = "iasc" if fix == "last" else "idesc"
                text = f"{{x {grade} {TEXT_NULL} x`{cols[0]}}} ({text})"
            continue
        verb = "xdesc" if desc else "xasc"
        text = f"{''.join('`' + c for c in cols)} {verb} {wrap(text)}"
        if fix:
            grade = "iasc" if fix == "last" else "idesc"
            text = f"{{x {grade} null x`{cols[0]}}} ({text})"
    return text


def wrap(text: str) -> str:
    simple = text.replace("_", "").isalnum()
    return text if simple or enclosed(text) else f"({text})"


def enclosed(text: str) -> bool:
    """Whether the text is one parenthesised group, (...) end to end."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth, in_str = 0, False
    for i, ch in enumerate(text):
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            in_str = not in_str
        elif not in_str:
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth == 0 and i < len(text) - 1:
                return False
    return True


def limited(gen: QGenerator, node: exp.Select, text: str) -> str:
    limit = node.args.get("limit")
    offset = node.args.get("offset")
    n = gen.sql(limit.expression) if limit is not None else None
    m = gen.sql(offset.expression) if offset is not None else None
    # sublist, not #: n#t repeats t's rows when it has fewer than n.
    if n is not None and m is not None:
        return f"({m};{n}) sublist {wrap(text)}"
    if n is not None:
        return f"{n} sublist {wrap(text)}"
    if m is not None:
        return f"{m} _ {wrap(text)}"
    return text
