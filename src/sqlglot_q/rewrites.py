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


def regex_like(expression: exp.Expr) -> exp.Expr:
    """A regular expression LIKE can state, as that LIKE: q has no regex
    engine, and its like is LIKE's. REGEXP_MATCHES searches the text, and
    SIMILAR TO and ~ (REGEXP_FULL_MATCH) match the whole of it. Anything
    else is left, and refused."""
    if not isinstance(expression, exp.Select):
        return expression

    def rewrite(node: exp.Expr) -> exp.Expr:
        full = isinstance(node, (exp.SimilarTo, exp.RegexpFullMatch))
        if not (full or isinstance(node, exp.RegexpLike)):
            return node
        pattern, flag = node.expression, node.args.get("flag")
        if not (isinstance(pattern, exp.Literal) and pattern.is_string):
            return node
        if node.args.get("escape") or (flag is not None and flag.name != "i"):
            return node
        if isinstance(node, exp.SimilarTo) and any(c in pattern.this for c in "%_.*+?|()[]{}\\"):
            # SIMILAR TO is a regex in DuckDB and LIKE's wildcards with regex
            # repetition in Postgres, where . is itself: which this is is not
            # known, so only a pattern of plain text is translated.
            return node
        like = like_of_regex(pattern.this, full)
        if like is None:
            return node
        kind = exp.ILike if flag is not None else exp.Like
        test = kind(this=node.this.copy(), expression=exp.Literal.string(like))
        if "\\" in like:
            return exp.Escape(this=test, expression=exp.Literal.string("\\"))
        return test

    return expression.transform(rewrite)


def like_of_regex(regex: str, full: bool) -> str | None:
    """The LIKE pattern matching what regex matches, or None: literal text,
    . and .*, ^ and $, and escaped characters."""
    start = full or regex.startswith("^")
    end = full or (regex.endswith("$") and not regex.endswith("\\$"))
    body = regex[1 if regex.startswith("^") else 0 :]
    body = body[:-1] if regex.endswith("$") and not regex.endswith("\\$") else body
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            if i + 1 == len(body) or body[i + 1].isalnum():
                return None  # \d, \w, \b: classes and anchors LIKE has no form for
            ch, i = body[i + 1], i + 1
            out.append("\\" + ch if ch in "%_\\" else ch)
        elif body.startswith(".*", i):
            out.append("%")
            i += 1
        elif ch == ".":
            out.append("_")
        elif ch in "*+?{}()[]|^$":
            return None
        elif ch in "%_":
            out.append("\\" + ch)
        else:
            out.append(ch)
        i += 1
    text = "".join(out)
    return ("" if start else "%") + text + ("" if end else "%")


def order_by_expression(expression: exp.Expr) -> exp.Expr:
    """ORDER BY an expression the select does not have, as a column of a
    subquery that the outer select sorts on and leaves out:

        SELECT g FROM t ORDER BY ABS(n)
        SELECT g FROM (SELECT *, ABS(n) AS q_o0 FROM t) ORDER BY q_o0

    and with GROUP BY, the expression is an item of the grouped subquery."""
    if not isinstance(expression, exp.Select):
        return expression
    order = expression.args.get("order")
    if order is None or expression.args.get("distinct") or expression.args.get("joins"):
        return expression
    items = expression.expressions
    if any(i.find(exp.Window) for i in items) or not expression.args.get("from_"):
        return expression
    plain = [i.this if isinstance(i, exp.Alias) else i for i in items]
    aliases = {i.alias for i in items if isinstance(i, exp.Alias)}

    def wanted(term: exp.Expr) -> bool:
        target = term.this
        if isinstance(target, exp.Literal) or isinstance(target, exp.Column):
            return False  # a position, a column or an alias
        return target not in plain and not (
            isinstance(target, exp.Identifier) and target.name in aliases
        )

    if not any(wanted(o) for o in order.expressions):
        return expression
    grouped = expression.args.get("group") is not None
    if grouped and not all(isinstance(i, (exp.Alias, exp.Column)) for i in items):
        return expression  # the outer select names each item
    inner = expression.copy()
    for key in ("order", "limit", "offset"):
        inner.set(key, None)
    hidden, terms = [], []
    for term in order.expressions:
        if wanted(term):
            name = f"q_o{len(hidden)}"
            hidden.append(exp.alias_(term.this.copy(), name))
            term = term.copy()
            term.set("this", exp.column(name))
        terms.append(term.copy())
    if grouped:
        inner.set("expressions", [i.copy() for i in items] + hidden)
        outer_items = [exp.column(i.alias_or_name) for i in items]
    else:
        inner.set("expressions", [exp.Star(), *hidden])

        def unqualified(node: exp.Expr) -> exp.Expr:
            if isinstance(node, exp.Column) and node.table:
                return exp.column(node.name)
            return node

        outer_items = [i.copy().transform(unqualified) for i in items]
    outer = exp.Select(expressions=outer_items).from_(exp.Subquery(this=inner), copy=False)
    outer.set("order", exp.Order(expressions=terms))
    for key in ("limit", "offset"):
        if expression.args.get(key):
            outer.set(key, expression.args[key].copy())
    return outer


