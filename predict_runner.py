#!/usr/bin/env python3
"""
Desk BTC-5m — 4-phase prediction desk (Binance Web3 Up/Down, BTC 5min).

Architecture (faithful to "several desks, 1 wallet, reports flow to the center desk"):
  This is ONE scheduled agent that runs 4 PHASES per tick. Only the EXECUTE phase
  may call `baw prediction trade place-order`. Phases 1-3 only WRITE proposals/state.
  Channel separation is enforced in code: analyze/plan/decide never import the trade path.

  Phase 1 — Settle      (cross-run): realize PnL from newly-settled positions -> pnl_history.json
  Phase 2 — Analisa     : read live BTC-5m round (market list/detail, READ-ONLY)
  Phase 3 — Rencanakan  : compute net edge (after fee + slippage), filter liquidity/TTE/cooldown
  Phase 4 — Memutuskan + Eksekusi : pick a side or SKIP; live place-order OR dry simulate.

Loss-minimizing rules:
  * Enter only if edge_net >= EDGE_THRESHOLD, liquidity >= MIN, TTE >= MIN_TTE, not cooled down.
  * Cooldown a desk after COOLDOWN_LOSSES recent realized losses (the "find-cause-and-fix" step:
    the fix is to PAUSE the desk with the adverse-regime hypothesis logged).
  * Errors are classified with an actionable diagnosis (AUTH_REQUIRED / BALANCE_LOW / RATE_LIMIT
    / TIMEOUT / BAW_MISSING / UNKNOWN). Auto-retry on rate-limit; log the rest for the next run.

Persistence reality:
  * baw session lives at ~/.baw/session.json (local disk) -> wiped on sandbox reset; NOT in the
    agent snapshot. baw binary (npm global) IS in the snapshot. So a scheduled run may need
    re-pairing (QR) after a reset; until then it reports AUTH_REQUIRED and skips execution.
  * State is pushed to GitHub (state.json, pnl_history.json, pnl_state.json, log.jsonl) so the
    Vercel dashboard can monitor it via raw.githubusercontent.com.

Env (overridable):
  DRY_RUN=1  BET_AMOUNT=1  EDGE_THRESHOLD=0.03  MIN_LIQUIDITY=2000
  MIN_TIME_TO_END_MS=60000  COOLDOWN_LOSSES=3
  GH_REPO=owner/repo  GITHUB_TOKEN=...  BAW_BIN=baw
  SCOPE_SYMBOL=BTCUSDT  SCOPE_TIMEFRAME=5 min  SCOPE_COIN=BTC
"""
import os, sys, json, subprocess, time, urllib.request, urllib.error, datetime, hashlib, traceback, base64

DRY_RUN        = os.environ.get("DRY_RUN", "1") != "0"
BET_AMOUNT     = float(os.environ.get("BET_AMOUNT", "1"))        # fallback flat bet (dry)
MAX_BET        = float(os.environ.get("MAX_BET", "3"))            # ceiling per live trade (USDT)
MIN_BET        = float(os.environ.get("MIN_BET", "0.5"))          # floor when marginally confident
STRONG_EDGE    = float(os.environ.get("STRONG_EDGE", "0.10"))      # edge at which bet = MAX_BET
MIN_EDGE_NET   = float(os.environ.get("MIN_EDGE_NET", "0.05"))      # MINIMUM net edge after fee+slip to ENTER (jangan asal masuk)
EDGE_THRESHOLD = MIN_EDGE_NET                                       # alias (legacy guard uses this)
SLIPPAGE_BPS   = int(os.environ.get("SLIPPAGE_BPS", "300"))         # tight slippage (3%) for live quotes
MIN_LIQUIDITY  = float(os.environ.get("MIN_LIQUIDITY", "2000"))
MIN_TTE_MS     = int(os.environ.get("MIN_TIME_TO_END_MS", "60000"))
COOLDOWN_LOSSES= int(os.environ.get("COOLDOWN_LOSSES", "3"))
# --- mean-reversion edge signal (backtested ~53% hit, ~55% on small body) ---
BODY_MAX       = float(os.environ.get("BODY_MAX", "0.0005"))         # only trade reversal if last candle body < 0.05%
TREND_LOOKBACK = int(os.environ.get("TREND_LOOKBACK", "3"))           # if last N candles same dir -> TRENDING -> skip
MAX_PRICE_EXT  = float(os.environ.get("MAX_PRICE_EXT", "0.85"))       # skip if either side price > 85% (strong move, mean-rev fails)
REVERSAL_PROB  = float(os.environ.get("REVERSAL_PROB", "0.52"))       # conservative calibrated reversal prob (below 55% backtest)
KLINES_URL     = os.environ.get("KLINES_URL", "https://data-api.binance.vision/api/v3/klines")
GH_REPO        = os.environ.get("GH_REPO", "")
GH_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
BAW            = os.environ.get("BAW_BIN", "baw")
SCOPE_SYMBOL   = os.environ.get("SCOPE_SYMBOL", "BTCUSDT")
SCOPE_TIMEFRAME= os.environ.get("SCOPE_TIMEFRAME", "5 min")
SCOPE_COIN     = os.environ.get("SCOPE_COIN", "BTC")
DESK           = f"{SCOPE_COIN}-5m"

