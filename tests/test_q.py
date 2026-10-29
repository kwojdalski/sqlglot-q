"""The q text each construct becomes, in the Validator style sqlglot's own
dialect tests (and its plugins') use. These pin the text; whether the text
does what the SQL does is tests/integration's question."""

from __future__ import annotations

import unittest

import pytest
import sqlglot
from sqlglot import ErrorLevel
from sqlglot.errors import UnsupportedError

from sqlglot_q import Q


class Validator(unittest.TestCase):
    dialect = "q"

    def validate(self, sql: str, q: str, read: str | None = None, **opts) -> None:
        self.assertEqual(sqlglot.transpile(sql, read=read, write=self.dialect, **opts)[0], q)

    read: str | None = None

    def refuses(self, sql: str, message: str) -> None:
        with pytest.raises(UnsupportedError, match=message):
            sqlglot.transpile(sql, read=self.read, write=self.dialect)


class TestRegistration(Validator):
    def test_entry_points(self):
        # Both names come from the package's entry points, not from an import.
        for name in ("q", "kdb"):
            self.assertIs(sqlglot.Dialect.get_or_raise(name).__class__, Q)

    def test_reading_qsql(self):
        # The reader is tests/test_roundtrip.py's subject; this is the hookup.
        self.assertEqual(sqlglot.parse_one("select a from t", read="q").sql(), "SELECT a FROM t")
        with pytest.raises(sqlglot.errors.ParseError):
            sqlglot.parse_one("{x+1}", read="q")


