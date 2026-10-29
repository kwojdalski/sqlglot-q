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
    node = node.transform(lambda n: n.this if isinstance(n, exp.Paren) else n)

    def wrap(n: exp.Expr) -> exp.Expr:
        if isinstance(n, (*COMPOUND, exp.Neg)):
            for slot in SLOTS:
                child = n.args.get(slot)
                if not isinstance(child, exp.Expr) or not compound(child):
                    continue
                if isinstance(n, exp.Not) and not isinstance(child, exp.Connector):
                    # SQL's NOT binds looser than everything but AND and OR:
                    # NOT b IN (...) is NOT (b IN (...)) already.
                    continue
                n.set(slot, exp.Paren(this=child))
        return n

    return node.transform(wrap)