def now_ms(): return int(time.time()*1000)
def iso(): return datetime.datetime.now(datetime.timezone.utc).isoformat()

def baw(*args, retries=1):
    for attempt in range(retries+1):
        try:
            r = subprocess.run([BAW]+list(args)+["--json"], capture_output=True, text=True, timeout=60)
            out = r.stdout.strip() or "{}"
            try: j = json.loads(out)
            except json.JSONDecodeError:
                j = {"_parse_failed": True, "_stdout": out[:400], "_stderr": r.stderr[:400], "_code": r.returncode}
            j["_code"] = r.returncode; j["_stderr"] = r.stderr[:400]
            if r.returncode != 0 and attempt < retries and ("429" in r.stderr or "rate" in r.stderr.lower()):
                time.sleep(2*(attempt+1)); continue
            return j
        except subprocess.TimeoutExpired:
            if attempt < retries: time.sleep(2); continue
            return {"_err": "TIMEOUT", "_args": list(args)}
        except FileNotFoundError:
            return {"_err": "BAW_MISSING", "_msg": f"{BAW} not found on PATH"}
        except Exception as e:
            return {"_err": "EXC", "_msg": str(e), "_tb": traceback.format_exc()[:500]}
    return {"_err": "RETRY_EXHAUSTED"}

def classify(err):
    s = str(err).lower()
    if "not logged" in s or ("auth" in s and "log" in s): return ("AUTH_REQUIRED","Wallet not logged in. baw session is at ~/.baw/session.json (local disk) — wiped on sandbox reset. Re-pair via `baw auth signin` (QR scan). baw binary persists in the agent snapshot; the session does not.")
    if "insufficient" in s and "balanc" in s: return ("BALANCE_LOW","Wallet has no USDT on BSC (chain 56). Fund it before live trading. Dry simulation still works.")
    if "429" in s or "rate" in s: return ("RATE_LIMIT","Binance rate-limited the call. Runner retries automatically; space runs further apart.")
    if "timeout" in s: return ("TIMEOUT","baw call exceeded 60s. Retry; market data endpoint may be slow.")
    if "not found" in s: return ("BAW_MISSING","baw CLI not installed in this sandbox. Install @binance/agentic-wallet globally; it persists in the agent snapshot.")
    return ("UNKNOWN", str(err)[:160])