class TestSelect(Validator):
    def test_projection(self):
        self.validate("SELECT * FROM t", "select from t")
        self.validate("SELECT a, b AS c FROM t", "select a, c:b from t")
        self.validate("SELECT t.a FROM t", "select a from t")
        self.validate("SELECT DISTINCT b FROM t", "select distinct b from t")

    def test_where_splits_top_level_and(self):
        self.validate("SELECT a FROM t WHERE a > 1 AND b = 2", "select a from t where a>1, b=2")
        self.validate(
            "SELECT a FROM t WHERE a > 1 AND (b = 2 OR c = 3)",
            "select a from t where a>1, (b=2)|(c=3)",
        )

    def test_operands_are_parenthesised(self):
        # q has no precedence: a*b+c would be a*(b+c).
        self.validate("SELECT a * b + c FROM t", "select (a*b)+c from t")
        self.validate("SELECT a - (b - c) FROM t", "select a-(b-c) from t")
        self.validate("SELECT a / b FROM t", "select a%b from t")
        # SQL's % takes the dividend's sign; q's mod floors.
        self.validate("SELECT a % b FROM t", "select {x-y*signum[x%y]*floor abs x%y}[a;b] from t")

    def test_literals(self):
        self.validate("SELECT a, 'abc', 'a b', '' FROM t", 'select a, `abc, `$("a b"), ` from t')
        self.validate("SELECT a, 'abc' FROM t", 'select a, "abc" from t', string_literals="string")
        self.validate(
            "SELECT a, 'a' FROM t", 'select a, enlist "a" from t', string_literals="string"
        )
        self.validate("SELECT a, TRUE, FALSE, NULL FROM t", "select a, 1b, 0b, 0N from t")
        # q refuses 1E3 and 1.5E+3; it reads 1000f and 1500.0.
        self.validate("SELECT a, 1E3, 1.5E+3, 0.5E0 FROM t", "select a, 1000.0, 1500.0, 0.5 from t")
        self.validate("SELECT a, -1.5 FROM t", "select a, -1.5 from t")

    def test_string_equality_in_string_mode(self):
        self.validate(
            "SELECT a FROM t WHERE b = 'xy'",
            'select a from t where b~\\:"xy"',
            string_literals="string",
        )

    def test_predicates(self):
        self.validate("SELECT a FROM t WHERE b IN ('x', 'y')", "select a from t where b in `x`y")
        self.validate("SELECT a FROM t WHERE a IN (1, 2)", "select a from t where a in 1 2")
        self.validate("SELECT a FROM t WHERE a IN (1)", "select a from t where a in enlist 1")
        self.validate(
            "SELECT a FROM t WHERE a BETWEEN 1 AND 3", "select a from t where a within 1 3"
        )
        self.validate("SELECT a FROM t WHERE d IS NULL", "select a from t where null d")
        self.validate("SELECT a FROM t WHERE d IS NOT NULL", "select a from t where not null d")
        self.validate(
            "SELECT a FROM t WHERE b LIKE 'a%_*'", 'select a from t where b like "a*?[*]"'
        )

    def test_aggregates(self):
        self.validate("SELECT COUNT(*) FROM t", "select count i from t")
        self.validate("SELECT COUNT(d) FROM t", "select count d where not null d from t")
        self.validate(
            "SELECT COUNT(DISTINCT d) FROM t",
            "select count distinct d where not null d from t",
        )
        self.validate(
            "SELECT SUM(a), AVG(b) FROM t", "select sum {$[all null x;x 0N;x]} a, avg b from t"
        )

    def test_group_by(self):
        self.validate(
            "SELECT b, SUM(a) FROM t GROUP BY b",
            "0!select sum {$[all null x;x 0N;x]} a by b from t",
        )
        self.validate(
            "SELECT SUM(a) AS s, b FROM t GROUP BY b",
            "select s, b from (0!select s:sum {$[all null x;x 0N;x]} a by b from t)",
        )
        self.validate(
            "SELECT b, COUNT(*) AS n FROM t GROUP BY b HAVING COUNT(*) > 1",
            "select from (0!select n:count i by b from t) where n>1",
        )
        self.validate(
            "SELECT b FROM t GROUP BY b HAVING SUM(a) > 6",
            "select b from (0!select h0:sum {$[all null x;x 0N;x]} a by b from t) where h0>6",
        )

    def test_order_and_limit(self):
        self.validate("SELECT a FROM t ORDER BY a DESC", "`a xdesc (select a from t)")
        self.validate(
            "SELECT a, b FROM t ORDER BY b, a DESC", "`b xasc (`a xdesc (select a, b from t))"
        )
        self.validate("SELECT a FROM t ORDER BY c", "select a from (`c xasc t)")
        self.validate("SELECT a FROM t LIMIT 2", "2 sublist (select a from t)")
        self.validate("SELECT a FROM t LIMIT 2 OFFSET 3", "(3;2) sublist (select a from t)")

    def test_joins(self):
        self.validate(
            "SELECT * FROM t JOIN u ON t.k = u.k",
            "select from ej[`k;(select from t where not null k);u]",
        )
        self.validate(
            "SELECT * FROM t JOIN u USING (k, j)",
            "select from ej[`k`j;(select from t where not null k, not null j);u]",
        )
        self.validate(
            "SELECT * FROM t LEFT JOIN u ON t.k = u.k",
            "select from {k:(),x;ej[k;y;z] uj y where not (k#y) in k#z}"
            "[`k;t;(select from u where not null k)]",
        )
        self.validate("SELECT * FROM t CROSS JOIN u", "select from t cross u")

    def test_subqueries(self):
        self.validate(
            "SELECT a FROM t WHERE a > (SELECT AVG(a) FROM t)",
            "select a from t where (not null (first exec x from (select x:avg a from t)))"
            "&a>(first exec x from (select x:avg a from t))",
        )
        self.validate("SELECT s FROM (SELECT a AS s FROM t)", "select s from (select s:a from t)")

    def test_constant_columns_get_a_value_per_row(self):
        # q refuses a select whose columns are all atoms ('rank).
        self.validate("SELECT 1 AS one FROM t", "select one:{(count x)#y}[i;1] from t")
        self.validate("SELECT 1 AS one, a FROM t", "select one:1, a from t")

    def test_union_order_and_limit(self):
        self.validate(
            "SELECT a FROM t UNION ALL SELECT a FROM u ORDER BY a LIMIT 2",
            "2 sublist (`a xasc ((select a from t),(select a from u)))",
        )

    def test_ctes_are_inlined(self):
        self.validate(
            "WITH w AS (SELECT a FROM t) SELECT a FROM w",
            "select a from (select a from t)",
        )

    def test_unnameable_output_columns_are_renamed(self):
        # orbital names its outputs output_probability.0; q cannot write that
        # in a select phrase, but xcol takes any symbol.
        self.validate(
            'SELECT a AS "p.0", b AS c FROM t',
            "`p.0`c xcol (select c0:a, c:b from t)",
        )


