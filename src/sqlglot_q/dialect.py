"""The q dialect: a sqlglot Dialect whose generator writes q.

It is write-only. Reading q is a different problem - q has no grammar a SQL
tokenizer can approximate - so `read="q"` is refused rather than guessed at.
"""

from __future__ import annotations

from sqlglot import parser
from sqlglot.dialects.dialect import Dialect

from sqlglot_q.generator import QGenerator


class Q(Dialect):
    Generator = QGenerator

    class Parser(parser.Parser):
        def parse(self, raw_tokens, sql=None):
            raise NotImplementedError("the q dialect writes q; it does not read it")
