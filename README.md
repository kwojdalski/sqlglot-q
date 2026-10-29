# sqlglot-q

SQL to q/kdb+, as a [sqlglot](https://github.com/tobymao/sqlglot) dialect
plugin. Install it and sqlglot can write q:

```python
import sqlglot

sqlglot.transpile(
    "SELECT b, SUM(a) AS s FROM t WHERE c > 1 GROUP BY b ORDER BY s DESC LIMIT 5",
    write="q",
)
# ['5 sublist (`s xdesc (0!select s:sum a by b from t where c>1))']
```

The dialect is registered through sqlglot's `sqlglot.dialects` entry point as
`q` and as `kdb`, so no import is needed. It also reads qSQL back into SQL:

```python
sqlglot.parse_one("select s:sum a by b from t where c>1", read="q").sql()
# 'SELECT b, SUM(a) AS s FROM t WHERE c > 1 GROUP BY b'
```

The reader covers qSQL queries and statements, the q the writer produces and
the common hand-written shapes of the same thing. q with no faithful SQL form
(foreign-key lookups like `s.city`, `x in z` against a variable, `=` on a
string, lambdas) is a `ParseError`, never a guess. Converting both ways and
back settles: after one round trip, another changes nothing.

## Install

```sh
pip install sqlglot sqlglot-q      # or: uv add sqlglot sqlglot-q
```

Until the first PyPI release, or for the latest commit, install from GitHub:

```sh
pip install sqlglot "sqlglot-q @ git+https://github.com/kwojdalski/sqlglot-q"
pip install -e path/to/sqlglot-q   # a local clone, editable
```

It needs Python 3.11+ and sqlglot 30+. Installing it is all the setup there
is: sqlglot finds the dialect through the package's entry point the first
time `"q"` is asked for. To check:

```sh
python -c "import sqlglot; print(sqlglot.transpile('SELECT a FROM t WHERE b > 1', write='q'))"
# ['select a from t where b>1']
```

**Use the pure-Python sqlglot, not `sqlglot[c]`.** The compiled build
refuses subclasses of its generator, so with it installed every transpile to
q fails with `TypeError: interpreted classes cannot inherit from compiled`.
This applies to every sqlglot plugin dialect. If you have it, remove the
compiled part with `pip uninstall sqlglotc`.

Running the q that comes out needs q itself (kdb+, any edition with a
licence, such as the free personal one). The translation does not.

## What it translates

- `SELECT` with `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`/`OFFSET`,
  `DISTINCT`, subqueries, CTEs, `UNION [ALL]`, and inner, left and cross joins
  on same-named keys
- comparisons, `AND`/`OR`/`NOT`, `IN` (lists and subqueries), `BETWEEN`,
  `LIKE`/`ILIKE`, `IS [NOT] NULL`, arithmetic, `CASE`, `CAST`, `COALESCE`,
  `COUNT`/`SUM`/`AVG`/`MIN`/`MAX`, and common scalar functions
- `GREATEST`, `LEAST`, `NULLIF`, `ROUND`, `POWER`, `SIGN`, integer `//`,
  `TRIM`/`LTRIM`/`RTRIM`, `CONCAT` and `||`, aggregates with `FILTER (WHERE
  ...)`, and `CASE` without `ELSE`
- `ORDER BY ... NULLS FIRST/LAST`, and DuckDB's default of nulls last
- joins on differently named keys (`ON t.a = u.k`), `RIGHT JOIN`,
  `FROM t, u`, and `SELECT * EXCLUDE (...)`
