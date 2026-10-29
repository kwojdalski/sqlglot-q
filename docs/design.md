# Design

## Following sqlglot's conventions

The dialect is built the way sqlglot's own are, so it extends the same way:

- `Q` declares its behaviour as dialect attributes: names are case
  sensitive (`NORMALIZATION_STRATEGY`), nulls sort small (`NULL_ORDERING`),
  and `string_literals` is a dialect setting (`SUPPORTED_SETTINGS`), so
  `write="q, string_literals=string"` works as `"duckdb, ..."` does.
- `QGenerator` handlers are `<key>_sql` methods and a `TRANSFORMS` table,
  and its dispatch is built as sqlglot builds a generator's.
- Constructs SQL can rewrite into others go through sqlglot's own rewrites
  with `transforms.preprocess`: `QUALIFY` (`eliminate_qualify`) and `DISTINCT
  ON` (`eliminate_distinct_on`) become a window in a subquery, which the
  window stages then translate. `rewrites.py` adds q's own in the same shape:
  `GROUP BY ALL`, and renaming the `_w`-style names sqlglot's rewrites add,
  since a q name cannot start with an underscore.
- Functions sqlglot parses as anonymous are mapped in `FUNCTIONS`, which the
  reader reads backwards.

Statistics where q and SQL agree on nulls (`sdev`, `var`, `cor`, `cov`) are
plain `TRANSFORMS` entries. `MEDIAN` is not: q's `med` counts a null as the
smallest value (`med 1 2 4 0n` is 1.5), so it is `med x where not null x`.
`LOG(x)` with one argument is refused, as it is base 10 in some dialects and
e in others, and the tree no longer says which.

## Why the generator owns dispatch

