"""SQL's three-valued logic, in q, wherever SQL keeps rows on a condition.

In SQL a comparison with NULL is unknown, and WHERE, HAVING, CASE WHEN and
FILTER keep a row only where the condition is true. q has no unknown: a null
is the smallest value of its type, so 0N<3 is true, 0N<>3 is true and 0N=0N
is true, and `not` turns a false into a true. So in those places a condition
is written as what makes it *definitely true* (`holds`), and a NOT as what
makes its operand *definitely false* (`fails`):

    a < 3          (not null a)&a<3        a null row is not kept
    NOT (a > 2)    (not null a)&a<=2
    a > 3          a>3                     a null is already not > 3
    a <> b         (not null a)&(not null b)&a<>b

A guard is only added on a side that can be null and would make q say yes;
literals cannot be null. parser.py strips these guards when reading q back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

if TYPE_CHECKING:
    from sqlglot_q.generator import QGenerator

# Text tests, all q's like: a null symbol is ` and matches "*".
TEXT_TESTS = (exp.Like, exp.ILike, exp.StartsWith, exp.EndsWith, exp.Contains)

NEGATED = {
    exp.EQ: exp.NEQ,
    exp.NEQ: exp.EQ,
    exp.GT: exp.LTE,
    exp.GTE: exp.LT,
    exp.LT: exp.GTE,
    exp.LTE: exp.GT,
}


def nullable(node: exp.Expr) -> bool:
    """Whether a value can be null: a literal cannot."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Interval)):
        return False
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return False
    return not (isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal))


def guarded(gen: QGenerator, sides: list[exp.Expr], text: str) -> str:
    seen, guards = [], []
    for side in sides:
        if nullable(side) and side not in seen:
            seen.append(side)
            guards.append(f"(not {gen.is_null(gen.operand(side))})")
    return "&".join([*guards, text]) if guards else text


def comparison_guards(node: exp.Expr) -> list[exp.Expr]:
    """The sides whose null would make q's comparison true: the right side of
    > and >= (x>0N), the left of < and <= (0N<x), both of <> and, for =,
    one side when both can be null (0N=0N)."""
    a, b = node.this, node.expression
    if isinstance(node, (exp.GT, exp.GTE)):
        return [b]
    if isinstance(node, (exp.LT, exp.LTE)):
        return [a]
    if isinstance(node, exp.NEQ):
        return [a, b]
    return [a] if nullable(a) and nullable(b) else []


def holds(gen: QGenerator, node: exp.Expr) -> str:
    """q true exactly where the SQL condition is true."""
    if isinstance(node, exp.Paren):
        return holds(gen, node.this)
    if isinstance(node, (exp.NullSafeEQ, exp.NullSafeNEQ)):
        return gen.sql(node)  # q's = and <> are null-safe already
    if isinstance(node, tuple(NEGATED)):
        return guarded(gen, comparison_guards(node), gen.sql(node))
    if isinstance(node, exp.Not):
        return fails(gen, node.this)
    if isinstance(node, exp.And):
        return f"({holds(gen, node.this)})&({holds(gen, node.expression)})"
    if isinstance(node, exp.Or):
        return f"({holds(gen, node.this)})|({holds(gen, node.expression)})"
    if isinstance(node, exp.Is):
        return gen.sql(node)
    if isinstance(node, exp.In):
        if node.args.get("query") is None:
            values = [v for v in node.expressions if not isinstance(v, exp.Null)]
            if not values:
                return "0b"
            node = node.copy()
            node.set("expressions", values)
            # A null is in no list without a null: 0N in 1 2 is false.
            return gen.sql(node)
        # A subquery can hold a null, and 0N in (...;0N) is true.
        return guarded(gen, [node.this], gen.sql(node))
    if isinstance(node, exp.Between):
        # x within (lo;hi) is x>=lo & x<=hi: a null lo makes x>=lo true,
        # and then a null x too; a null x or hi alone already fails.
        low = node.args["low"]
        sides = [low, node.this] if nullable(low) else []
        return guarded(gen, sides, gen.sql(node))
    if isinstance(node, TEXT_TESTS):
        # A null symbol is ` , the empty one: only a pattern that matches
        # the empty text ('%', '') matches it, and only those need a guard.
        pattern = node.expression
        if isinstance(pattern, exp.Literal) and pattern.is_string:
            if isinstance(node, (exp.Like, exp.ILike)) and pattern.this.replace("%", ""):
                return gen.sql(node)
            if not isinstance(node, (exp.Like, exp.ILike)) and pattern.this:
                return gen.sql(node)
        return guarded(gen, [node.this], gen.sql(node))
    return gen.sql(node)


def fails(gen: QGenerator, node: exp.Expr) -> str:
    """q true exactly where the SQL condition is false (not unknown)."""
    if isinstance(node, exp.Paren):
        return fails(gen, node.this)
    if isinstance(node, exp.NullSafeEQ):
        return gen.sql(exp.NullSafeNEQ(this=node.this.copy(), expression=node.expression.copy()))
    if isinstance(node, exp.NullSafeNEQ):
        return gen.sql(exp.NullSafeEQ(this=node.this.copy(), expression=node.expression.copy()))
    if type(node) in NEGATED:
        flipped = NEGATED[type(node)](this=node.this.copy(), expression=node.expression.copy())
        return holds(gen, flipped)
    if isinstance(node, exp.Not):
        return holds(gen, node.this)
    if isinstance(node, exp.And):
        return f"({fails(gen, node.this)})|({fails(gen, node.expression)})"
    if isinstance(node, exp.Or):
        return f"({fails(gen, node.this)})&({fails(gen, node.expression)})"
    if isinstance(node, exp.Is):
        return f"not {gen.sql(node)}"
    if isinstance(node, exp.In):
        query = node.args.get("query")
        if query is None:
            if any(isinstance(v, exp.Null) for v in node.expressions):
                return "0b"  # x NOT IN (..., NULL) is never true
            return guarded(gen, [node.this], f"not {gen.sql(node)}")
        # x NOT IN (SELECT ...) is never true once the subquery has a null.
        text = gen.sql(node)
        subject, _, values = text.partition(" in ")
        return guarded(gen, [node.this], f"(not {text})&not any null {values}")
    if isinstance(node, exp.Between):
        sides = [node.this, node.args["low"], node.args["high"]]
        return guarded(gen, sides, f"not {gen.sql(node)}")
    if isinstance(node, TEXT_TESTS):
        return guarded(gen, [node.this], f"not {gen.sql(node)}")
    if isinstance(node, exp.Boolean):
        return "0b" if node.this else "1b"
    return f"not {gen.operand(node)}"