- `QUALIFY` and `DISTINCT ON` (through sqlglot's own rewrites), `GROUP BY ALL`
- `MEDIAN`, `STDDEV`/`VARIANCE` (sample and population), `CORR`, `COVAR_*`,
  `COUNT_IF`, `ANY_VALUE`, `LOG(b, x)`/`LOG10`/`LOG2`, `STARTS_WITH`,
  `ENDS_WITH`, `CONTAINS`, `LEFT`, `RIGHT`
- keyed tables: `PRIMARY KEY` is a keyed table, and `INSERT OR REPLACE` and
  `ON CONFLICT DO UPDATE SET c = EXCLUDED.c` are `upsert`; `TRUNCATE`
- `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `DROP TABLE`
- dates, timestamps and times (`DATE '2024-01-15'` is `2024.01.15`), fixed
  intervals as timespans, `EXTRACT`/`YEAR`/`MONTH`/...
- time buckets: DuckDB's `TIME_BUCKET`, `DATE_TRUNC` to a day or less, and
  `FLOOR(x / n) * n` are q's `xbar`
- as-of joins: `ASOF LEFT JOIN ... USING (sym, time)` is q's `aj`
- window functions: `SUM`/`AVG`/`MIN`/`MAX`/`COUNT` over a partition,
  running (`ROWS` or the default `RANGE`) or sliding (`n PRECEDING`), and
  `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `LAG`, `LEAD`, as q's `update ... by`
  with `sums`, `msum`, `xprev` and friends

```python
sqlglot.transpile(
    "SELECT sym, TIME_BUCKET(INTERVAL '5 minutes', time) AS bar, AVG(px - bid) AS spread "
    "FROM trades ASOF LEFT JOIN quotes USING (sym, time) "
    "WHERE time >= TIMESTAMP '2024-01-15 09:30:00' GROUP BY sym, bar",
    read="duckdb",
    write="q",
)
# ['0!select spread:avg px-bid by sym, bar:0D00:05:00.000000000 xbar time
#   from aj[`sym`time;trades;`sym`time xasc quotes]
#   where time>=2024.01.15D09:30:00.000000000']
```

Every construct is checked by running its q in q 5 and its SQL in DuckDB on
the same tables. Where q and SQL disagree (rounding, null counting, `LIMIT`
past the end, precedence), the q is written to give SQL's answer;
[docs/design.md](docs/design.md) lists each case. Anything without a faithful
q form raises `UnsupportedError` rather than producing approximate q.

## Options

String literals become symbols (`'abc'` is `` `abc ``), which is what text
columns usually are in kdb+. For char-vector columns, use the dialect
setting, as for any sqlglot dialect:

```python
sqlglot.transpile("SELECT a FROM t WHERE s = 'ab'", write="q, string_literals=string")
# ['select a from t where s~\\:"ab"']
```

`string_literals="string"` as a keyword argument does the same.

## Extending the dialect

`Q` is extended the way any sqlglot dialect is: subclass it and its
`Generator`. A function sqlglot does not know (a UDF) is mapped to q in
`FUNCTIONS`, and the reader maps it back; a node gets a `<key>_sql` method
or a `TRANSFORMS` entry:

```python
from sqlglot import exp
from sqlglot_q import Q


class MyQ(Q):
    class Generator(Q.Generator):
        FUNCTIONS = {**Q.Generator.FUNCTIONS, "VWAP": "wavg"}
        TRANSFORMS = {**Q.Generator.TRANSFORMS, exp.Cos: lambda self, e: f"cos {self.sql(e.this)}"}


sqlglot.transpile("SELECT VWAP(qty, px) AS w, COS(a) AS c FROM t", write=MyQ)
# ['select w:wavg[qty;px], c:cos a from t']
```

Only methods defined on `QGenerator` or a subclass are used: one inherited
from sqlglot's SQL generator would write SQL, so a node without a q handler
is an `UnsupportedError` instead.

## Scoring scikit-learn models in kdb+ with orbital

[orbital](https://github.com/posit-dev/orbital) exports a fitted scikit-learn
pipeline as SQL, so a model trained in Python can be scored inside kdb+.

```sh
pip install sqlglot-q orbital scikit-learn pandas
```

orbital compiles through ibis, which has no q backend, so passing
`dialect="q"` to orbital does not work. Export DuckDB SQL and transpile that:

```python
import orbital, sqlglot

pipeline = orbital.parse_pipeline(model, features=orbital.types.guess_datatypes(X))
sql = orbital.export_sql("t", pipeline, dialect="duckdb")
q = sqlglot.transpile(sql, read="duckdb", write="q")[0]
```

`q` is one q expression over a table `t` with the model's feature columns;
run it in a q process that holds `t`, and it returns one row of scores per
row of `t`. Output columns orbital names `output_probability.0` and so on
keep those names (q renames them with `xcol` after the select).

[`examples/orbital_demo.py`](examples/orbital_demo.py) does the whole round
trip. It fits a logistic regression on iris, translates it, scores the data
in q, and compares the result with scikit-learn's own `predict_proba`. The
probabilities agree to 2e-16 and every label matches. Linear and logistic
regression, decision trees, gradient boosting and random forests are also
tested to score identically in q and DuckDB
(`tests/integration/test_orbital.py`).

## Command line

```sh
sqlglot-q "SELECT a FROM t WHERE b IN ('x', 'y')"
# select a from t where b in `x`y
echo "SELECT TOP 5 a FROM t" | sqlglot-q --read tsql
# 5 sublist (select a from t)
```

## Development

```sh
uv sync
uv run pytest                    # integration tests need q on PATH
uv run --group orbital pytest    # plus the orbital scoring tests
```
