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
| alias `"p.0"` | `p.0:...` | not a name q can write in a select | placeholder, then `` `p.0 xcol `` |

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
- `UNION` matches columns by name in q and by position in SQL.
- `SUM` of no non-null values is 0 in q and NULL in SQL; nulls sort first in
  q, last in DuckDB and Postgres by default.
- `ORDER BY` reads back from q as nulls first, q's order; the writer does
  not change q's null order to match a SQL `NULLS LAST`.
- CTEs are inlined, so a CTE used twice is computed twice. Recursive CTEs,
  window functions, `FULL`/`RIGHT` joins and correlated subqueries are
  refused.
