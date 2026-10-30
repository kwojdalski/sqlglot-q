# Changelog

## Unreleased

A third sweep of 50 problems (`tests/integration/test_sql_sweep3.py`,
`test_statements.py`, `test_string_mode.py`, `tests/test_cli.py`).

Wrong answers, silently:

- `x // y` of a float truncated it; DuckDB's `//` of a float divides.
- String mode: `UPDATE s SET w = 'up' WHERE ...` spread the string's
  characters over the rows.
- Symbol mode: a literal with leading or trailing spaces lost them (`` `$ ``
  trims), so `s = ' x'` found `x`: refused.
- Reading q: `t lj `k xkey q` (and `n!q`) as a LEFT JOIN gave every match
  where `lj` takes the first: refused unless `q` is a grouping by `k`.

q errors, and round trips that did not settle: `FETCH FIRST n ROWS`; a window
in a `CASE`; `WITH` over a set operation, an `INSERT`, `UPDATE` or `DELETE`;
a join of an unnamed query; `WHERE f OR c` of a boolean column; a constant
`ORDER BY` term; `CAST('true' AS BOOLEAN)`; `LEVENSHTEIN` of NULL; row values
of a long and a float. String mode: a NULL into a text column, `SET w =
NULL`, text into an added column, a `CASE` of a text column with no `ELSE`,
`LAG`/`LEAD` text defaults, `= 1` (`~'`), `ANY_VALUE`, a scalar subquery's
text.

Now translated: `(a, b) = / <> / IN ((...), ...)`, `UNION ALL BY NAME` (q's
`uj`), `TRY_CAST` to dates, timestamps and times, `ISFINITE`, `ROUND_EVEN`,
`SIGNBIT`, `WEEKOFYEAR`, `EPOCH_US`/`EPOCH_NS`, `MAKE_TIME`, `PREFIX`/
`SUFFIX`, `UNICODE`, `LEVENSHTEIN`, `BIT_LENGTH`, `STRING_SPLIT`,
`TRUNC(x, d)`.

Reading q: `lj`/`ij` with a keyed grouping, `uj`, `aj[...]`/`ej[...]` on
their own, a parenthesised join, `"D"$x` (and `"J"$`, `"P"$`) as `TRY_CAST`,
`count each string x`, and functional selects, `?[t;c;b;a]`.

The command reads q too: `sqlglot-q --from-q [--write dialect]`, and runs as
`python -m sqlglot_q`.

A second sweep of 101 problems, each with its case in
`tests/integration/test_sql_sweep2.py`, `test_statements.py` (statements run
in q and DuckDB, the table they leave compared), the string-mode and
calendar files, or `tests/test_q.py`.

Wrong answers, silently:

- `UPDATE ... FROM` and `DELETE ... USING` read `t.k = u.k` as `k=k`: every
  row (refused); `CREATE TABLE IF NOT EXISTS` replaced an existing table.
- A join's sides sharing a column name (`SELECT t.g FROM t CROSS JOIN u`,
  `ej`'s non-key columns): q keeps the right side's. Each side now leaves out
  what the query reads from another, and the reader qualifies it back.
- `LEFT(s, -n)`; `COALESCE(n <= 2, FALSE)` was true on a null `n`; and reading
  q, `not (not null n)&n<=2` as `NOT (n <= 2)`, which drops the null rows q
  keeps.

q errors on SQL that should translate:

- `INSERT ... SELECT` by position, `INSERT (cols) SELECT`, a `NULL` into a
  symbol column, one column of several rows; `UPDATE` changing a column's
  type (`v / 3`) and `SET c = NULL`.
- Ranks of several order keys; `n AS "my col"`.
- String mode: joins on a text key, any constant text in a select, text
  windows (`LAST_VALUE`, `NTH_VALUE`, `COUNT(DISTINCT)`), row values,
  `GREATEST`/`LEAST` and `NULLIF` of text columns, a grouping set's text key.

Now translated: correlated `UPDATE ... SET c = (SELECT ...)`, `ALTER TABLE`
(add, rename, drop columns; rename), `CREATE VIEW`, `VALUES` tables, `ROLLUP`,
`CUBE`, `GROUPING SETS`, named windows, windows over a grouped result,
`ORDER BY` after `INTERSECT`/`EXCEPT`, `LAST_VALUE`/`NTH_VALUE` to the current
row, `COUNT(DISTINCT) OVER`, `SUM(DISTINCT) OVER`, `FIRST`/`LAST OVER`,
`STRING_AGG`/`ARRAY_AGG` with `ORDER BY` or `DISTINCT`, `FILTER` on any
aggregate, `IS TRUE`/`IS FALSE`, `LIKE ANY`, `REGEXP_REPLACE` of plain text,
`COUNT(DISTINCT a, b)`, `UNNEST` of a list; math (trig, `ATAN2`, `DEGREES`,
`CBRT`, `FACTORIAL`, `GCD`, `LCM`, `EVEN`, `ISINF`), bits (`& | xor << >> ~`,
`BIT_COUNT`, `BIT_AND`/`OR`/`XOR`), `TRY_CAST`, `CONCAT_WS`, `TRANSLATE`,
`MD5`, `KURTOSIS`, `SKEWNESS`, `PRODUCT`, `QUANTILE_DISC`, `... WITHIN GROUP`;
dates (`DATE_ADD`/`DATE_SUB`, `DATE_PART`, `DAYOFMONTH`, `WEEKDAY`, `ISODOW`,
`YEARWEEK`, `MONTHNAME`, `MAKE_TIMESTAMP`, `STRPTIME`, `CURRENT_TIME`,
`DATE_DIFF` in weeks, milliseconds and microseconds, composite intervals,
`EXTRACT` of ISO parts, decades, centuries, millennia and microseconds,
`DATE_TRUNC` to a decade, century or millennium).

Reading q: `sym in `a`, an aggregate beside the rows' columns (`OVER ()`),
`div`, `select[m n]`, a sort or `sublist` of a table, `fby` (as `QUALIFY`),
running functions in a select over a sorted table, `x::select ...` views.

A sweep of 50 problems, each with its case in
`tests/integration/test_sql_sweep.py` (or the string-mode and calendar
files) and round-tripped.

Wrong answers, or q errors, on SQL that should translate:

- `GROUP BY` with nothing computed over a key-only source raised `'rank`;
  `HAVING` took a subquery's aggregate for the group's.
- `x IN (SELECT ...)` followed by another condition raised `'type`: q's
  `exec ... from` took the next constraint into its `from`. And a long
  `IN` floats raised `'type`.
- `DATE_TRUNC('day'/'hour', date)` and `TIME_BUCKET` of a DATE were
  2000-01-01: a timespan `xbar` of a date counts its days as nanoseconds.
- `ts + INTERVAL n MONTH` raised `'type` on a timestamp.
- Running, sliding and framed `MIN`/`MAX` of text raised `'type` (now by
  ranks, `iasc iasc x`).
- `LIKE '%%'` was q's `**` (`'nyi`); `SPLIT_PART` on `''` raised `'length`.
- Text functions of a literal (`REPLACE('abc', ...)`, `LENGTH('abc')`)
  walked its characters; a constant piece of a concatenation was paired
  row by row.
- `UNION`/`INTERSECT` with a `SELECT` of constants raised `'mismatch`.
- String mode: `w < v`, `BETWEEN`, `COALESCE(w, v)` and `w = (SELECT ...)`
  raised `'length`; sliding text `MIN`, `MAX(w) OVER ()` and `FIRST_VALUE`
  of text raised `'type`/`'length`.
- `CAST(ts AS VARCHAR)` was q's `2024.01.15D09:30:00.000000000`, and a
  whole float lost its `.0`.

Now translated: `t.*` of one table, `LIKE` a per-row pattern, `(a, b) IN
(SELECT ...)`, `ARRAY_AGG`/`LIST`, `FIRST`/`LAST`, `HOUR`/`MINUTE`/`SECOND`,
`EPOCH` and `EXTRACT(epoch|week)`, `DATE_DIFF` in hours, minutes, seconds and
quarters, `DATE_TRUNC` of a DATE literal.

Reading q:

- `abs -3` read as `abs - 3` (q's `-3` after a space is a number);
  `count t` as a bare `COUNT(*)`; `select l:n by g` as an ungrouped column;
  a window over a join, `inter` as a `FROM` source, an aggregate in an
  update without `by` (it lost its `OVER ()`) and string mode's
  `enlist "abc"` did not read back.
- Newly read: `union`, `sum[x]`, `delete ... from` a table value, `first`
  and `last` of a select, `exec`. q's row index `i` is refused.

Fixed, where q answered something other than SQL with no error
(`tests/integration/test_sql_more.py`):

- A `LEFT JOIN` kept only the first match on the right (q's `lj`); it now
  keeps every match (`ej` and the unmatched left rows).
- A correlated subquery was evaluated once, its `b.g = a.g` read as `g = g`.
  A correlated `EXISTS` on `inner.k = outer.k` is now a semi-join (and
  `NOT EXISTS` keeps a null key, as SQL's does); every other correlated
  subquery is refused.
- `t.n` and `u.n` across a join, a self-join or a cross join were one
  column in q; a LEFT JOIN's right key was the left's. Both are refused.
- Running `MAX`/`MIN` over leading nulls were q's infinities, and sliding
  `SUM`/`MAX`/`MIN` frames over nulls counted them as 0 or as the least.
- `CAST(d AS VARCHAR)` wrote a date `2024.01.31` and a boolean `1`; SQL
  writes `2024-01-31` and `true`.
- `UNION` of differently named columns raised `'mismatch`.
- Reading q: `s like "a_x"` read as SQL `LIKE 'a_x'`, where `_` is any
  character; it is now escaped.
- `DATE_TRUNC('quarter', d)` of a NULL date was 2000-01-01 (`3 xbar 0Nm`
  is `2000.01m`).
- `CAST(2.5 AS INTEGER)` was 2: a literal `2.5` is a DECIMAL, which SQL
  casts half away from zero (a DOUBLE column is still cast half even).
- Reading q: `select g from (`k xasc update k:e from t)` lost `e` and
  ordered by an undefined column; it is now `ORDER BY e`.
- In string mode, `CONCAT(w, '-', v)` and `w || '-' || v` raised `'length`:
  q read `w,\:"-",'v` right to left, joining `"-"` to `v` first.
- `CONCAT(s, NULL)` raised `'length`; a NULL adds nothing to it. `s || NULL`
  is refused.

Added, each checked in q 5 against DuckDB and round-tripped:

- `ORDER BY` an expression the select does not have, as a hidden column of
  a subquery that is sorted on and left out (and with `GROUP BY`, as an
  item of the grouped subquery).
- `FULL JOIN` on keys of one name: every match, and both sides' rows with
  none (a NULL key matches nothing). The key is read as `USING`'s.
- A scalar subquery correlated on one key (`(SELECT AVG(n) FROM t AS b
  WHERE b.g = a.g)`), as its aggregate grouped by the key and LEFT JOINed:
  NULL where no row matches, 0 for `COUNT`.
- `INTERSECT ALL` and `EXCEPT ALL`, counting each row's copies.
- `BOOL_AND` and `BOOL_OR`, skipping the rows where the condition is
  unknown (their NULL over no known rows is q's `0b`).
- `ROWS` frames that reach ahead or end before the current row (`1
  PRECEDING AND 1 FOLLOWING`, `CURRENT ROW AND UNBOUNDED FOLLOWING`) for
  `SUM`, `AVG`, `MIN`, `MAX` and `COUNT`.
- `REGEXP_MATCHES`, `~` and plain-text `SIMILAR TO`, where the regex is
  one `LIKE` can state (literal text, `.`, `.*`, `^`, `$`, escapes; the `i`
  flag as `ILIKE`); `CHR`; `STRFTIME` with `%Y %m %d %H %M %S %j`.
- Reading q: a plain `a inter b` (no `distinct`) is refused; it keeps the
  first's copies, which is neither `INTERSECT` nor `INTERSECT ALL`.

- `x > ANY (SELECT ...)` and `x > ALL (SELECT ...)` (and `>=`, `<`, `<=`)
  in `WHERE` and `HAVING`, as a comparison with the subquery's `MIN` or
  `MAX`; `ALL` is true over no rows and never over a NULL.
- An inner join's `ON` beyond its key equalities (`ON t.g = u.g AND t.n >
  2`) is a filter after the join, and `FROM t, u WHERE t.g = u.g` is the
  join on `g`.
- `TRIM`, `LTRIM` and `RTRIM` of given characters, `SPLIT_PART` and
  `ASCII`, in both string modes; `CURRENT_DATE` and `CURRENT_TIMESTAMP` as
  `.z.D` and `.z.P`.

- `scalars.py`, one table of q lambdas read both ways: `TRUNC`, `LPAD`,
  `RPAD`, `REPEAT`, `STRPOS`/`POSITION`/`INSTR`, `LAST_DAY`, `MAKE_DATE`,
  `DAYNAME`, `WEEK`, `DATE_DIFF` in months and years, and the aggregates
  `ARG_MAX`, `ARG_MIN`, `MODE` and `QUANTILE_CONT`, in both string modes.
- `SUM`/`AVG`/`MIN`/`MAX(DISTINCT x)`; `INTERSECT` and `EXCEPT`; `date +
  INTERVAL n MONTH` (and `QUARTER`, `YEAR`, clamped to the month's end);
  `DATE_TRUNC('week', ...)`; `PI()`; `LIKE ... ESCAPE`; `= ANY` and `<> ALL`
  over a subquery; `SELECT *, e AS a`; `ROWS n PRECEDING` without `AND
  CURRENT ROW`.

Fixed and added earlier in this release, each checked in q 5 against DuckDB
and round-tripped:

- String mode: null tests, ORDER BY, comparisons with text, MIN/MAX, CASE,
  COALESCE, column = column, CONCAT, ||, LENGTH and STRING_AGG over q
  strings (`tests/integration/test_string_mode.py`); it was largely broken.
- IN over mixed numbers ('type in q); NTILE's bucket sizes; SIGN(NULL) (q's
  signum of a null is -1); a running SUM over a leading run of nulls.
- DATE_TRUNC to month/quarter/year; EXTRACT DOW/DOY/QUARTER/MILLISECOND;
  DATE_DIFF in days; REPLACE, SUBSTRING, REVERSE; uncorrelated EXISTS;
  FIRST_VALUE, LAST_VALUE, NTILE, PERCENT_RANK, CUME_DIST; LAG and LEAD
  with a default.
- Reading hand-written q: comparisons keep q's meaning on nulls (`where
  n<3` keeps the null rows); select[n], count x, wavg, wsum, deltas, prev,
  next, sums, signum and aggregates in a where phrase read as their SQL
  (`tests/integration/test_reading.py`, which runs q against DuckDB).
- Harness: durations compare as seconds; q string columns.

Fixed, each found by running q against DuckDB on data with nulls, null join
keys and one-row tables (`tests/integration/test_nulls.py`):

- `WHERE a < 3`, `a <= 3`, `a <> 3`, `a = b` and `NOT (...)` kept null
  rows: q has no unknown, and a null is its smallest value. Conditions in
  `WHERE`, `HAVING`, `CASE WHEN` and `FILTER` now follow SQL's three-valued
  logic (`truth.py`), guarding only the sides that need it.
- `x NOT IN (..., NULL)` and `x NOT IN (SELECT ...)` over a null kept rows
  SQL drops; `x IN (SELECT ...)` matched a null to a null.
- `b NOT LIKE 'x%'` lost its NOT (sqlglot's `Like(negate=True)`).
- `SUM` of no values was 0 and `MIN`/`MAX` were `0W`/`-0W`; SQL's are NULL.
  `MIN`/`MAX` of text raised `'type`.
- Joins paired up null keys (`0N=0N`).
- `LENGTH(NULL)` was 0; `CAST('5' AS BIGINT)` raised `'type`; `-7 % 3` was 2.
- Reading q: plain `sum x` is now `COALESCE(SUM(x), 0)` (q's sum of nothing
  is 0); nested parentheses were not all removed when reading.
- The test harness could not build a one-row table, or read q's `inf`.
- A second sweep (`tests/integration/test_nulls_more.py`: windows, dates and
  timestamps over nulls, unions, grouping) found no more mismatches, and two
  refusals worth lifting: `IS [NOT] DISTINCT FROM`, which is q's own `=` and
  `<>`, and `STRING_AGG` (`sv`, skipping nulls). Reading q, `<>` and `=`
  between two columns are now the null-safe SQL operators they are.
- The harness wrote typed nulls (`0Nd`) inside temporal vectors, which q
  refuses.

Aligned with sqlglot's dialect conventions:

- `string_literals` is a dialect setting: `write="q, string_literals=string"`.
- `Q` declares `NORMALIZATION_STRATEGY` (case sensitive) and `NULL_ORDERING`.
- Generator handlers are `<key>_sql` methods and a `TRANSFORMS` table, as in
  any sqlglot generator, so a subclass extends them; `FUNCTIONS` maps
  functions sqlglot does not know (UDFs) to q, both ways.
- sqlglot's own rewrites run through `transforms.preprocess`.

Added, both directions:

- `QUALIFY` and `DISTINCT ON` (sqlglot's `eliminate_qualify` and
  `eliminate_distinct_on`), and `GROUP BY ALL`.
- `MEDIAN` (skipping nulls, which q's `med` does not), `STDDEV`,
  `STDDEV_POP`, `VARIANCE`, `VAR_POP`, `CORR`, `COVAR_POP`, `COVAR_SAMP`,
  `COUNT_IF`, `ANY_VALUE`.
- `LOG(b, x)`, `LOG10`, `LOG2`; `STARTS_WITH`, `ENDS_WITH`, `CONTAINS`,
  `LEFT`, `RIGHT`.
- Keyed tables: `PRIMARY KEY` columns are a keyed table; `INSERT OR REPLACE`
  and `ON CONFLICT DO UPDATE SET c = EXCLUDED.c` are `upsert`; `TRUNCATE`.

Fixed, each found by running q against DuckDB:

- `ORDER BY` ignored where SQL puts nulls: DuckDB sorts them last, q first,
  so a sort on a column with nulls came back in the wrong order. `NULLS
  FIRST/LAST` and DuckDB's default are now honoured, in windows too.
- The reader read every q `|` and `&` as OR and AND, but on values they are
  max and min: `select a|b` came back as `a OR b`. Only between conditions
  are they OR and AND now; `x&y` on values (min, which keeps nulls) is
  refused, and so is `size=1 and side=0`, which q reads as
  `size=(1 and side=0)`.

Added, both directions:

- `CASE` without `ELSE` and `NULLIF`, with a null of the branch's type.
- `GREATEST`, `LEAST`, `ROUND`, `POWER`, `SIGN`, integer `//` (truncating,
  as SQL, where q's `div` floors).
- Aggregates with `FILTER (WHERE ...)`.
- `TRIM`/`LTRIM`/`RTRIM`, `CONCAT` and `||` (which is NULL when a side is).
- Joins on differently named keys, `RIGHT JOIN`, `FROM t, u`, and
  `SELECT * EXCLUDE (...)`.

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