sqlglot's `Generator` produces SQL, and its fallback for a node it has no
method for is SQL text: every `<key>_sql` it inherits writes SQL. For a SQL dialect that fallback is usually close
enough; for q it is wrong text that q either refuses or, worse, reads as
something else. `QGenerator` therefore builds its dispatch from `TRANSFORMS` and the
`<key>_sql` methods defined on it or on a subclass, never the ones inherited
from sqlglot's generator, and refuses everything else. Adding a construct means
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
| `ORDER BY x` (DuckDB: nulls last) | `` `x xasc t `` | q sorts nulls first ascending | `` {x iasc null x`x} (`x xasc t) ``: a stable re-sort on whether x is null |
| `CASE WHEN c THEN v END`, `NULLIF(a, b)` | `?[c;v;0N]` | a long null in a symbol column makes a mixed list | `?[c;v;v 0N]`: a null of v's own type |
| `LEAST(a, b)` | `a&b` | `&` does not skip a null, and `x^2` (fill an atom) is `'nyi` | `?[null a;b;?[null b;a;a&b]]` |
| `GREATEST(a, b)` | | | `a|b`: `|` skips a null as GREATEST does |
| `ROUND(x)`, `ROUND(x, n)` | | q has no round; SQL rounds half away from zero | `{signum[x]*floor 0.5+abs x} x` |
| `a // b` | `a div b` | `div` floors (-7 div 2 is -4); `//` truncates (-3) | `{signum[x*y]*div[abs x;abs y]}[a;b]` |
| `SUM(x) FILTER (WHERE c)` | | | `sum x where c`; `COUNT(*) FILTER` is `count i where c` |
| `a || b`, `CONCAT(a, b)` | `a,b` | `,` joins the lists, not each row's text | `` `$(string a),'string b ``; `,\:` and `,/:` against a literal; `||` guards nulls, CONCAT skips them |
| `JOIN u ON t.a = u.k` | | the keys are named differently | `` ej[`a;t;update a:k from u] ``: u keeps k and gains a copy named a |
| `RIGHT JOIN` | | | the LEFT JOIN the other way round |
| `SELECT * EXCLUDE (a)` | | | `delete a from select from t` |
| `DATE '2024-01-15'` | | a typed string in SQL | `2024.01.15` (timestamps `...D09:30:00.000000000`, times `09:30:00.000`) |
| `INTERVAL '5' MINUTE` | | | the timespan `0D00:05:00.000000000`; months and years have no fixed length and are refused |
| `EXTRACT(MINUTE FROM t)` | | | `` `uu$t `` (`year`, `mm`, `dd`, `hh`, `uu`, `ss`) |
| `TIME_BUCKET(INTERVAL '5 minutes', t)` | | q buckets from 2000.01.01, DuckDB from 2000-01-03 | `0D00:05:00.000000000 xbar t`, for widths that divide a day |
| `FLOOR(x / n) * n` | | | `n xbar x` |
| `ASOF LEFT JOIN q USING (sym, time)` | `aj[...;t;q]` | aj wants the right table sorted | `` aj[`sym`time;t;`sym`time xasc q] `` |
| alias `"p.0"` | `p.0:...` | not a name q can write in a select | placeholder, then `` `p.0 xcol `` |

## NULL

SQL has a third truth value, unknown, and q has none: a q null is the
smallest value of its type, so `0N<3`, `0N<>3` and `0N=0N` are all true, and
`not` turns false into true. Where SQL keeps rows on a condition (`WHERE`,
`HAVING`, `CASE WHEN`, `FILTER`), `truth.py` writes what makes the condition
*definitely true*, and a `NOT` as what makes its operand *definitely false*:

| SQL | q | why |
|---|---|---|
| `a > 3`, `a = 3`, `a >= 3` | `a>3` | a null is already not greater, nor equal to a literal |
| `a < 3`, `a <= 3` | `(not null a)&a<3` | `0N<3` is true |
| `a <> 3`, `a <> b` | `(not null a)&a<>3` | `0N<>3` is true |
| `a = b` | `(not null a)&a=b` | `0N=0N` is true |
| `NOT (a > 2)` | `(not null a)&a<=2` | `not 0N>2` is true |
| `NOT (p OR q)` | `NOT p, NOT q` | De Morgan, as a where list |
| `x NOT IN (1, NULL)` | `0b` | never true in SQL |
| `x NOT IN (SELECT ...)` | `... &not any null S` | never true once the subquery has a null |
| `b LIKE '%'` | `(not null b)&b like "*"` | a null symbol is ` , which `*` matches; other patterns need no guard |

`a IS NOT DISTINCT FROM b` and `a IS DISTINCT FROM b` need nothing: q's own
`=` and `<>` are null-safe (`0N=0N` is true). Reading q back follows from
that: hand-written `b<>`x` is true for a null `b`, so it reads as
`b IS DISTINCT FROM 'x'`, and `a=b` between two columns as `a IS NOT
DISTINCT FROM b`; under the generator's guard they are plain `<>` and `=`.
(Hand-written `a<3` still reads as SQL's `a < 3`, which drops the null rows
q's keeps.)

A guard goes only on a side that can be null and would make q say yes. In a
select list a comparison is still q's boolean, which cannot be null.

Aggregates and joins:

| SQL | q | why |
|---|---|---|
| `SUM(x)` | `sum {$[all null x;x 0N;x]} x` | `sum` of no values is 0, SQL's NULL |
| `MIN(x)`, `MAX(x)` | `first asc x where not null x`, `last asc x` | `min` of no values is `0W`; `min` and `max` refuse symbols |
| `MEDIAN(x)` | `med x where not null x` | `med` counts a null as the smallest value |
| `LENGTH(b)` | `{?[null x;0N;count each string x]} b` | the length of a null symbol is 0 |
| `JOIN ... ON t.k = u.k` | `ej[`k;(select from t where not null k);u]` | `0N=0N`: null keys would pair up |
| `-7 % 3` | `{x-y*signum[x%y]*floor abs x%y}[-7;3]` | q's `mod` floors (2); SQL's `%` keeps the sign (-1) |
| `CAST('5' AS BIGINT)` | `"J"$"5"` | `` `long$`5 `` is `'type` |

Reading q back is the same table backwards: plain q `sum x` is
`COALESCE(SUM(x), 0)`, and `x mod y` is `((x % y) + y) % y`.

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
| `x&y`, `x and y` on values | q's min, which keeps a null; only between conditions is it AND (`size=1 and side=0` is `size=(1 and side=0)`) |

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
- A symbol cannot start or end with a space (`` `$ `` trims), so in the
  default symbol mode `TRIM` has nothing to remove and a `CONCAT` that would
  start or end with one is refused; use `string_literals="string"`.
- In q a null symbol is the empty symbol, so SQL's `''` and `NULL` text are
  one value there.
- `CAST(x AS VARCHAR)` of a float is q's `string`, which prints with q's
  display precision (7 significant digits by default) and drops a `.0`:
  `0.0` is `"0"` where SQL writes `'0.0'`.
- `UNION` matches columns by name in q and by position in SQL.
- `SUM` of no non-null values is 0 in q and NULL in SQL; nulls sort first in
  q, last in DuckDB and Postgres by default.
- `ORDER BY` reads back from q as nulls first, q's order; the writer does
  not change q's null order to match a SQL `NULLS LAST`.
- CTEs are inlined, so a CTE used twice is computed twice. Recursive CTEs,
  window functions, `FULL`/`RIGHT` joins and correlated subqueries are
  refused.
