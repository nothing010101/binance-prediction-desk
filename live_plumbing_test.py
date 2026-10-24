#!/usr/bin/env python3
"""One-off LIVE plumbing test: place a $1 market order on the current BTC-5m round
to validate quote -> place-order -> fill -> (later) settle -> PnL pipeline.

This is NOT the agent's decision logic (which correctly SKIPs on no edge).
It's a manual validation that the live execution path works end-to-end with real funds.
"""
import os, sys, json, subprocess, time, datetime

def baw(*args):
    r = subprocess.run(["baw"]+list(args)+["--json"], capture_output=True, text=True, timeout=60)
    try: j = json.loads(r.stdout.strip() or "{}")
    except: j = {"_raw": r.stdout[:300], "_err": r.stderr[:300]}
    j["_code"] = r.returncode; j["_stderr"] = r.stderr[:300]
    return j

def now_ms(): return int(time.time()*1000)

AMOUNT = float(os.environ.get("TEST_AMOUNT", "1"))
SLIPPAGE = int(os.environ.get("SLIPPAGE_BPS", "300"))

print(f"=== LIVE plumbing test | amount=${AMOUNT} | slippage={SLIPPAGE}bps ===")
st = baw("wallet","status")
ws = (st.get("data") or {}).get("status") if isinstance(st.get("data"),dict) else None
print("auth:", ws)
bal = baw("wallet","balance")
usdt = 0.0
for b in bal.get("data",[]) or []:
    if b.get("symbol")=="USDT": usdt=float(b.get("balance","0") or 0)
    print(f"  {b.get('symbol'):6} bal={b.get('balance','0')[:12]} value=${float(b.get('value','0') or 0):.4f}")
print(f"USDT available: ${usdt:.4f}")
if usdt < AMOUNT:
    print(f"REFUSE: need ${AMOUNT}, have ${usdt:.4f}"); sys.exit(1)

# current BTC-5m round
lst = baw("prediction","market","list","--l1Category","crypto","--l2Category","up-down","--limit","100")
topics = lst.get("data",{}).get("marketTopics",[])
cand = [t for t in topics if t.get("symbol")=="BTCUSDT" and t.get("timeframe")=="5 min"]
if not cand:
    print("REFUSE: no live BTC-5m round"); sys.exit(1)
t = cand[0]
tid = t.get("marketTopicId")
print(f"round topicId={tid} | slug={t.get('slug')} | liveStatus={t.get('liveStatus')}")

det = baw("prediction","market","detail","--marketTopicId",str(tid))
d = det.get("data",{})
outs = (d.get("markets") or [{}])[0].get("outcomes",[])
tte = (d.get("endDate") or 0) - now_ms()
print(f"TTE={tte}ms ({tte/1000:.0f}s) | liquidity=${float(d.get('liquidity') or 0):.0f}")
if tte < 60000:
    print(f"REFUSE: TTE {tte}ms < 60000ms (too close to settlement)"); sys.exit(1)

# pick best side by raw chance-price (ignore fee for side selection)
best = max(outs, key=lambda o: float(o.get("chance",0))-float(o.get("price",0)))
price=float(best.get("price",0)); chance=float(best.get("chance",0))
fee=float(d.get("feeRateBps",0))/1e4
print(f"best side={best['name']} price={price} chance={chance} fee={fee}")
print(f"  EV per $1 = ({1/price:.4f} shares * {chance}) - 1 = {(1/price*chance)-1:+.4f}  (expected ~-1 to -2%)")

# quote
q = baw("prediction","trade","quote","--binanceChainId","56","--tokenId",best["tokenId"],
        "--marketTopicId",str(tid),"--side","BUY","--amount",str(AMOUNT),
        "--orderType","MARKET","--slippageBps",str(SLIPPAGE))
if not q.get("success"):
    print("QUOTE FAILED:", json.dumps(q.get("error") or q)[:300]); sys.exit(1)
qd = q["data"]
print(f"\n--- QUOTE ---")
print(f"  quoteId={qd.get('quoteId')}")
print(f"  amountIn=${qd.get('amountIn')} -> amountOut={qd.get('amountOut')} shares")
print(f"  averagePrice={qd.get('averagePrice')} | minReceive={qd.get('minReceive')} | feeAmount=${qd.get('feeAmount')}")
print(f"  priceImpact={qd.get('priceImpact')} | expires={qd.get('expireAt')}")

# place order
qid = qd.get("quoteId")
slip = qd.get("slippageBps", SLIPPAGE)
print(f"\n--- PLACING ORDER (quoteId={qid}, slippageBps={slip}) ---")
po = baw("prediction","trade","place-order","--quoteId",qid,"--slippageBps",str(slip))
print("place-order result:")
print(json.dumps(po, indent=1)[:1200])
if po.get("success"):
    print("\n=== FILL OK ===")
    print("positionId:", (po.get("data") or {}).get("positionId"))
else:
    print("\n=== PLACE FAILED ===")
    print(json.dumps(po.get("error") or po, indent=1)[:600])
    sys.exit(1)