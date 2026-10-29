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
`q` and as `kdb`, so no import is needed. It writes q; it does not read it.

## Install

```sh
pip install sqlglot-q          # or: uv add sqlglot-q
```

Requires Python 3.11+ and sqlglot 30+.

## What it translates

- `SELECT` with `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`/`OFFSET`,
  `DISTINCT`, subqueries, CTEs, `UNION [ALL]`, and inner, left and cross joins
  on same-named keys
- comparisons, `AND`/`OR`/`NOT`, `IN` (lists and subqueries), `BETWEEN`,
  `LIKE`/`ILIKE`, `IS [NOT] NULL`, arithmetic, `CASE`, `CAST`, `COALESCE`,
  `COUNT`/`SUM`/`AVG`/`MIN`/`MAX`, and common scalar functions
- `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `DROP TABLE`

Every construct is checked by running its q in q 5 and its SQL in DuckDB on
the same tables. Where q and SQL disagree (rounding, null counting, `LIMIT`
past the end, precedence), the q is written to give SQL's answer;
[docs/design.md](docs/design.md) lists each case. Anything without a faithful
q form raises `UnsupportedError` rather than producing approximate q.

## Options

String literals become symbols (`'abc'` is `` `abc ``), which is what text
columns usually are in kdb+. For char-vector columns:

```python
sqlglot.transpile("SELECT a FROM t WHERE s = 'ab'", write="q", string_literals="string")
# ['select a from t where s~\\:"ab"']
```

## Scoring scikit-learn models in kdb+ with orbital

[orbital](https://github.com/posit-dev/orbital) exports a fitted scikit-learn
pipeline as SQL. It compiles through ibis, which has no q backend, so export
DuckDB SQL and transpile that:

```python
import orbital, sqlglot

pipeline = orbital.parse_pipeline(model, features=orbital.types.guess_datatypes(df))
sql = orbital.export_sql("t", pipeline, dialect="duckdb")
q = sqlglot.transpile(sql, read="duckdb", write="q")[0]
```

Linear and logistic regression, decision trees, gradient boosting and random
forests are tested to score identically in q and DuckDB
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
