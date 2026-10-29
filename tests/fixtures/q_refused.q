/ q the reader must refuse rather than misread: each has no faithful SQL
/ form. The comment above a line says why.


/ q-lint examples/showcase.q
/ refused: = on a string compares characters: SQL equality is ~\:
select from trades where sym="EUR"
/ refused: = on a string compares characters: SQL equality is ~\:
select from trades where sym=""
/ refused: delete t from `. is the only delete of a name read
delete size from trades where size>1
/ refused: '~' has no SQL form here
select from trades where sym~`EUR
/ refused: unexpected end
select from trades where sym like `EUR*
/ refused: rows has no SQL form here
select from trades where sym=`EUR`USD
/ refused: expected 'from', found 'the end'
select px by sym trades
/ refused: a where phrase is read as conditions only
select from quotes where px:1
/ refused: first has no SQL form here
select first size by sym from trades

/ ~/q-corpus
/ refused: p.color is a foreign-key lookup in q, a table-qualified column in SQL
select sum qty by p.color from sp
/ refused: s.city and p.city are foreign-key lookups
select from sp where s.city=p.city
/ refused: a namespaced table has no SQL form
delete from `.finos.init.priv.hooks where fun in hookNames
/ refused: use is a q keyword, so the writer could not name the table back
select from use where date<enddate+1
/ refused: fnm in z tests against the items of z, SQL's IN (z) against z
select from cd where fnm in z
