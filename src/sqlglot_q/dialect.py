"""The q dialect: a sqlglot Dialect that writes q, and reads the qSQL it writes.

Writing is the generator (generator.py). Reading is its own small parser
(parser.py), because q has no grammar a SQL tokenizer can approximate; it
reads qSQL queries and statements and refuses the rest of q.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect, NormalizationStrategy

from sqlglot_q.generator import QGenerator
from sqlglot_q.parser import QParser


class Q(Dialect):
    """q/kdb+. Settings, as for any sqlglot dialect, follow the name:
    `write="q, string_literals=string"` makes SQL text q strings, not
    symbols."""

    Generator = QGenerator

    # q names are case sensitive: Price and price are two columns.
    NORMALIZATION_STRATEGY = NormalizationStrategy.CASE_SENSITIVE
    # q sorts a null first ascending and last descending.
    NULL_ORDERING = "nulls_are_small"

    # sqlglot refuses a setting a dialect does not declare.
    SUPPORTED_SETTINGS = {*getattr(Dialect, "SUPPORTED_SETTINGS", set()), "string_literals"}

    def parse(self, sql: str, **opts) -> list[exp.Expr | None]:
        return QParser(dialect=self, **opts).parse(sql)

    def parse_into(self, expression_type, sql: str, **opts) -> list[exp.Expr | None]:
        return self.parse(sql, **opts)