class TestFunctions(Validator):
    def test_case(self):
        self.validate(
            "SELECT CASE WHEN a > 1 THEN 1 WHEN a > 0 THEN 2 ELSE 3 END FROM t",
            "select ?[a>1;1;?[a>0;2;3]] from t",
        )
        self.validate("SELECT CASE b WHEN 1 THEN 2 ELSE 3 END FROM t", "select ?[b=1;2;3] from t")

    def test_casts(self):
        self.validate("SELECT CAST(a AS DOUBLE) FROM t", "select `float$a from t")
        self.validate(
            "SELECT CAST(a AS BIGINT) FROM t",
            "select `long${f:floor x+0.5;f-(x=f-0.5)&1=f mod 2} a from t",
        )
        from sqlglot_q.generator import TEXT_OF

        self.validate("SELECT CAST(a AS VARCHAR) FROM t", f"select `${TEXT_OF} a from t")

    def test_scalars(self):
        self.validate("SELECT COALESCE(a, b, 0) FROM t", "select 0^b^a from t")
        self.validate(
            "SELECT EXP(-a), LN(a), ABS(a) FROM t", "select exp neg a, log a, abs a from t"
        )


class TestTemporal(Validator):
    read = "duckdb"

    def validate(self, sql: str, q: str, **opts) -> None:
        super().validate(sql, q, read="duckdb", **opts)

    def test_literals(self):
        self.validate(
            "SELECT a FROM t WHERE d = DATE '2024-01-15'", "select a from t where d=2024.01.15"
        )
        self.validate(
            "SELECT a FROM t WHERE ts > TIMESTAMP '2024-01-15 09:30:00.5'",
            "select a from t where ts>2024.01.15D09:30:00.500000000",
        )
        self.validate(
            "SELECT a FROM t WHERE tm < TIME '09:30:00'",
            "select a from t where (not null tm)&tm<09:30:00.000",
        )

    def test_intervals_are_timespans(self):
        self.validate(
            "SELECT ts + INTERVAL 90 SECOND AS x FROM t",
            "select x:ts+0D00:01:30.000000000 from t",
        )
        self.refuses("SELECT d + INTERVAL '1.5' MONTH FROM t", "fixed intervals")

    def test_parts(self):
        self.validate(
            "SELECT EXTRACT(year FROM ts) AS y, MONTH(d) AS m, EXTRACT(minute FROM ts) AS u FROM t",
            "select y:`year$ts, m:`mm$d, u:`uu$ts from t",
        )

    def test_buckets_are_xbar(self):
        self.validate(
            "SELECT TIME_BUCKET(INTERVAL '5 minutes', ts) AS b FROM t",
            "select b:{$[14h=abs type y;y;x xbar y]}[0D00:05:00.000000000;ts] from t",
        )
        self.validate(
            "SELECT DATE_TRUNC('hour', ts) AS h FROM t",
            # A timestamp first: a timespan xbar of a date is no hour.
            "select h:0D01:00:00.000000000 xbar `timestamp$ts from t",
        )
        self.validate("SELECT FLOOR(a / 5) * 5 AS b FROM t", "select b:5 xbar a from t")
        # q counts buckets from 2000.01.01, DuckDB from 2000-01-03.
        self.refuses("SELECT TIME_BUCKET(INTERVAL '7 minutes', ts) FROM t", "divide a day")

    def test_asof_join_is_aj(self):
        q = "select from aj[`sym`time;t;`sym`time xasc q]"
        self.validate("SELECT * FROM t ASOF LEFT JOIN q USING (sym, time)", q)
        self.validate("SELECT * FROM t ASOF LEFT JOIN q ON t.sym = q.sym AND t.time >= q.time", q)
        self.refuses("SELECT * FROM t ASOF JOIN q USING (sym, time)", "ASOF LEFT JOIN")


