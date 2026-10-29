"""Rewrites of a SQL tree before it is written as q, in the shape of
sqlglot's own (sqlglot.transforms): each takes an expression and returns one,
so QGenerator.TRANSFORMS chains them with transforms.preprocess."""

from __future__ import annotations

from sqlglot import exp


def expand_group_by_all(expression: exp.Expr) -> exp.Expr:
    """GROUP BY ALL (DuckDB) as GROUP BY each select item that is not an
    aggregate, which is what DuckDB groups by."""
    if not isinstance(expression, exp.Select):
        return expression
    group = expression.args.get("group")
    if group is None or not group.args.get("all"):
        return expression
    keys = [
        item.this.copy() if isinstance(item, exp.Alias) else item.copy()
        for item in expression.expressions
        if not item.find(exp.AggFunc)
    ]
    expression = expression.copy()
    expression.set("group", exp.Group(expressions=keys))
    return expression


def q_names(expression: exp.Expr) -> exp.Expr:
    """sqlglot's rewrites name what they add _w, _row_number, _t; a q name
    cannot start with an underscore, so those become q_w, q_row_number."""
    if not isinstance(expression, exp.Select):
        return expression
    # eliminate_distinct_on projects bare identifiers; a parser makes columns.
    if any(isinstance(i, exp.Identifier) for i in expression.expressions):
        expression = expression.copy()
        expression.set(
            "expressions",
            [
                exp.column(i.name) if isinstance(i, exp.Identifier) else i
                for i in expression.expressions
            ],
        )
    hidden = {i.name for i in expression.find_all(exp.Identifier) if i.name.startswith("_")}
    if not hidden:
        return expression

    def rename(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Identifier) and node.name in hidden:
            return exp.to_identifier(f"q{node.name}")
        return node

    return expression.transform(rename)


def quantified_in(expression: exp.Expr) -> exp.Expr:
    """x = ANY (SELECT ...) is x IN (SELECT ...), and x <> ALL (SELECT ...)
    is x NOT IN (SELECT ...), NULLs and all; both already have a q form."""
    if not isinstance(expression, exp.Select):
        return expression

    def rewrite(node: exp.Expr) -> exp.Expr:
        if isinstance(node, (exp.EQ, exp.NEQ)) and isinstance(node.expression, (exp.Any, exp.All)):
            quantifier = node.expression
            query = quantifier.this
            if not isinstance(query, (exp.Subquery, exp.Select)):
                return node
            wanted = exp.Any if isinstance(node, exp.EQ) else exp.All
            if not isinstance(quantifier, wanted):
                return node
            found = exp.In(this=node.this.copy(), query=query.copy())
            return found if isinstance(node, exp.EQ) else exp.Not(this=found)
        return node

    expression = expression.transform(rewrite)
    for key in ("where", "having"):
        clause = expression.args.get(key)
        if clause is not None:
            clause.set("this", ordered_quantified(clause.this))
    return expression


ORDERED = (exp.GT, exp.GTE, exp.LT, exp.LTE)


def ordered_quantified(cond: exp.Expr) -> exp.Expr:
    """x > ANY (SELECT v ...) as x > (SELECT MIN(v) ...), and x > ALL as x
    above the MAX, with no NULL among the values and nothing above an empty
    set. The two agree on where the condition is true, so this is done only
    where nothing but that matters: down a WHERE's ANDs and ORs, not under a
    NOT, where a false and an unknown part."""
    if isinstance(cond, (exp.And, exp.Or)):
        return cond.__class__(
            this=ordered_quantified(cond.this), expression=ordered_quantified(cond.expression)
        )
    if isinstance(cond, exp.Paren):
        return exp.Paren(this=ordered_quantified(cond.this))
    if not (isinstance(cond, ORDERED) and isinstance(cond.expression, (exp.Any, exp.All))):
        return cond
    quantifier = cond.expression
    query = quantifier.this
    query = query.this if isinstance(query, exp.Subquery) else query
    if not isinstance(query, exp.Select) or len(query.expressions) != 1:
        return cond
    if any(query.args.get(k) for k in ("group", "having", "limit", "offset", "order")):
        return cond
    value = query.expressions[0]
    value = value.this if isinstance(value, exp.Alias) else value
    above = isinstance(cond, (exp.GT, exp.GTE))
    every = isinstance(quantifier, exp.All)
    # Above ANY value is above the least; above ALL is above the greatest.
    agg = exp.Max if above == every else exp.Min

    def scalar(item: exp.Expr) -> exp.Expr:
        sub = query.copy()
        sub.set("distinct", None)
        sub.set("expressions", [exp.alias_(item, "q_v")])
        return exp.Subquery(this=sub)

    compared = cond.__class__(this=cond.this.copy(), expression=scalar(agg(this=value.copy())))
    if not every:
        return compared
    count = exp.Count(this=exp.Star())
    no_nulls = exp.EQ(
        this=scalar(exp.Sub(this=count.copy(), expression=exp.Count(this=value.copy()))),
        expression=exp.Literal.number(0),
    )
    empty = exp.EQ(this=scalar(count), expression=exp.Literal.number(0))
    return exp.Paren(this=exp.and_(no_nulls, exp.Paren(this=exp.or_(compared, empty)), copy=False))


def join_conditions(expression: exp.Expr) -> exp.Expr:
    """An inner join's condition split into what q's ej joins on and what it
    filters: FROM t JOIN u ON t.k = u.k AND t.n > 2 is the join on k, WHERE
    t.n > 2, which for an inner join is the same rows. And FROM t, u WHERE
    t.k = u.k is that join written the old way."""
    if not isinstance(expression, exp.Select) or not expression.args.get("joins"):
        return expression
    expression = expression.copy()
    from_ = expression.args.get("from_")
    seen = {from_.this.alias_or_name} if from_ else set()
    moved: list[exp.Expr] = []
    where = expression.args.get("where")
    remaining = conjuncts(where.this) if where is not None else []
    for join in expression.args["joins"]:
        name = join.this.alias_or_name
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        inner = not side and kind in ("", "INNER") and not join.args.get("method")
        on = join.args.get("on")
        if inner and on is not None and not join.args.get("using"):
            keys = [c for c in conjuncts(on) if key_pair(c, seen, name)]
            rest = [c for c in conjuncts(on) if not key_pair(c, seen, name)]
            if keys and rest:
                join.set("on", exp.and_(*keys, copy=False))
                moved.extend(rest)
        elif inner and on is None and not join.args.get("using") and kind != "CROSS":
            keys = [c for c in remaining if key_pair(c, seen, name)]
            if keys:
                join.set("on", exp.and_(*(k.copy() for k in keys), copy=False))
                remaining = [c for c in remaining if not any(c is k for k in keys)]
        seen.add(name)
    if remaining != (conjuncts(where.this) if where is not None else []) or moved:
        conds = [*remaining, *moved]
        expression.set("where", exp.Where(this=exp.and_(*conds, copy=False)) if conds else None)
    return expression


def conjuncts(node: exp.Expr) -> list[exp.Expr]:
    return list(node.flatten()) if isinstance(node, exp.And) else [node]


def key_pair(cond: exp.Expr, left: set[str], right: str) -> bool:
    """cond is left.a = right.b, a table already joined against the new one."""
    if not (
        isinstance(cond, exp.EQ)
        and isinstance(cond.this, exp.Column)
        and isinstance(cond.expression, exp.Column)
    ):
        return False
    tables = {cond.this.table, cond.expression.table}
    return right in tables and len(tables) == 2 and bool(tables - {right} <= left)
