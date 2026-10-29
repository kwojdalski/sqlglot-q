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
