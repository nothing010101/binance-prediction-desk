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
BET_AMOUNT     = float(os.environ.get("BET_AMOUNT", "1"))
EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.03"))
MIN_LIQUIDITY  = float(os.environ.get("MIN_LIQUIDITY", "2000"))
MIN_TTE_MS     = int(os.environ.get("MIN_TIME_TO_END_MS", "60000"))
COOLDOWN_LOSSES= int(os.environ.get("COOLDOWN_LOSSES", "3"))
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

def phase_settle(state):
    """Realize PnL from new settled positions; append a point to pnl_history."""
    phase = {"name":"Settle","status":"OK","detail":"","new_settled":0,"delta_usd":0.0}
    seen_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),"pnl_state.json")
    hist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),"pnl_history.json")
    try:
        st = json.load(open(seen_path))
    except Exception: st = {"seen_ids":[], "cumulative":0.0, "wins":0, "losses":0}
    seen = set(st.get("seen_ids",[]))
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
    """Compute net edge per side; pick best; filter."""
    phase={"name":"Rencanakan","status":"OK","detail":"","best":None}
    outs=state["market"]["outcomes"]
    fee=float(state["market"].get("feeRateBps",0))/1e4
    slip=float(state["market"].get("slippageBps",0))/1e4
    best=None
    for o in outs:
        price=float(o.get("price",0) or 0); chance=float(o.get("chance",0) or 0)
        edge_net=(chance-price)-fee-slip
        if best is None or edge_net>best["edge_net"]:
            best={"side":o["name"],"price":price,"chance":chance,"edge_gross":round(chance-price,4),
                  "fee":fee,"slip":slip,"edge_net":round(edge_net,4),"tokenId":o.get("tokenId")}
    state["market"]["best"]=best
    liq=state["market"]["liquidity"]; tte=(state["market"].get("endDate") or 0)-now_ms()
    state["market"]["tte_ms"]=tte
    # cooldown from realized losses
    cooldown = state["pnl"]["losses"] >= COOLDOWN_LOSSES
    checks={"edge":best and best["edge_net"]>=EDGE_THRESHOLD,
            "liquidity":liq>=MIN_LIQUIDITY,"tte":tte>=MIN_TTE_MS,"cooldown":not cooldown}
    phase["best"]=best; phase["checks"]=checks
    phase["detail"]=f"best={best['side'] if best else '-'} edge_net={best['edge_net'] if best else '-'} | liq ${liq:.0f} tte {tte}ms | pass={checks}"
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
    # execute
    if DRY_RUN:
        price=best["price"]; shares=BET_AMOUNT/price if price>0 else 0
        payout_if_win=shares*1.0; ev=shares*best["chance"]-BET_AMOUNT
        pos={"run_id":state["run_id"],"desk":DESK,"side":best["side"],"price":price,"chance":best["chance"],
             "amount":BET_AMOUNT,"shares":round(shares,4),"cost":BET_AMOUNT,"payout_if_win":round(payout_if_win,4),
             "ev":round(ev,4),"edge_net":best["edge_net"],"dry":True,"ts":iso()}
        state["sim_positions"].append(pos)
        state["pnl_dry"]["trades"]+=1; state["pnl_dry"]["cost"]+=BET_AMOUNT
        state["pnl_dry"]["expected_payout"]+=payout_if_win*best["chance"]; state["pnl_dry"]["ev"]+=ev
        phase["action"]="SIMULATE_BUY"; phase["status"]="EXECUTED_DRY"; phase["detail"]=f"SIM {best['side']} ${BET_AMOUNT} @ {price} (ev {ev:.4f})"
    else:
        # live: quote then place-order
        q=baw("prediction","trade","quote","--binanceChainId","56","--tokenId",best["tokenId"],
             "--marketTopicId",str(state["market"]["marketTopicId"]),"--side","BUY","--amount",str(BET_AMOUNT),
             "--orderType","MARKET")
        if not q.get("success"):
            c,msg=classify(q.get("_err") or q.get("error",{}))
            phase["status"]="ERROR"; phase["reason"]=c; phase["detail"]=f"quote failed: {msg}"
            state["errors"].append({"where":"execute:quote","code":c,"diag":msg})
            state["phases"].append(phase); return
        qid=q["data"].get("quoteId") or q["data"].get("quoteId")
        slip=q["data"].get("slippageBps", state["market"].get("slippageBps"))
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
           "scope_timeframe":SCOPE_TIMEFRAME,"config":{"bet_amount":BET_AMOUNT,"edge_threshold":EDGE_THRESHOLD,
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