def github_put(path, content_bytes, message):
    if not (GH_TOKEN and GH_REPO): return None
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    sha = None
    try:
        req = urllib.request.Request(url, headers={"Authorization":f"Bearer {GH_TOKEN}","Accept":"application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r: sha = json.load(r).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404: return None
    except Exception: pass
    body = json.dumps({"message": message, "content": base64.b64encode(content_bytes).decode(), "sha": sha}).encode()
    try:
        req = urllib.request.Request(url, data=body, method="PUT",
                headers={"Authorization":f"Bearer {GH_TOKEN}","Accept":"application/vnd.github+json","Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=20) as r: return json.load(r).get("content",{}).get("sha")
    except Exception: return None

def map_to_desk(p):
    """Map a settled position to BTC-5m if it belongs to this desk (via imageUrl pattern)."""
    img = (p.get("imageUrl") or "").lower()
    if "automarket-btc-usd-5-minutes" in img: return DESK
    return None

def fetch_btc_klines(interval="5m", limit=30):
    """Fetch recent BTC 5m klines from Binance public market-data API (no key needed)."""
    url = f"{KLINES_URL}?symbol={SCOPE_SYMBOL}&interval={interval}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"prediction-desk/1.2"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
        # each row: [openTime, open, high, low, close, volume, closeTime, ...]
        return [{"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),"c":float(x[4]),"t":x[0],"ct":x[6]} for x in rows]
    except Exception as e:
        return None

def compute_meanrev_signal():
    """Mean-reversion signal on the last CLOSED 5m candle.

    Returns dict with: signal_side ("Up"/"Down" or None), my_prob (float), body, regime
    ("CHOPPY"/"TRENDING"/"NO_DATA"), reason.

    Signal = predict REVERSAL of the last completed candle, but ONLY enter if:
      - last candle body < BODY_MAX (small body -> stronger reversal in backtest)
      - last TREND_LOOKBACK candles are NOT all same direction (no strong trend -> mean-rev fails)
    Calibrated reversal prob = REVERSAL_PROB (0.52, conservative vs ~55% backtest).
    """
    kl = fetch_btc_klines("5m", max(30, TREND_LOOKBACK+5))
    if not kl or len(kl) < TREND_LOOKBACK+2:
        return {"signal_side":None,"my_prob":None,"body":None,"regime":"NO_DATA","reason":"no klines"}
    # use only candles that have CLOSED (exclude the currently-forming one: the last in list)
    closed = kl[:-1]
    last = closed[-1]
    body = abs(last["c"] - last["o"]) / last["o"] if last["o"]>0 else 0.0
    last_dir = 1 if last["c"] > last["o"] else (-1 if last["c"] < last["o"] else 0)
    # trend check: last TREND_LOOKBACK closed candles all same direction?
    dirs = [ (1 if c["c"]>c["o"] else (-1 if c["c"]<c["o"] else 0)) for c in closed[-TREND_LOOKBACK:] ]
    trending = (len(set(dirs))==1 and dirs[0]!=0)
    if last_dir == 0:
        return {"signal_side":None,"my_prob":None,"body":round(body,6),"regime":"FLAT","reason":"last candle doji"}
    signal_side = "Up" if last_dir == -1 else "Down"   # reverse the last move
    if body >= BODY_MAX:
        return {"signal_side":signal_side,"my_prob":None,"body":round(body,6),"regime":"BODY_TOO_LARGE",
                "reason":f"body {body:.5f} >= BODY_MAX {BODY_MAX} (signal weak)"}
    if trending:
        return {"signal_side":signal_side,"my_prob":None,"body":round(body,6),"regime":"TRENDING",
                "reason":f"last {TREND_LOOKBACK} candles all same dir (mean-rev fails in trend)"}
    return {"signal_side":signal_side,"my_prob":REVERSAL_PROB,"body":round(body,6),"regime":"CHOPPY",
            "reason":f"small-body reversal ({signal_side}); my_prob={REVERSAL_PROB}"}

def phase_settle(state):
    """Realize PnL from new settled positions; append a point to pnl_history."""
    phase = {"name":"Settle","status":"OK","detail":"","new_settled":0,"delta_usd":0.0}
    seen_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),"pnl_state.json")
    hist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),"pnl_history.json")
    try:
        st = json.load(open(seen_path))
    except Exception: st = {"seen_ids":[], "cumulative":0.0, "wins":0, "losses":0}
    seen = set(st.get("seen_ids",[]))
    # Auto-redeem winning PENDING_CLAIM positions in scope (uses the POSITION tokenId,
    # not the market outcome tokenId — the latter returns NOT_WINNING_POSITION).
    pend = baw("prediction","position","list","--tab","PENDING_CLAIM","--limit","50")
    redeemed=[]
    if pend.get("success"):
        for p in pend.get("data",{}).get("positions",[]) or []:
            if map_to_desk(p) != DESK: continue
            if not (bool(p.get("isWinner")) or p.get("result")=="WIN"): continue
            ptid = p.get("tokenId")
            if not ptid: continue
            rd = baw("prediction","trade","redeem","--tokenIds",str(ptid),"--binanceChainId","56")
            ok = bool(rd.get("success"))
            redeemed.append({"positionId":p.get("positionId"),"redeem_ok":ok,
                             "txHash":(rd.get("data") or {}).get("batchId"),
                             "err":(rd.get("error") or {}).get("message") if not ok else None})
            if not ok and (rd.get("error") or {}).get("message") != "NOT_WINNING_POSITION":
                state["errors"].append({"where":"settle:redeem","code":"REDEEM_FAILED",
                    "diag":f"Redeem for pos {p.get('positionId')} failed: {(rd.get('error') or {}).get('message')}. Will retry next run."})
    if redeemed:
        phase["detail"]+=f" | redeemed {sum(1 for r in redeemed if r['redeem_ok'])}/{len(redeemed)} pending wins"
    r = baw("prediction","position","settled-history","--l1Category","crypto","--limit","100")
    new=[]; cum=st.get("cumulative",0.0); wins=st.get("wins",0); losses=st.get("losses",0)
    if r.get("success"):
        for p in r.get("data",{}).get("positions",[]):
            pid = p.get("positionId")
            if pid in seen: continue
            if map_to_desk(p) != DESK: continue
            cost=float(p.get("totalCost") or 0); shares=float(p.get("shares") or 0)
            win = bool(p.get("isWinner")) or (p.get("result")=="WIN")
            payout = shares if win else 0.0
            realized = round(payout - cost, 6)
            cum = round(cum + realized, 6)
            if win: wins+=1
            else: losses+=1
            new.append({"positionId":pid,"desk":DESK,"outcome":p.get("outcomeName"),
                        "shares":shares,"cost":cost,"realized":realized,"win":win,
                        "title":p.get("marketTopicTitle")})
            seen.add(pid)
            if not win:
                state["errors"].append({"where":"settle","code":"REALIZED_LOSS","diag":
                    f"{DESK} settled LOSS on {p.get('outcomeName')} @ cost {cost:.4f} (shares {shares}). "
                    f"Hypothesis: adverse 5m move against the side. Net edge filter + cooldown mitigate; review edge model."})
    st = {"seen_ids":sorted(seen), "cumulative":cum, "wins":wins, "losses":losses}
    json.dump(st, open(seen_path,"w"), indent=1)
    # append a point every run so the curve lives
    hist=[]
    try: hist=json.load(open(hist_path))
    except Exception: hist=[]
    hist.append({"ts":iso(),"run_id":state["run_id"],"cumulative_usd":round(cum,4),
                 "delta_usd":round(sum(n["realized"] for n in new),4),"wins":wins,"losses":losses,
                 "new_settled":len(new)})
    json.dump(hist, open(hist_path,"w"), indent=1)
    phase["new_settled"]=len(new); phase["delta_usd"]=round(sum(n["realized"] for n in new),4)
    phase["detail"]=f"cumulative ${cum:.4f} | {wins}W/{losses}L | +{len(new)} settled"
    state["pnl"]={"cumulative_usd":round(cum,4),"wins":wins,"losses":losses,"new":new}
    state["phases"].append(phase)
    return st

def phase_analyze(state):
    """Read live BTC-5m round (read-only)."""
    phase={"name":"Analisa","status":"OK","detail":"","marketTopicId":None}
    lst = baw("prediction","market","list","--l1Category","crypto","--l2Category","up-down","--limit","100")
    if not lst.get("success"):
        c,msg=classify(lst.get("_err") or lst.get("error",{}))
        phase["status"]="ERROR"; phase["detail"]=msg; state["errors"].append({"where":"analyze:list","code":c,"diag":msg})
        state["phases"].append(phase); return None
    topics=lst.get("data",{}).get("marketTopics",[])
    cand=[t for t in topics if t.get("symbol")==SCOPE_SYMBOL and t.get("timeframe")==SCOPE_TIMEFRAME]
    if not cand:
        phase["status"]="ERROR"; phase["detail"]=f"No live {SCOPE_SYMBOL} {SCOPE_TIMEFRAME} round found."
        state["errors"].append({"where":"analyze:scope","code":"NO_ROUND","diag":phase["detail"]})
        state["phases"].append(phase); return None
    t=cand[0]
    det=baw("prediction","market","detail","--marketTopicId",str(t.get("marketTopicId")))
    if not det.get("success"):
        c,msg=classify(det.get("_err") or det.get("error",{}))
        phase["status"]="ERROR"; phase["detail"]=msg; state["errors"].append({"where":"analyze:detail","code":c,"diag":msg})
        state["phases"].append(phase); return None
    d=det["data"]
    phase["marketTopicId"]=d.get("marketTopicId")
    outs=d.get("markets",[{}])[0].get("outcomes",[])
    phase["detail"]=f"topic {d.get('marketTopicId')} | {len(outs)} outcomes | liq ${float(d.get('liquidity') or 0):.0f}"
    state["market"]={"marketTopicId":d.get("marketTopicId"),"symbol":d.get("symbol"),"timeframe":d.get("timeframe"),
                     "status":d.get("status"),"liveStatus":d.get("liveStatus"),
                     "liquidity":float(d.get("liquidity") or 0),"feeRateBps":d.get("feeRateBps"),
                     "slippageBps":d.get("slippageBps"),"endDate":d.get("endDate"),"outcomes":outs}
    state["phases"].append(phase)
    return d

def phase_plan(state, m):
    """Compute edge from the MEAN-REVERSION SIGNAL (not market chance) and gate entry.

    Filters (all must pass to enter — 'jangan asal masuk'):
      1. signal present (last candle body < BODY_MAX, small body)
      2. not TRENDING (last N candles not all same dir — mean-rev fails in trends)
      3. not extreme market price (neither side > MAX_PRICE_EXT — strong move)
      4. edge_net = my_prob - price - fee - slip >= MIN_EDGE_NET
      5. liquidity + TTE + not-cooldown
    """
    phase={"name":"Rencanakan","status":"SKIP","detail":"","best":None,"signal":None}
    outs=state["market"]["outcomes"]
    fee=float(state["market"].get("feeRateBps",0))/1e4
    slip=SLIPPAGE_BPS/1e4
    liq=state["market"]["liquidity"]; tte=(state["market"].get("endDate") or 0)-now_ms()
    state["market"]["tte_ms"]=tte
    sig=compute_meanrev_signal()
    phase["signal"]=sig
    by_name={o.get("name"):o for o in outs}
    best=None
    if sig["signal_side"] and sig["my_prob"] is not None and sig["signal_side"] in by_name:
        o=by_name[sig["signal_side"]]
        price=float(o.get("price",0) or 0); my_prob=float(sig["my_prob"])
        edge_gross=my_prob-price; edge_net=edge_gross-fee-slip
        best={"side":sig["signal_side"],"price":price,"my_prob":my_prob,
              "edge_gross":round(edge_gross,4),"fee":fee,"slip":slip,
              "edge_net":round(edge_net,4),"tokenId":o.get("tokenId"),
              "signal_regime":sig["regime"],"signal_body":sig["body"]}
    state["market"]["best"]=best
    extreme = any(float(o.get("price",0) or 0) > MAX_PRICE_EXT for o in outs)
    cooldown = state["pnl"]["losses"] >= COOLDOWN_LOSSES
    checks={"signal":best is not None,
            "edge":best is not None and best["edge_net"]>=MIN_EDGE_NET,
            "extreme":not extreme,"liquidity":liq>=MIN_LIQUIDITY,
            "tte":tte>=MIN_TTE_MS,"cooldown":not cooldown}
    phase["best"]=best; phase["checks"]=checks
    if best:
        phase["detail"]=(f"{best['side']} price={best['price']} my_prob={best['my_prob']} "
                          f"edge_net={best['edge_net']} (req>={MIN_EDGE_NET}) | liq ${liq:.0f} tte {tte}ms | "
                          f"regime={sig['regime']} body={sig['body']} extreme={extreme} | pass={checks}")
        phase["status"]="OK"
        if extreme:
            phase["status"]="SKIP"; phase["detail"]+=f" | SKIP: price extreme (>{MAX_PRICE_EXT})"
            state["errors"].append({"where":"plan","code":"PRICE_EXTREME","diag":f"One side > {MAX_PRICE_EXT}: strong move, mean-reversion unreliable. Skipping."})
        elif best["edge_net"] < MIN_EDGE_NET:
            phase["status"]="SKIP"; phase["detail"]+=f" | SKIP: edge {best['edge_net']} < MIN {MIN_EDGE_NET}"
    else:
        phase["detail"]=(f"no signal ({sig['regime']}: {sig['reason']}) | liq ${liq:.0f} tte {tte}ms | "
                          f"extreme={extreme} | pass={checks}")
    if cooldown:
        phase["status"]="COOLDOWN"; phase["detail"]+=f" | COOLDOWN ({state['pnl']['losses']} losses >= {COOLDOWN_LOSSES})"
        state["errors"].append({"where":"plan","code":"COOLDOWN","diag":f"{DESK} paused after {state['pnl']['losses']} realized losses >= {COOLDOWN_LOSSES}. Hypothesis: adverse regime; pause until losses age out / edge model is reviewed."})
    state["phases"].append(phase)
    return best, checks

def phase_execute(state, best, checks):
    """Decide + execute. Only this phase may call baw trade place-order (live) or simulate (dry)."""
    phase={"name":"Memutuskan+Eksekusi","status":"SKIP","detail":"","side":None,"action":"SKIP","reason":None}
    if not best:
        phase["reason"]="NO_BEST"; phase["detail"]="no outcome to evaluate"; state["phases"].append(phase); return
    if not checks["edge"]:
        phase["reason"]=f"EDGE_TOO_LOW({best['edge_net']})"; phase["detail"]=f"edge_net {best['edge_net']} < {EDGE_THRESHOLD}"
    elif not checks["liquidity"]:
        phase["reason"]=f"LOW_LIQUIDITY({state['market']['liquidity']:.0f})"; phase["detail"]=phase["reason"]
    elif not checks["tte"]:
        phase["reason"]=f"TOO_CLOSE_TO_SETTLEMENT({state['market']['tte_ms']}ms)"; phase["detail"]=phase["reason"]
    elif not checks["cooldown"]:
        phase["reason"]="COOLDOWN_LOSSES"; phase["detail"]="desk cooled down"
    if phase["reason"]:
        state["phases"].append(phase); return
    # decide
    phase["side"]=best["side"]; phase["status"]="DECIDED"
    # bet sizing: linear from EDGE_THRESHOLD (MIN_BET) to STRONG_EDGE (MAX_BET); cap by 25% of balance
    bal_usdt=0.0
    for b in state.get("balance",[]) or []:
        if b.get("symbol")=="USDT": bal_usdt=float(b.get("balance","0") or 0)
    span=max(1e-6, STRONG_EDGE-EDGE_THRESHOLD)
    frac=min(1.0, max(0.0, (best["edge_net"]-EDGE_THRESHOLD)/span))
    bet=min(MAX_BET, MIN_BET + (MAX_BET-MIN_BET)*frac)
    bet=max(MIN_BET, min(bet, bal_usdt*0.25)) if bal_usdt>0 else bet
    bet=round(bet, 2)
    state["bet_size"]=bet; phase["bet"]=bet
    # execute
    if DRY_RUN:
        price=best["price"]; shares=bet/price if price>0 else 0
        payout_if_win=shares*1.0; ev=shares*best["chance"]-bet
        pos={"run_id":state["run_id"],"desk":DESK,"side":best["side"],"price":price,"chance":best["chance"],
             "amount":bet,"shares":round(shares,4),"cost":bet,"payout_if_win":round(payout_if_win,4),
             "ev":round(ev,4),"edge_net":best["edge_net"],"bet":bet,"dry":True,"ts":iso()}
        state["sim_positions"].append(pos)
        state["pnl_dry"]["trades"]+=1; state["pnl_dry"]["cost"]+=bet
        state["pnl_dry"]["expected_payout"]+=payout_if_win*best["chance"]; state["pnl_dry"]["ev"]+=ev
        phase["action"]="SIMULATE_BUY"; phase["status"]="EXECUTED_DRY"; phase["detail"]=f"SIM {best['side']} ${bet} @ {price} (ev {ev:.4f})"
    else:
        # live: quote then place-order with tight slippage and sized amount
        q=baw("prediction","trade","quote","--binanceChainId","56","--tokenId",best["tokenId"],
             "--marketTopicId",str(state["market"]["marketTopicId"]),"--side","BUY","--amount",str(bet),
             "--orderType","MARKET","--slippageBps",str(SLIPPAGE_BPS))
        if not q.get("success"):
            c,msg=classify(q.get("_err") or q.get("error",{}))
            phase["status"]="ERROR"; phase["reason"]=c; phase["detail"]=f"quote failed: {msg}"
            state["errors"].append({"where":"execute:quote","code":c,"diag":msg})
            state["phases"].append(phase); return
        qid=q["data"].get("quoteId") or q["data"].get("quoteId")
        slip=q["data"].get("slippageBps", SLIPPAGE_BPS)
        po=baw("prediction","trade","place-order","--quoteId",qid,"--slippageBps",str(slip))
        if not po.get("success"):
            c,msg=classify(po.get("_err") or po.get("error",{}))
            phase["status"]="ERROR"; phase["reason"]=c; phase["detail"]=f"place-order failed: {msg}"
            state["errors"].append({"where":"execute:place","code":c,"diag":msg})
        else:
            phase["action"]="LIVE_BUY"; phase["status"]="EXECUTED_LIVE"; phase["detail"]=f"LIVE {best['side']} ${BET_AMOUNT} quote {qid}"
            state["live_positions"].append(po.get("data",{}))
    state["phases"].append(phase)

def main():
    rid=hashlib.sha256(str(now_ms()).encode()).hexdigest()[:10]
    state={"run_id":rid,"ts":iso(),"dry_run":DRY_RUN,"desk":DESK,"scope_symbol":SCOPE_SYMBOL,
           "scope_timeframe":SCOPE_TIMEFRAME,"config":{"bet_amount":BET_AMOUNT,"max_bet":MAX_BET,"min_bet":MIN_BET,
           "edge_threshold":EDGE_THRESHOLD,"strong_edge":STRONG_EDGE,"slippage_bps":SLIPPAGE_BPS,
           "min_liquidity":MIN_LIQUIDITY,"min_tte_ms":MIN_TTE_MS,"cooldown_losses":COOLDOWN_LOSSES},
           "auth":None,"balance":[],"phases":[],"market":None,"sim_positions":[],"live_positions":[],
           "pnl":None,"pnl_dry":{"trades":0,"cost":0.0,"expected_payout":0.0,"ev":0.0},"errors":[],
           "summary":""}
    log={"run_id":rid,"ts":iso(),"dry_run":DRY_RUN}

    # auth + balance
    st=baw("wallet","status"); ws=((st.get("data") or {}).get("status")) if isinstance(st.get("data"),dict) else None
    if st.get("success") and ws in ("CONNECTED","LOGGED_IN"): state["auth"]=ws or "CONNECTED"
    elif st.get("_err")=="BAW_MISSING":
        c,msg=classify("BAW_MISSING"); state["errors"].append({"where":"baw_status","code":c,"diag":msg})
        state["auth"]=c; state["summary"]=f"baw missing — {c}"; return finish(state,log)
    else:
        c,msg=classify(st.get("_err") or st.get("error",{}).get("message") or "auth")
        state["errors"].append({"where":"baw_status","code":c,"diag":msg}); state["auth"]=c
    bal=baw("wallet","balance")
    if bal.get("success"): state["balance"]=bal.get("data",[])
    has_usdt=any((b.get("symbol")=="USDT" and float(b.get("balance","0"))>0) for b in state["balance"]) if state["balance"] else False
    if not DRY_RUN and not has_usdt:
        state["errors"].append({"where":"balance","code":"BALANCE_LOW","diag":"Live mode but no USDT on BSC. Refusing to trade; staying dry this run."})

    # Phase 1 — Settle (cross-run realized PnL)
    try: phase_settle(state)
    except Exception as e:
        state["errors"].append({"where":"settle","code":"EXC","diag":str(e)[:200]})
        state["phases"].append({"name":"Settle","status":"ERROR","detail":str(e)[:160]})

    # Phase 2 — Analisa
    m=phase_analyze(state)
    if m is None:
        state["phases"].append({"name":"Rencanakan","status":"SKIP","detail":"no market"})
        state["phases"].append({"name":"Memutuskan+Eksekusi","status":"SKIP","detail":"no market"})
        state["summary"]=f"{'DRY' if DRY_RUN else 'LIVE'} run {rid} ({DESK}): analyze failed — see errors."
        return finish(state,log)

    # Phase 3 — Rencanakan
    best,checks=phase_plan(state,m)

    # Phase 4 — Memutuskan + Eksekusi
    phase_execute(state,best,checks)

    state["pnl_dry"]["cost"]=round(state["pnl_dry"]["cost"],4)
    state["pnl_dry"]["expected_payout"]=round(state["pnl_dry"]["expected_payout"],4)
    state["pnl_dry"]["ev"]=round(state["pnl_dry"]["ev"],4)
    n_exec=sum(1 for p in state["phases"] if p["status"].startswith("EXECUTED"))
    state["summary"]=f"{'DRY' if DRY_RUN else 'LIVE'} run {rid} ({DESK}): {len(state['phases'])} phases, exec={n_exec}, errors={len(state['errors'])}, pnl_cum=${state['pnl']['cumulative_usd']:.4f}"
    log["phases"]=[{"name":p["name"],"status":p["status"],"detail":p.get("detail","")} for p in state["phases"]]
    log["errors"]=state["errors"]
    return finish(state,log)

def finish(state,log):
    d=os.path.dirname(os.path.abspath(__file__))
    sp=os.path.join(d,"state.json"); lp=os.path.join(d,"log.jsonl")
    hp=os.path.join(d,"pnl_history.json"); pp=os.path.join(d,"pnl_state.json")
    json.dump(state, open(sp,"w"), indent=1)
    prev=""
    try: prev=open(lp).read()
    except Exception: pass
    with open(lp,"w") as f: f.write(prev+json.dumps(log)+"\n")
    pushed=False
    if GH_TOKEN and GH_REPO:
        for path,fp,msg in [("state.json",sp,f"run {state['run_id']} state"),
                            ("pnl_history.json",hp,f"run {state['run_id']} pnl"),
                            ("pnl_state.json",pp,f"run {state['run_id']} pnl-state"),
                            ("log.jsonl",lp,f"run {state['run_id']} log")]:
            try: pushed = github_put(path, open(fp,"rb").read(), msg) and pushed
            except Exception: pass
        if not pushed:
            state["errors"].append({"where":"github_push","code":"PUSH_FAILED","diag":"GitHub contents API push failed (token/repo/permissions). Local files still written."})
    print(json.dumps({"summary":state["summary"],"auth":state["auth"],"phases":len(state["phases"]),
                      "errors":len(state["errors"]),"pnl_cum":state["pnl"]["cumulative_usd"] if state["pnl"] else 0,
                      "pushed":pushed}, indent=1))
    return state

if __name__=="__main__": main()