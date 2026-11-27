"""Step 1 of docs/strategies/endgame-maker-tf.md: late-window calibration of the favourite per timeframe.

    uv run python -m polydata.research.endgame_tf_calibration 2026-11-24
"""
import sys, numpy as np, pandas as pd
from polydata.views import connect
con=connect()
day=sys.argv[1]
TAUS=[30,60,120,300,600,1800]
con.execute(f"""create temp table mk as select slug,coin,timeframe,window_start_ms,window_end_ms,(outcome='Up')::int up
 from markets where outcome is not null and timeframe in ('5m','15m','1h','4h','1d')
 and strftime(window_end,'%Y-%m-%d')='{day}' and window_start_ms>=(select min(recv_ms) from pm_best_bid_ask where slug in (select slug from markets where strftime(window_start,'%Y-%m-%d')='{day}' and timeframe='5m'))-0""")
print(con.execute("select timeframe,count(*) from mk group by 1").fetchall())
con.execute("create temp table pm as select slug,outcome,recv_ms,best_bid,best_ask from pm_best_bid_ask where slug in (select slug from mk)")
con.execute(f"create temp table g as select mk.*, tau, window_end_ms-tau*1000 t from mk, (select unnest({TAUS}) tau)")
df=con.execute("""select g.*, u.best_bid ub,u.best_ask ua,d.best_bid db,d.best_ask da from g
 asof left join (select * from pm where outcome='Up') u on g.slug=u.slug and g.t>=u.recv_ms
 asof left join (select * from pm where outcome='Down') d on g.slug=d.slug and g.t>=d.recv_ms
 where g.window_end_ms-g.window_start_ms>g.tau*1000""").df()
df=df.dropna(subset=['ub','ua','db','da'])
um=(df.ub+df.ua)/2; dm=(df.db+df.da)/2
upfav=um>=dm
df['mid']=np.where(upfav,um,dm); df['spr']=np.where(upfav,df.ua-df.ub,df.da-df.db)
df['win']=np.where(upfav,df.up,1-df.up)
df['block']=df.window_start_ms//900000
df=df[(df.spr<=0.0201)&(df.mid>=0.75)&(df.mid<=0.95)]
rows=[]
rng=np.random.default_rng(0)
for (tf,tau),g in df.groupby(['timeframe','tau']):
    b=g.groupby('block').agg(w=('win','sum'),m=('mid','sum'),n=('win','size'))
    gap=(b.w.sum()-b.m.sum())/b.n.sum()*100
    idx=rng.integers(0,len(b),(2000,len(b)))
    bs=(b.w.to_numpy()[idx].sum(1)-b.m.to_numpy()[idx].sum(1))/b.n.to_numpy()[idx].sum(1)*100
    lo,hi=np.percentile(bs,[2.5,97.5])
    rows.append((tf,tau,len(g),g.win.mean()*100,g.mid.mean()*100,gap,lo,hi))
r=pd.DataFrame(rows,columns=['tf','tau','n','win%','mid%','gap_c','lo','hi'])
r['tf']=pd.Categorical(r.tf,['5m','15m','1h','4h','1d']); print(r.sort_values(['tf','tau']).round(1).to_string(index=False))
