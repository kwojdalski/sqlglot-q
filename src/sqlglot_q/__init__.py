"""SQL to q/kdb+, as a sqlglot dialect plugin.

Installing the package registers the dialect with sqlglot through the
``sqlglot.dialects`` entry point, as ``q`` and as ``kdb``, so no import is
needed::

    import sqlglot
    sqlglot.transpile("SELECT a, SUM(b) FROM t GROUP BY a", write="q")
"""

from sqlglot_q.dialect import Q
from sqlglot_q.generator import QGenerator
from sqlglot_q.version import VERSION

__version__ = VERSION
__all__ = ["Q", "QGenerator", "__version__"]