class TestWindows(Validator):
    read = "duckdb"

    def validate(self, sql: str, q: str, **opts) -> None:
        super().validate(sql, q, read="duckdb", **opts)

    def test_whole_partition_is_the_aggregate(self):
        self.validate(
            "SELECT g, SUM(x) OVER (PARTITION BY g) AS s FROM t",
            "select g, s from (update s:sum {$[all null x;x 0N;x]} x by g from t)",
        )

    def test_running_rows_and_range(self):
        rows = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
        self.validate(
            f"SELECT SUM(x) OVER (PARTITION BY g ORDER BY o {rows}) AS s FROM t",
            # sums of a leading run of nulls is 0; SQL's running SUM is NULL.
            "select s from (update s:{?[0=sums not null x;x 0N;sums x]} x by g from "
            "({x iasc null x`o} (`o xasc t)))",
        )
        # The default frame is RANGE: rows tied on o share the last value.
        # DuckDB sorts o's nulls last, q first: {x iasc null x`o} moves them.
        self.validate(
            "SELECT SUM(x) OVER (PARTITION BY g ORDER BY o NULLS FIRST) AS s FROM t",
            "select s from (update s:last s by g,o from (update s:{?[0=sums not null x;x 0N;"
            "sums x]} x by g from (`o xasc t)))",
        )

    def test_ranking_offsets_and_moving(self):
        self.validate(
            "SELECT ROW_NUMBER() OVER (PARTITION BY g ORDER BY o NULLS FIRST) AS n FROM t",
            "select n from (update n:1+til count i by g from (`o xasc t))",
        )
        self.validate(
            "SELECT RANK() OVER (ORDER BY o DESC) AS r FROM t",
            "select r from (update r:1+o?o from (`o xdesc t))",
        )
        self.validate(
            "SELECT LEAD(x, 2) OVER (ORDER BY o NULLS FIRST) AS n FROM t",
            "select n from (update n:-2 xprev x from (`o xasc t))",
        )
        self.validate(
            "SELECT AVG(x) OVER (ORDER BY o NULLS FIRST ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) "
            "AS m FROM t",
            "select m from (update m:3 mavg x from (`o xasc t))",
        )

    def test_where_runs_first(self):
        self.validate(
            "SELECT SUM(x) OVER (PARTITION BY g) AS s FROM t WHERE x > 1",
            "select s from (update s:sum {$[all null x;x 0N;x]} x by g from "
            "(select from t where x>1))",
        )

    def test_reading_back(self):
        # q's sums of nothing but nulls so far is 0, SQL's running SUM NULL.
        sql = sqlglot.parse_one("update s:sums x by g from `o xasc t", read="q").sql("duckdb")
        self.assertEqual(
            sql,
            "SELECT *, COALESCE(SUM(x) OVER (PARTITION BY g ORDER BY o ASC NULLS FIRST "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0) AS s FROM t",
        )

    def test_refusals(self):
        self.refuses("SELECT LAG(x, o) OVER (ORDER BY o) FROM t", "literal offset")
        self.refuses(
            "SELECT SUM(x) OVER (ORDER BY o RANGE BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t",
            "RANGE frame",
        )


class TestStatements(Validator):
    def test_insert(self):
        self.validate("INSERT INTO t VALUES (1, 'x')", "`t insert (1;`x)")
        self.validate("INSERT INTO t VALUES (1, 'x'), (2, 'y')", "`t insert (1 2;`x`y)")
        self.validate(
            "INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')", "`t insert ([] a:1 2;b:`x`y)"
        )

    def test_update_and_delete(self):
        self.validate("UPDATE t SET a = 1", "update a:(type a)$1 from `t")
        self.validate("UPDATE t SET a = 1 WHERE b = 2", "update a:(type a)$1 from `t where b=2")
        self.validate("DELETE FROM t WHERE a = 1", "delete from `t where a=1")

    def test_ddl(self):
        self.validate(
            "CREATE TABLE t (a INT, b VARCHAR, c DOUBLE)",
            "t:([] a:`int$();b:`symbol$();c:`float$())",
        )
        self.validate("DROP TABLE t", "delete t from `.")


class TestRefusals(Validator):
    def test_unsupported_constructs_raise_by_default(self):
        self.refuses("SELECT a FROM t FULL JOIN u ON t.k = u.j", "FULL JOIN")
        self.refuses("SELECT a FROM t JOIN u ON k = j", "told apart")
        self.refuses("SELECT NTH_VALUE(a, 2) OVER (ORDER BY a) FROM t", "NthValue")
        self.refuses("SELECT count FROM t", "q keyword")
        self.refuses("SELECT a FROM db.t", "qualified table")
        self.refuses("SELECT DISTINCT a FROM t ORDER BY a + 1", "ORDER BY")
        self.refuses("SELECT a FROM t WHERE d = NULL", "IS NULL")

    def test_ignore_level_still_emits_q_that_refuses(self):
        q = sqlglot.transpile(
            "SELECT a FROM t FULL JOIN u ON t.k = u.j",
            write="q",
            unsupported_level=ErrorLevel.IGNORE,
        )[0]
        self.assertIn("'unsupported", q)