def correlated_scalars(expression: exp.Expr) -> exp.Expr:
    """A scalar subquery correlated on one key, as a grouped join: q
    evaluates a subquery once, so the per-row value comes from joining the
    subquery's aggregate by its key.

        SELECT g, n FROM t AS a WHERE n > (SELECT AVG(n) FROM t AS b WHERE b.g = a.g)
        SELECT g, n FROM t AS a LEFT JOIN
          (SELECT b.g AS q_k0, AVG(n) AS q_c0 FROM t AS b GROUP BY b.g) AS q_s0
          ON a.g = q_s0.q_k0 WHERE n > q_c0

    No row for the key is NULL, as the subquery over no rows; COUNT's 0."""
    if not isinstance(expression, exp.Select) or not expression.args.get("from_"):
        return expression
    from sqlglot_q.correlated import outer_columns, semi_join

    expression = expression.copy()
    joins = []
    for sub in list(expression.find_all(exp.Subquery)):
        if sub.find_ancestor(exp.Select) is not expression:
            continue
        if isinstance(sub.parent, (exp.From, exp.Join, exp.In, exp.Exists)):
            continue
        query = sub.this
        if not isinstance(query, exp.Select) or not outer_columns(query):
            continue
        found = semi_join(query)
        if found is None or len(query.expressions) != 1 or query.args.get("distinct"):
            continue
        outer, inner, rest = found
        value = query.expressions[0]
        value = value.this if isinstance(value, exp.Alias) else value
        if not isinstance(value.find(exp.AggFunc), exp.AggFunc):
            continue
        bare = [c for c in value.find_all(exp.Column) if c.find_ancestor(exp.AggFunc) is None]
        if bare:
            continue  # a column outside the aggregate: one value per row
        n = len(joins)
        key, column, alias = f"q_k{n}", f"q_c{n}", f"q_s{n}"
        grouped = exp.Select(
            expressions=[exp.alias_(inner.copy(), key), exp.alias_(value.copy(), column)]
        ).from_(query.args["from_"].this.copy(), copy=False)
        if rest:
            grouped.set("where", exp.Where(this=exp.and_(*(c.copy() for c in rest), copy=False)))
        grouped.set("group", exp.Group(expressions=[inner.copy()]))
        joins.append(
            exp.Join(
                this=exp.Subquery(
                    this=grouped, alias=exp.TableAlias(this=exp.to_identifier(alias))
                ),
                side="LEFT",
                on=exp.EQ(this=outer.copy(), expression=exp.column(key, table=alias)),
            )
        )
        result = exp.column(column, table=alias)
        if isinstance(value, exp.Count):
            result = exp.Coalesce(this=result, expressions=[exp.Literal.number(0)])
        sub.replace(result)
    if joins:
        expression.set("joins", [*(expression.args.get("joins") or []), *joins])
    return expression


def table_star(expression: exp.Expr) -> exp.Expr:
    """SELECT t.* FROM t is SELECT * FROM t. (Over a join, t.* is one side's
    columns, which q's joined table does not keep apart: refused.)"""
    if not isinstance(expression, exp.Select) or expression.args.get("joins"):
        return expression
    from_ = expression.args.get("from_")
    if from_ is None:
        return expression
    name = from_.this.alias_or_name
    items = expression.expressions
    if not any(isinstance(i, exp.Column) and isinstance(i.this, exp.Star) for i in items):
        return expression
    expression = expression.copy()
    expression.set(
        "expressions",
        [
            exp.Star()
            if isinstance(i, exp.Column) and isinstance(i.this, exp.Star) and i.table == name
            else i
            for i in expression.expressions
        ],
    )
    return expression
