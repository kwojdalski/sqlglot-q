# Changelog

## Unreleased

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
