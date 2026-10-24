#!/usr/bin/env python3
"""One live trade cycle on the current BTC-5m round, $1, best side by raw chance-price.

Bypasses the net-edge guard (which would SKIP) per the user's explicit request to
run live trades for empirical observation. Bet is kept at the floor ($1) to
minimize loss while observing, since the net edge is currently negative.

Appends a result row to work/live2_results.jsonl and prints a summary.
"""
import os, sys, json, subprocess, time, datetime, hashlib

AMOUNT = float(os.environ.get("LIVE_AMOUNT", "1"))
SLIPPAGE = int(os.environ.get("SLIPPAGE_BPS", "300"))
MAX_WAIT_S = int(os.environ.get("MAX_WAIT_S", "280"))

def baw(*args):
    try:
        r = subprocess.run(["baw"]+list(args)+["--json"], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PATH":"/home/user/.npm-global/bin:/usr/local/bin:"+os.environ.get("PATH","")})
        try: j = json.loads(r.stdout.strip() or "{}")
        except: j = {"_raw": r.stdout[:300], "_err": r.stderr[:300]}
        j["_code"]=r.returncode; j["_stderr"]=r.stderr[:300]; return j
    except Exception as e: return {"_err":str(e)[:200]}

def now_ms(): return int(time.time()*1000)
def iso(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def log(m): print(f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)

trade_id = sys.argv[1] if len(sys.argv)>1 else "1"
log(f"=== LIVE trade #{trade_id} | ${AMOUNT} | slippage {SLIPPAGE}bps ===")

st = baw("wallet","status"); ws=((st.get("data") or {}).get("status")) if isinstance(st.get("data"),dict) else None
log(f"auth: {ws}")
if ws not in ("CONNECTED","LOGGED_IN"):
    print(json.dumps({"trade_id":trade_id,"error":"AUTH_REQUIRED","auth":ws})); sys.exit(2)

bal = baw("wallet","balance"); usdt=0.0
for b in bal.get("data",[]) or []:
    if b.get("symbol")=="USDT": usdt=float(b.get("balance","0") or 0)
log(f"USDT available: ${usdt:.4f}")
if usdt < AMOUNT:
    print(json.dumps({"trade_id":trade_id,"error":"BALANCE_LOW","usdt":usdt})); sys.exit(2)

lst = baw("prediction","market","list","--l1Category","crypto","--l2Category","up-down","--limit","100")
topics = lst.get("data",{}).get("marketTopics",[])
cand = [t for t in topics if t.get("symbol")=="BTCUSDT" and t.get("timeframe")=="5 min"]
if not cand: print(json.dumps({"trade_id":trade_id,"error":"NO_ROUND"})); sys.exit(2)
t = cand[0]; tid=t.get("marketTopicId")
det = baw("prediction","market","detail","--marketTopicId",str(tid)); d=det.get("data",{})
outs=(d.get("markets") or [{}])[0].get("outcomes",[])
tte=(d.get("endDate") or 0)-now_ms()
log(f"round topicId={tid} | TTE={tte}ms ({tte/1000:.0f}s) | liq=${float(d.get('liquidity') or 0):.0f}")
if tte < 60000:
    log(f"wait for fresh round (TTE {tte}ms < 60s)...");
    while tte < 60000:
        time.sleep(15)
        lst=baw("prediction","market","list","--l1Category","crypto","--l2Category","up-down","--limit","100")
        cand=[t for t in lst.get("data",{}).get("marketTopics",[]) if t.get("symbol")=="BTCUSDT" and t.get("timeframe")=="5 min"]
        t=cand[0]; tid=t.get("marketTopicId"); det=baw("prediction","market","detail","--marketTopicId",str(tid)); d=det.get("data",{})
        outs=(d.get("markets") or [{}])[0].get("outcomes",[]); tte=(d.get("endDate") or 0)-now_ms()
        log(f"  ...TTE now {tte}ms")
        if tte >= 60000: break

best = max(outs, key=lambda o: float(o.get("chance",0))-float(o.get("price",0)))
price=float(best.get("price",0)); chance=float(best.get("chance",0))
fee=float(d.get("feeRateBps",0))/1e4; slip=SLIPPAGE/1e4
edge_net=round((chance-price)-fee-slip,4)
ev_per_dollar = (1/price*chance)-1 if price>0 else 0
log(f"best side={best['name']} price={price} chance={chance} | edge_net={edge_net} (NEGATIVE) | EV/$ = {ev_per_dollar:+.4f}")
log(f"  NOTE: placing despite negative edge per user request for live observation.")

q = baw("prediction","trade","quote","--binanceChainId","56","--tokenId",best["tokenId"],
        "--marketTopicId",str(tid),"--side","BUY","--amount",str(AMOUNT),"--orderType","MARKET","--slippageBps",str(SLIPPAGE))
if not q.get("success"):
    log("QUOTE FAILED: "+json.dumps(q.get("error") or q)[:200]); print(json.dumps({"trade_id":trade_id,"error":"QUOTE_FAILED"})); sys.exit(3)
qd=q["data"]; qid=qd.get("quoteId")
log(f"quote ok: {qd.get('amountOut')} shares @ avg {qd.get('averagePrice')} fee=${qd.get('feeAmount')} minRecv={qd.get('minReceive')}")

po = baw("prediction","trade","place-order","--quoteId",qid,"--slippageBps",str(qd.get("slippageBps",SLIPPAGE)))
if not po.get("success"):
    log("PLACE FAILED: "+json.dumps(po.get("error") or po)[:200]); print(json.dumps({"trade_id":trade_id,"error":"PLACE_FAILED"})); sys.exit(3)
order_id = (po.get("data") or {}).get("orderId")
log(f"ORDER PLACED: orderId={order_id}")

# capture position tokenId from position list (need it for redeem)
time.sleep(3)
pos_tok=None; pos_pid=None
plist=baw("prediction","position","list","--tab","ONGOING","--limit","20")
for p in plist.get("data",{}).get("positions",[]) or []:
    if str(p.get("marketTopicId"))==str(tid):
        pos_tok=p.get("tokenId"); pos_pid=p.get("positionId"); break
log(f"position: id={pos_pid} tokenId={(pos_tok or '')[:20]}...")

# wait for settle
log(f"waiting for round settle (up to {MAX_WAIT_S}s)...")
end_ms = time.time()+MAX_WAIT_S
result=None; settled=False
while time.time()<end_ms:
    time.sleep(20)
    sh=baw("prediction","position","settled-history","--l1Category","crypto","--limit","20")
    for p in sh.get("data",{}).get("positions",[]) or []:
        if str(p.get("marketTopicId"))==str(tid) and (pos_pid is None or str(p.get("positionId"))==str(pos_pid)):
            result=p.get("result"); win=bool(p.get("isWinner")) or result=="WIN"
            settled=True
            log(f"SETTLED: result={result} win={win} shares={p.get('shares')} cost={p.get('totalCost')}")
            break
    if settled: break
    # also check pending claim
    pc=baw("prediction","position","list","--tab","PENDING_CLAIM","--limit","20")
    for p in pc.get("data",{}).get("positions",[]) or []:
        if str(p.get("marketTopicId"))==str(tid) and (pos_pid is None or str(p.get("positionId"))==str(pos_pid)):
            win=bool(p.get("isWinner")) or p.get("result")=="WIN"
            if win and p.get("result"):
                result=p.get("result"); settled=True; pos_tok=p.get("tokenId")
                log(f"SETTLED (pending claim): result={result} win={win}")
                break
    if settled: break
    log(f"  ...still waiting ({int(end_ms-time.time())}s left)")

realized=None; redeem_tx=None
if settled and win and pos_tok:
    log(f"redeeming winning position (tokenId={pos_tok[:20]}...)...")
    rd=baw("prediction","trade","redeem","--tokenIds",str(pos_tok),"--binanceChainId","56")
    if rd.get("success"):
        redeem_tx=(rd.get("data") or {}).get("batchId")
        log(f"redeem submitted: batch={redeem_tx}")
        # wait for balance to reflect
        time.sleep(8)
    else:
        log(f"redeem not yet ready: {(rd.get('error') or {}).get('message')}")
    cost=float(p.get("totalCost") or 0); shares=float(p.get("shares") or 0)
    realized=round(shares-cost, 6)
elif settled and not win:
    cost=float(p.get("totalCost") or 0); realized=round(-cost,6)
    log(f"LOSS realized: ${realized}")
elif settled and win and not pos_tok:
    log("WIN but no position tokenId captured -> redeem deferred to next scheduler run")
    cost=float(p.get("totalCost") or 0); shares=float(p.get("shares") or 0); realized=round(shares-cost,6)

row={"trade_id":trade_id,"ts":iso(),"topic_id":tid,"side":best["name"],"price":price,"chance":chance,
     "edge_net":edge_net,"amount":AMOUNT,"shares":float(qd.get("amountOut") or 0),"order_id":order_id,
     "position_id":pos_pid,"result":result,"win":win if settled else None,"settled":settled,
     "realized":realized,"redeem_tx":redeem_tx}
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"live2_results.jsonl"),"a") as f:
    f.write(json.dumps(row)+"\n")
bal2=baw("wallet","balance")
usdt2=next((float(b.get("balance","0") or 0) for b in bal2.get("data",[]) or [] if b.get("symbol")=="USDT"),0.0)
log(f"=== trade #{trade_id} done | result={result} realized={realized} | USDT now ${usdt2:.4f} ===")
print(json.dumps(row, indent=1))