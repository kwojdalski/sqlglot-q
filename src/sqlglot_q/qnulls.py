"""Reading q's comparisons as SQL that means the same, nulls included.

q has no unknown: a null is the smallest value of its type, so 0N<3 is
true, 0N>3 false, and every comparison is a plain boolean. SQL's is unknown
on a null. Read back naively, hand-written q's `where n<3` would drop the null
rows q keeps, and `select f:n>2` give NULL where q gives 0b. So each
comparison the generator did not guard is read as SQL that is never unknown
and agrees with q:

    q            in a filter (WHERE, HAVING, CASE WHEN)   as a value
    n<3          n < 3 OR n IS NULL                       COALESCE(n < 3, TRUE)
    n>3          n > 3                                    COALESCE(n > 3, FALSE)
    not n>2      NOT COALESCE(n > 2, FALSE)

The generator writes COALESCE(n > 3, FALSE) back as n>3, which is what q
computes, so a round trip settles.
"""

from __future__ import annotations

from sqlglot import exp

ORDERED = (exp.LT, exp.LTE, exp.GT, exp.GTE)
CONTAINS = (exp.In, exp.Between, exp.Like, exp.ILike)


def nullable(node: exp.Expr) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Interval)):
        return False
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return False
    return not (isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal))


def null_result(node: exp.Expr) -> bool | None:
    """What q's comparison gives when its one nullable side is null (a null
    is below every value), or None when both sides can be null."""
    a, b = nullable(node.this), nullable(node.expression)
    if a and b:
        return None
    if not (a or b):
        return False
    if isinstance(node, exp.EQ):
        return False
    small_left = isinstance(node, (exp.LT, exp.LTE))
    return small_left if a else not small_left


def faithful(node: exp.Expr, filter: bool = False) -> exp.Expr:
    """The tree with q's comparisons read as never-unknown SQL."""
    while isinstance(node, exp.Paren):
        node = node.this
    if node.meta.get("guarded"):
        return node  # the generator's guard: SQL's own comparison
    if isinstance(node, (exp.And, exp.Or)):
        return type(node)(
            this=faithful(node.this, filter), expression=faithful(node.expression, filter)
        )
    if isinstance(node, exp.Not):
        return exp.Not(this=exp.Paren(this=faithful(node.this, filter=False)))
    if isinstance(node, (*ORDERED, exp.EQ)):
        result = null_result(node)
        if result is None:
            return node  # two nullable sides: rare; left as SQL reads them
        if not result:
            return node if filter else exp.Coalesce(this=node, expressions=[exp.false()])
        if filter:
            side = node.this if nullable(node.this) else node.expression
            return exp.Or(this=node, expression=exp.Is(this=side.copy(), expression=exp.Null()))
        return exp.Coalesce(this=node, expressions=[exp.true()])
    if isinstance(node, exp.Escape):
        test = node.this.this if isinstance(node.this, exp.Paren) else node.this
        if nullable(test.this) and not filter:
            return exp.Coalesce(this=node, expressions=[exp.false()])
        return node
    if isinstance(node, CONTAINS) and nullable(node.this):
        # 0N in 1 2 and 0N within 1 3 are false in q, NULL in SQL
        return node if filter else exp.Coalesce(this=node, expressions=[exp.false()])
    return walk(node)


FILTERS = ("where", "having", "qualify")


def walk(node: exp.Expr) -> exp.Expr:
    """Every child, a filter where SQL keeps rows on it and a value elsewhere."""
    for key, value in list(node.args.items()):
        if isinstance(node, exp.Update) and key == "expressions":
            # SET c = v: assignments, which sqlglot holds as EQ
            for assign in value:
                assign.set("expression", faithful(assign.expression))
            continue
        if isinstance(node, exp.Case) and key == "ifs":
            for when in value:
                when.set("this", faithful(when.this, filter=True))
                when.set("true", faithful(when.args["true"]))
            continue
        if isinstance(value, exp.Expr):
            if isinstance(value, (exp.Where, exp.Having)):
                value.set("this", faithful(value.this, filter=True))
                continue
            if isinstance(node, exp.Join) and key == "on":
                node.set(key, faithful(value, filter=True))
                continue
            node.set(key, faithful(value))
        elif isinstance(value, list):
            node.set(key, [faithful(v) if isinstance(v, exp.Expr) else v for v in value])
    return node
