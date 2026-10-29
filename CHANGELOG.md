# Changelog

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
