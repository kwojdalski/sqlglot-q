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
    if kind == "CROSS":
        return f"{left} cross {right}"
    keys = join_keys(gen, join)
    if keys is None:
        return gen.refuse(
            "a join is only translated on equality of same-named columns (ON a.k=b.k or USING)"
        )
    symbols = "".join(f"`{k}" for k in keys)
    if side == "LEFT":
        # lj takes the first match on the right, which is SQL's LEFT JOIN when
        # the right side is unique on the keys.
        return f"{left} lj {symbols} xkey {right}"
    if side or kind not in ("", "INNER"):
        return gen.refuse(f"{side or kind} JOIN has no q translation here")
    return f"ej[{symbols};{left};{right}]"


def join_keys(gen: QGenerator, join: exp.Join) -> list[str] | None:
    using = join.args.get("using")
    if using:
        return [gen.name(u.name) for u in using]
    on = join.args.get("on")
    if on is None:
        return None
    keys = []
    for c in conjuncts(on):
        if not (
            isinstance(c, exp.EQ)
            and isinstance(c.this, exp.Column)
            and isinstance(c.expression, exp.Column)
            and c.this.name == c.expression.name
        ):
            return None
        keys.append(gen.name(c.this.name))
    return keys


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
        text, names = grouped(gen, node, items, src, where)
    else:
        if node.args.get("having"):
            return gen.refuse("HAVING without GROUP BY is not translated")
        names = output_names(items)
        if order:
            sort_result, sort_source = order_terms(gen, order, items, names, allow_source=True)
        if sort_source:
            src = f"({sort(sort_source, src)})"
        columns = "" if star else " " + ", ".join(projection(gen, items))
        distinct = " distinct" if node.args.get("distinct") else ""
        text = f"select{distinct}{columns} from {src}{where}"

    if group:
        if node.args.get("distinct"):
            text = f"distinct {text}"
        if order:
            sort_result, _ = order_terms(gen, order, items, names, allow_source=False)

    if sort_result:
        text = sort(sort_result, text)
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
    for i, (key, expr) in enumerate(zip(keys, key_exprs, strict=True)):
        if isinstance(key, exp.Alias):
            key_names.append(key.alias)
        elif isinstance(expr, exp.Column):
            key_names.append(expr.name)
        else:
            alias = next(
                (it.alias for it in items if isinstance(it, exp.Alias) and it.this == expr), None
            )
            key_names.append(alias or f"k{i}")

    # Each select item is either a by key, which q puts in the result itself,
    # or a column computed per group.
    names: list[str | None] = []
    computed: list[tuple[str | None, exp.Expr]] = []
    for item in items:
        expr = unalias(item)
        if expr in key_exprs:
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
    reorder = bool(hidden) or names != keys_first
    if reorder:
        # The outer select picks columns by name, so every column needs one.
        for i, (name, expr) in enumerate(computed):
            if name is None:
                fresh = f"x{i}"
                computed[i] = (fresh, expr)
                names[names.index(None)] = fresh

    def column(name: str | None, expr: exp.Expr) -> str:
        return f"{gen.name(name)}:{gen.sql(expr)}" if name else gen.sql(expr)

    cols = [column(n, e) for n, e in computed] + [column(n, e) for n, e in hidden]
    by = ", ".join(
        gen.sql(e) if isinstance(e, exp.Column) and e.name == n else f"{gen.name(n)}:{gen.sql(e)}"
        for n, e in zip(key_names, key_exprs, strict=True)
    )
    inner = f"0!select {', '.join(cols)} by {by} from {src}{where}"
    if not reorder and condition is None:
        return inner, names
    picked = "" if not reorder else " " + ", ".join(gen.name(n) for n in names)
    filt = f" where {gen.sql(condition)}" if condition is not None else ""
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
        if target in items:
            name = names[items.index(target)]
            if name is None:
                gen.refuse("ORDER BY an unnamed expression: give it an alias")
                return [], []
            terms.append((name, bool(ordered.args.get("desc")), True))
        elif isinstance(target, exp.Column) and target.name in names:
            terms.append((target.name, bool(ordered.args.get("desc")), True))
        elif isinstance(target, exp.Column):
            terms.append((target.name, bool(ordered.args.get("desc")), False))
        else:
            gen.refuse("ORDER BY is only translated for columns, aliases and positions")
            return [], []
    if all(in_output for _, _, in_output in terms):
        return [(gen.name(n), d) for n, d, _ in terms], []
    if allow_source:
        # A select keeps its source's order, so sorting the table first sorts
        # the result by columns the result does not include.
        return [], [(gen.name(n), d) for n, d, _ in terms]
    gen.refuse("ORDER BY a column the grouped result does not include")
    return [], []


def sort(terms: list[tuple[str, bool]], text: str) -> str:
    # xasc and xdesc are stable, so the least significant key sorts first and
    # each more significant one re-sorts what it left.
    runs: list[tuple[list[str], bool]] = []
    for name, desc in terms:
        if runs and runs[-1][1] == desc:
            runs[-1][0].append(name)
        else:
            runs.append(([name], desc))
    for cols, desc in reversed(runs):
        verb = "xdesc" if desc else "xasc"
        text = f"{''.join('`' + c for c in cols)} {verb} {wrap(text)}"
    return text


def wrap(text: str) -> str:
    simple = text.replace("_", "").isalnum()
    return text if simple else f"({text})"


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
