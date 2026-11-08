from __future__ import annotations
import json
from pathlib import Path
import pandas as pd
from backtest_strategy import (DATA, OUT, FEE_BPS, SLIPPAGE_BPS, compute_pullback_signals,
                               simulate, summarize)

OUT.mkdir(exist_ok=True)
symbols=("BTCUSDT","ETHUSDT","SOLUSDT")
periods=(
    ("2021-02_through_2023",int(pd.Timestamp("2021-02-01",tz="UTC").timestamp()*1000),int(pd.Timestamp("2024-01-01",tz="UTC").timestamp()*1000)),
    ("2024_through_2025",int(pd.Timestamp("2024-01-01",tz="UTC").timestamp()*1000),int(pd.Timestamp("2026-01-01",tz="UTC").timestamp()*1000)),
    ("2026_ytd_through_oct",int(pd.Timestamp("2026-01-01",tz="UTC").timestamp()*1000),None),
)
variants=(
    ("pullback_rising_rsi45",{"rsi_mode":"rising45","slope_bars":3,"require_regime":True}),
    ("pullback_rsi_recovery40",{"rsi_mode":"recovery40","slope_bars":3,"require_regime":True}),
    ("pullback_no_rsi_diagnostic",{"rsi_mode":"none","slope_bars":3,"require_regime":True}),
)
results=[]
for name,kwargs in variants:
    frames={s:compute_pullback_signals(s,**kwargs) for s in symbols}
    counts={s:{"eligible_buys":int((f.signal=="BUY").sum()),"sell_signals":int((f.signal=="SELL").sum())} for s,f in frames.items()}
    results.append({"kind":"funnel","variant":name,"counts":counts})
    for period,start,end in periods:
        sim=simulate(frames,FEE_BPS,SLIPPAGE_BPS,enforce_risk_halts=False,start_timestamp_ms=start,end_timestamp_ms=end)
        results.append({"kind":period,**summarize(name,sim)})
        print(name,period,json.dumps(results[-1]),flush=True)
(OUT/"pullback_results.json").write_text(json.dumps({"method":"research-only state-based EMA20 pullback reclaim with rising EMA50 direction; all exits and risk sizing follow shared approximate simulator; base costs 10bps fee + 5bps slippage per side","results":results},indent=2,default=str),encoding="utf-8")
