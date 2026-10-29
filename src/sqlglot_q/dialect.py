"""The q dialect: a sqlglot Dialect that writes q, and reads the qSQL it writes.

Writing is the generator (generator.py). Reading is its own small parser
(parser.py), because q has no grammar a SQL tokenizer can approximate; it
reads qSQL queries and statements and refuses the rest of q.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from sqlglot_q.generator import QGenerator
from sqlglot_q.parser import QParser


class Q(Dialect):
    Generator = QGenerator

    def parse(self, sql: str, **opts) -> list[exp.Expr | None]:
        return QParser(**opts).parse(sql)

    def parse_into(self, expression_type, sql: str, **opts) -> list[exp.Expr | None]:
        return self.parse(sql, **opts)
