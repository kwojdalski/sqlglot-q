# Design

## Why the generator owns dispatch

sqlglot's `Generator` produces SQL, and its fallback for a node it has no
method for is SQL text. For a SQL dialect that fallback is usually close
enough; for q it is wrong text that q either refuses or, worse, reads as
something else. `QGenerator.sql` therefore dispatches only to its own
`q_<key>` methods and refuses everything else. Adding a construct means
adding a method, and the absence of one is a loud error.

## SQL and q, where they differ

Each of these was measured in q 5 and is pinned by `tests/integration`.

| SQL | Naive q | Why it's wrong | Emitted |
|---|---|---|---|
| `a*b+c` | `a*b+c` | q is right to left: `a*(b+c)` | `(a*b)+c` |
| `a/b` | `a/b` | `/` is over in q | `a%b` |
| `1E3` | `1E3` | q refuses `1E3` and `1.5E+3` | `1000.0` |
| `'abc'` | `"abc"` | text columns are symbols in q | `` `abc `` (`string_literals="string"` for chars) |
| `COUNT(x)` | `count x` | counts nulls too | `count x where not null x` |
| `CAST(2.5 AS BIGINT)` | `` `long$2.5 `` | q rounds half away from zero (3); SQL half to even (2) | `` `long${...half even...} x `` |
| `LIMIT 5` | `5#t` | `#` repeats rows when there are fewer | `5 sublist t` |
| `UPDATE t SET c=0 WHERE ...` | `update c:0 ...` | amending some rows needs the column's type ('type) | `c:(type c)$0` |
| `GROUP BY` | `select ... by` | the result is keyed, and keys come first | `0!select ... by ...`, reprojected to SQL's column order |
| `ORDER BY a, b DESC` | | | `` `a xasc `b xdesc r ``: stable sorts, least significant first |
| `AND` in WHERE | `&` | | a q where list, `where p, q`: same rows, and q filters progressively |
| `COALESCE(a,b)` | | | `b^a` |
| `x.y` qualifier | | q columns are unqualified | `y` |
| `DATE '2024-01-15'` | | a typed string in SQL | `2024.01.15` (timestamps `...D09:30:00.000000000`, times `09:30:00.000`) |
| `INTERVAL '5' MINUTE` | | | the timespan `0D00:05:00.000000000`; months and years have no fixed length and are refused |
| `EXTRACT(MINUTE FROM t)` | | | `` `uu$t `` (`year`, `mm`, `dd`, `hh`, `uu`, `ss`) |
| `TIME_BUCKET(INTERVAL '5 minutes', t)` | | q buckets from 2000.01.01, DuckDB from 2000-01-03 | `0D00:05:00.000000000 xbar t`, for widths that divide a day |
| `FLOOR(x / n) * n` | | | `n xbar x` |
| `ASOF LEFT JOIN q USING (sym, time)` | `aj[...;t;q]` | aj wants the right table sorted | `` aj[`sym`time;t;`sym`time xasc q] `` |
| alias `"p.0"` | `p.0:...` | not a name q can write in a select | placeholder, then `` `p.0 xcol `` |

## Window functions

SQL computes a select's windows after `WHERE` and before the projection,
each over its partition in its own order. q does that with `update ... by`,
one stage per window, over the filtered table sorted on the window's order:

    SELECT g, SUM(x) OVER (PARTITION BY g ORDER BY o) AS s FROM t WHERE c
    select g, s from (update s:last s by g,o from (update s:sums x by g from
        (`o xasc (select from t where c))))

| SQL | q, per partition |
|---|---|
| `SUM(x) OVER (PARTITION BY g)` | `sum x` (update broadcasts it) |
| running, `ROWS UNBOUNDED PRECEDING` | `sums x`, `avgs`, `maxs`, `mins`; `COUNT(*)` is `1+til count i` |
| running, the default `RANGE` | the same, then `update s:last s by g,o`: rows tied on `o` are peers and share the value at the last of them |
| `ROWS n PRECEDING` | `(n+1) msum x`, `mavg`, `mmax`, `mmin`, `mcount` |
| `ROW_NUMBER()`, `RANK()`, `DENSE_RANK()` | `1+til count i`, `1+o?o`, `sums differ o` |
| `LAG(x, n)`, `LEAD(x, n)` | `n xprev x`, `neg[n] xprev x` |

Windows in one expression that share a partition and order are one stage
(`update vwap:(sums px*qty)%sums qty by sym from ...`); others get a stage
each, named `w0`, `w1`, that the expression then uses. Refused: `LAG` and
`LEAD` with a default (SQL fills only past the edge, `^` would fill `x`'s own
nulls), frames that end anywhere but the current row, `NTILE` and the like,
and windows over a grouped result.

## Reading q

`parser.py` reads qSQL into sqlglot's tree. It is the generator run
backwards, plus the hand-written shapes of the same queries (a keyed
`select ... by`, `` `a xasc t ``, `sym=`x`), and it refuses what it cannot
state faithfully:

| q | Why it is refused |
|---|---|
| `s.city`, `` `.ns.t `` | a foreign-key lookup or a namespace in q, `table.column` in SQL |
| `x in z` | tests against z's items; SQL's `IN (z)` against z |
| `sym="EUR"` | `=` on a string compares characters; SQL equality is `~\:` |
| `where px:1` | an assignment, not a condition |
| `update ... from t`, `delete c from t` | return a new table; SQL's change one |
| a q keyword as a name | the writer could not name it back |

## Round trips

Both directions put the tree through `canonical.py` first: q's parentheses
are dropped (the tree holds the grouping) and every compound operand is
parenthesised one way. sqlglot prints a tree without adding parentheses for
precedence, so without this q's `a*b+c` (which is `a*(b+c)`) would read back
as SQL's `a * b + c`. With it, SQL -> q -> SQL -> q gives the same q twice
and q -> SQL -> q -> SQL the same SQL twice, which `tests/test_roundtrip.py`
checks over every test query and over qSQL from q-lint's showcase and the
public q corpus. The first trip may normalise (a keyed `select ... by`
becomes `0!select ... by`); the second must not.

## Known gaps

- Joins must be on equality of same-named columns. `LEFT JOIN` is `lj`,
  which takes the first match on the right: SQL's answer when the right side
  is unique on the keys, not otherwise.
- `aj` keeps the right table's value for a column both sides have; DuckDB's
  `ASOF JOIN` keeps both columns. `ASOF JOIN` without `LEFT` drops the rows
  with no match, which aj keeps, and is refused.
- A running `SUM` over a partition's leading nulls is 0 in q and NULL in
  SQL. A window's alias that is also a source column replaces it in q.
- `UNION` matches columns by name in q and by position in SQL.
- `SUM` of no non-null values is 0 in q and NULL in SQL; nulls sort first in
  q, last in DuckDB and Postgres by default.
- `ORDER BY` reads back from q as nulls first, q's order; the writer does
  not change q's null order to match a SQL `NULLS LAST`.
- CTEs are inlined, so a CTE used twice is computed twice. Recursive CTEs,
  window functions, `FULL`/`RIGHT` joins and correlated subqueries are
  refused.
