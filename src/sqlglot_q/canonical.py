"""One spelling of an expression's grouping, shared by reading and writing.

Both directions pass their tree through `canonical`, so a round trip lands on
the same text: SQL -> q -> SQL -> q gives the same q twice, and the SQL read
from q is parenthesised the same way whatever the q it came from wrote.
"""

from __future__ import annotations

from sqlglot import exp

# Operators whose operands SQL would misread without parentheses, and the
# operand slots that need them.
COMPOUND = (exp.Binary, exp.Not, exp.Is, exp.In, exp.Between, exp.Like, exp.ILike)
SLOTS = ("this", "expression", "low", "high")


def compound(node: exp.Expr) -> bool:
    if isinstance(node, exp.Neg):
        return not isinstance(node.this, exp.Literal)
    return isinstance(node, COMPOUND)


def canonical(node: exp.Expr) -> exp.Expr:
    """The tree with q's grouping and SQL's spelling of it: q's parentheses
    are dropped, since the tree already holds what they grouped, and every
    compound operand of an operator is parenthesised. sqlglot prints a tree
    without adding parentheses for precedence, so a*(b+c) read from q's
    a*b+c would otherwise print as a * b + c."""
    node = strip_parens(node)
    node = node.transform(unnegate)

    def wrap(n: exp.Expr) -> exp.Expr:
        if isinstance(n, (*COMPOUND, exp.Neg)):
            for slot in SLOTS:
                child = n.args.get(slot)
                if not isinstance(child, exp.Expr) or not compound(child):
                    continue
                if isinstance(n, exp.Escape) and slot == "this":
                    continue  # x LIKE p ESCAPE e is one operator
                if isinstance(n, exp.Not) and not isinstance(child, exp.Connector):
                    # SQL's NOT binds looser than everything but AND and OR:
                    # NOT b IN (...) is NOT (b IN (...)) already.
                    continue
                n.set(slot, exp.Paren(this=child))
        return n

    return node.transform(wrap)


def unnegate(node: exp.Expr) -> exp.Expr:
    """b NOT LIKE 'x%' arrives as Like(negate=True) from newer sqlglot; as
    NOT around a plain Like, nothing downstream can miss the NOT."""
    if node.args.get("negate"):
        node = node.copy()
        node.set("negate", None)
        return exp.Not(this=node)
    return node


def strip_parens(node: exp.Expr) -> exp.Expr:
    """Every Paren replaced by what it holds. sqlglot's transform does not
    descend into a node it has just replaced, so ((x)) would keep its inner
    parentheses; this removes them all."""
    while isinstance(node, exp.Paren):
        node = node.this
    for paren in list(node.find_all(exp.Paren)):
        inner = paren.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        paren.replace(inner)
    return node
