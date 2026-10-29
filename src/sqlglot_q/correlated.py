"""Subqueries that read the outer query's columns.

q evaluates a subquery once, as a table of its own: in
(SELECT AVG(n) FROM t AS b WHERE b.g = a.g) both sides of b.g = a.g are t's g,
so g=g is true on every row and the subquery is the overall average. Such a
subquery is translated only where SQL's meaning has a q form, and refused
everywhere else:

    EXISTS (SELECT ... FROM u WHERE u.k = t.k AND c)
        t's k is among u's k where c: k in exec k from u where c, not null k
"""

from __future__ import annotations

from sqlglot import exp


def sources(node: exp.Expr) -> set[str]:
    """The names the tables of node (and of its subqueries) go by."""
    names = set()
    for table in node.find_all(exp.Table):
        names.add(table.alias_or_name)
    for sub in node.find_all(exp.Subquery):
        if sub.alias:
            names.add(sub.alias)
    return names


def outer_columns(query: exp.Expr) -> list[exp.Column]:
    """The columns of query qualified by a table it does not read."""
    own = sources(query)
    return [c for c in query.find_all(exp.Column) if c.table and c.table not in own]


def semi_join(query: exp.Select) -> tuple[exp.Column, exp.Column, list[exp.Expr]] | None:
    """For WHERE inner.k = outer.k AND rest, with no other outer reference:
    (the outer column, the inner one, rest)."""
    from sqlglot_q.query import conjuncts

    where = query.args.get("where")
    if where is None or any(query.args.get(k) for k in ("group", "having", "limit", "joins")):
        return None
    own = sources(query)
    pairs, rest = [], []
    for cond in conjuncts(where.this):
        outer = [c for c in cond.find_all(exp.Column) if c.table and c.table not in own]
        if not outer:
            rest.append(cond)
            continue
        if not (
            isinstance(cond, exp.EQ)
            and isinstance(cond.this, exp.Column)
            and isinstance(cond.expression, exp.Column)
            and len(outer) == 1
        ):
            return None
        inner = cond.expression if outer[0] is cond.this else cond.this
        pairs.append((outer[0], inner))
    if len(pairs) != 1:
        return None
    outside = [c for c in query.find_all(exp.Column) if c.table and c.table not in own]
    if len(outside) != 1:
        return None
    return pairs[0][0], pairs[0][1], rest


def exists(gen, query: exp.Select, negated: bool = False) -> str | None:
    """EXISTS over a correlated query as IN: None when it is not one. NOT
    EXISTS is true for a null outer key, which is in no list: not k in ..."""
    if not outer_columns(query):
        return None
    found = semi_join(query)
    if found is None:
        return gen.refuse(
            "a correlated EXISTS is translated when its only reference to the outer query "
            "is WHERE inner.k = outer.k"
        )
    outer, inner, rest = found
    # A null key matches nothing in SQL: the inner side loses its nulls, and
    # a null outer key is in no list without one.
    conds = [
        *(c.copy() for c in rest),
        exp.Not(this=exp.Is(this=inner.copy(), expression=exp.Null())),
    ]
    sub = exp.Select(expressions=[inner.copy()]).from_(query.args["from_"].this.copy())
    sub = sub.where(exp.and_(*conds), copy=False)
    from sqlglot_q.truth import holds

    found = exp.In(this=exp.column(outer.name), query=sub)
    if negated:
        return f"not ({gen.sql(found)})"
    # As IN prints where it keeps rows, so EXISTS and the IN it reads back as
    # print the same.
    return holds(gen, found)


def refuse_correlated(gen, query: exp.Expr, what: str) -> str | None:
    if outer_columns(query):
        return gen.refuse(
            f"{what} reads the outer query's columns; q evaluates a subquery once, "
            "as a table of its own"
        )
    return None
