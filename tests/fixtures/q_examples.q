/ qSQL the reader reads, each line one statement. tests/test_roundtrip.py
/ converts every line q -> SQL -> q -> SQL and wants the second round to
/ change nothing. Sources: q-lint's examples/showcase.q and ~/q-corpus.


/ q-lint examples/showcase.q
select size from quotes
select px from quotes
select from quotes where size>100
select from trades where sym=sym
select from trades where size=1 and side=0
select from quotes where px=1
select max px by px from trades
select top:max px by px from trades
select from trades where sym like "a*b*"
select from trades where sym like "ab*"
select from trades where sym like "a*b"
select from trades where sym like "EUR*"
select from trades where sym~\:`EUR
select from trades where sym in `EUR`USD
select from trades where size=1

/ KxSystems_automl
select from modelTab where apply

/ KxSystems_cookbook
select from data where date=`date$sourcetime
select sum usage by custtype,region from t
select month,meterid,cost:price*usage from tobill
select from tbl where not null Volume

/ KxSystems_kdb
select from t
select sum q,sum x,sum xt,sum xd,n:count i by v,u from l
select from trade where date=d
select avg size from trade
select avg size by date from trade
select avg size by ex from trade
select time,price,size from trade where date=d,sym=s
select sum size by sym from trade where ex=exch
select price1:price,size1:size from cc where corr=1h
select time,sym,price,size from cc where corr in 7 8h
select from cc where corr in 7 8 12h
select sum size by sym from trade
select c,ci,t,maybe,empty,res,j10,j12,ipa,mw,mdot,rules,gr,ndv,dchar from info
select from info where maybe

/ KxSystems_ml
select repPt,clust:clust0 from oldRep
select from inputEdges where not null sourceNode
select sourceNode,sourceName,destName,destName from outputEdges

/ finos_kdb

/ kxcontrib_cburke
select price,size*neg b,size*not b from x

/ psaris_funq
select genres from movie where movieId = 85261
select avg rating by movieId from rating
select avg rating by userId from rating

/ psaris_qtips
select sum time,nc:count i by id:pid from e
select sum time*1e-6,n:count i,avg nc by func from e
select id,time,tp,ts from t where date=dt

/ qbists_studyq
delete from `C where y<max fr

/ timeseries_kdb
select from t where success

/ ~/q-corpus, readable once dates, times and xbar were
select time,price from trade where date=d,sym=s,time>09:35
select min price,avg price,max price from trade where date=2010.12.09,sym=`IBM
select nm:count i by .5 xbar rating from t

/ hand-written tick queries
select from trades where time within 2024.01.15D09:30:00.000000000 2024.01.15D10:00:00.000000000
select vwap:(sum px*qty)%sum qty by sym, 0D00:05:00.000000000 xbar time from trades
select n:count i by 0D01:00 xbar time from trades where day=2024.01.15
select sym, px, bid from aj[`sym`time;trades;quotes]
select hi:max px, lo:min px by sym, `year$time from trades
update px:(type px)$0f from `trades where time<2024.01.15D09:31:00.000000000

/ window functions, as update ... by
update vwap:(sums px*qty)%sums qty by sym from `time xasc trades
update n:1+til count i by sym from `time xasc trades
update chg:px-1 xprev px by sym from `time xasc trades
update m:3 mavg px by sym from `time xasc trades
update total:sum qty by sym from trades
