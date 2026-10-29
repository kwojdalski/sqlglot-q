# Changelog

## Unreleased

Added, both directions:

- Window functions: aggregates over a partition, running (`ROWS` and the
  default `RANGE`) and sliding frames, `ROW_NUMBER`, `RANK`, `DENSE_RANK`,
  `LAG` and `LEAD`, as `update ... by` stages; hand-written stages such as
  `update vwap:(sums px*qty)%sums qty by sym from ...` read back as windows.
- A release workflow: a `vX.Y.Z` tag publishes to PyPI by trusted publishing
  and makes a GitHub release.

Added, both directions, each checked in q 5 against DuckDB:

- Dates, timestamps and times: `DATE '2024-01-15'` is `2024.01.15`, fixed
  `INTERVAL`s are timespans, and `EXTRACT`/`YEAR`/`MONTH`/`DAY` are q's
  `` `year$ ``/`` `mm$ ``/`` `dd$ ``... casts.
- Time buckets: `TIME_BUCKET`, `DATE_TRUNC` (a day or less) and
  `FLOOR(x / n) * n` are `xbar`.
- As-of joins: `ASOF LEFT JOIN ... USING (sym, time)` (or `ON k = k AND
  l.time >= r.time`) is `aj`, with the right table sorted as aj needs.
- A group key q can name itself (`by 0D00:05 xbar time`) is left for q to
  name, rather than given a made-up `k0`.

Added:

- Reading qSQL: `sqlglot.parse_one(q, read="q")`. Covers what the writer
  emits and the common hand-written forms; refuses q with no faithful SQL.
- `tests/test_roundtrip.py`: SQL -> q -> SQL -> q and q -> SQL -> q -> SQL
  settle after one round, mean the same (DuckDB and q check), and give the
  same output under any hash seed. Examples come from q-lint's showcase and
  the public q corpus.

Fixed, found by those round trips:

- `SELECT pid AS id ... GROUP BY pid` lost the alias `id` in q.

Fixed, each found by running the translation in q against DuckDB:

- A scalar subquery (`WHERE a > (SELECT AVG(a) FROM t)`) compared against a
  one-row table and q raised 'length; it is now `first exec` of its column.
  In a select list it is parenthesised, which q needs ('from otherwise).
- `SELECT 1 AS one FROM t`, where every column is a constant, was refused by
  q ('rank); the constant now has a value per row.
- `ORDER BY`, `LIMIT` and `OFFSET` after a `UNION` were dropped.
- `x = NULL` matched the null rows in q, where SQL matches none; it is now
  refused with a pointer to `IS NULL`.

## 0.1.0

First release: a write-only q dialect for sqlglot, registered as `q` and `kdb`.

- SELECT with WHERE, GROUP BY, HAVING, ORDER BY, LIMIT/OFFSET, DISTINCT,
  subqueries, CTEs (inlined), UNION [ALL], and inner, left and cross joins on
  same-named keys.
- Predicates, arithmetic, CASE, CAST, COALESCE and the common scalar and
  aggregate functions, with SQL's null and rounding semantics where q's differ.
- INSERT, UPDATE, DELETE, CREATE TABLE and DROP TABLE.
- Output columns q cannot name in a select (orbital's `output_probability.0`)
  are renamed with `xcol`.
- `sqlglot-q` command-line tool.
