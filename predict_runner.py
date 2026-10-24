#!/usr/bin/env python3
"""
Binance Web3 Prediction Up/Down — desk runner.
Scope: BTC / ETH / BNB × 5m / 15m (SOL is not offered by the Binance Web3 prediction market).

DESIGN (honest, loss-minimizing):
  * Read-only market enumeration via `baw prediction market list/detail`.
  * Edge model: edge_net = (modelChance - impliedPrice) - feeBps - slippageBps, per USDT.
    Enter only if edge_net >= EDGE_THRESHOLD AND liquidity ok AND time-to-settlement ok AND not cooled down.
    This is a PLACEHOLDER edge (model chance vs market price). It will mostly SKIP, which is the
    correct conservative behaviour until a real alpha signal is wired in.
  * Dry mode (default): simulates the fill from price/chance. NEVER calls `baw prediction trade place-order`.
  * Loss analysis: pulls `baw prediction position settled-history` (real), classifies recent losses
    per (symbol, timeframe), and COOLS DOWN a desk after COOLDOWN_LOSSES recent losses — that is the
    "find the likely cause and fix" step: the fix is to pause the desk, with the hypothesis logged.
  * Error handling: each baw call is wrapped; failures are classified with an actionable diagnosis.
    Auto-fixable (rate-limit/network) -> retry. Not auto-fixable (AUTH_REQUIRED, BALANCE_LOW,
    BAW_MISSING) -> logged so a human / next run can act.

State is written to state.json (+ append log.jsonl) and pushed to GitHub via the contents API
when GITHUB_TOKEN + GH_REPO are present. The website dashboard reads raw GitHub.
"""
import os, sys, json, subprocess, time, urllib.request, urllib.error, datetime, hashlib, traceback

# ---- config (env-overridable) ----
DRY_RUN        = os.environ.get("DRY_RUN", "1") != "0"
BET_AMOUNT     = float(os.environ.get("BET_AMOUNT", "1"))        # placeholder, dry
EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.03")) # net edge after fees to enter
MIN_LIQUIDITY  = float(os.environ.get("MIN_LIQUIDITY", "2000"))
MIN_TTE_MS     = int(os.environ.get("MIN_TIME_TO_END_MS", "60000"))
COOLDOWN_LOSSES= int(os.environ.get("COOLDOWN_LOSSES", "3"))
COOLDOWN_WINDOW= int(os.environ.get("COOLDOWN_WINDOW", "20"))    # last N settled to look at
GH_REPO        = os.environ.get("GH_REPO", "")                   # e.g. BADAKKAKITIGA/binance-prediction-desk
GH_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
BAW            = os.environ.get("BAW_BIN", "baw")

SCOPE = {
    ("BTCUSDT", "5 min"):  {"desk": "BTC-5m",  "coin": "BTC"},
    ("BTCUSDT", "15 min"): {"desk": "BTC-15m", "coin": "BTC"},
    ("ETHUSDT", "5 min"):  {"desk": "ETH-5m",  "coin": "ETH"},
    ("ETHUSDT", "15 min"): {"desk": "ETH-15m", "coin": "ETH"},
    ("BNBUSDT", "5 min"):  {"desk": "BNB-5m",  "coin": "BNB"},
    ("BNBUSDT", "15 min"): {"desk": "BNB-15m", "coin": "BNB"},
}

def now_ms(): return int(time.time()*1000)
def iso(): return datetime.datetime.now(datetime.timezone.utc).isoformat()

def baw(*args, retries=1):
    """Run a baw subcommand, return parsed JSON dict (or {'_raw':...,'_err':...})."""
    for attempt in range(retries+1):
        try:
            r = subprocess.run([BAW]+list(args)+"--json" if False else [BAW]+list(args)+["--json"],
                               capture_output=True, text=True, timeout=60)
            out = r.stdout.strip() or "{}"
            try:
                j = json.loads(out)
            except json.JSONDecodeError:
                # some commands print logs before JSON; take last JSON-ish line
                j = {"_parse_failed": True, "_stdout": out[:500], "_stderr": r.stderr[:500], "_code": r.returncode}
            if r.returncode != 0 and not isinstance(j, dict):
                j = {"_err": r.stderr[:500], "_code": r.returncode}
            j.setdefault("_code", r.returncode)
            j["_stderr"] = r.stderr[:500]
            # retry on rate-limit
            if r.returncode != 0 and attempt < retries and ("429" in r.stderr or "rate" in r.stderr.lower()):
                time.sleep(2*(attempt+1)); continue
            return j
        except subprocess.TimeoutExpired:
            if attempt < retries: time.sleep(2); continue
            return {"_err": "TIMEOUT", "_args": list(args)}
        except FileNotFoundError:
            return {"_err": "BAW_MISSING", "_msg": f"{BAW} not found on PATH"}
        except Exception as e:
            return {"_err": "EXC", "_msg": str(e), "_tb": traceback.format_exc()[:600]}
    return {"_err": "RETRY_EXHAUSTED"}

def classify(err):
    s = (str(err)).lower()
    if "not logged in" in s or "auth" in s and "log" in s: return ("AUTH_REQUIRED", "Wallet not logged in. Re-pair via `baw auth signin` (needs QR scan). Cannot auto-fix without a human scan.")
    if "insufficient" in s and "balanc" in s: return ("BALANCE_LOW", "Wallet has no USDT on BSC. Fund the wallet with USDT (chain 56) before live trading. Dry run still works.")
    if "429" in s or "rate" in s: return ("RATE_LIMIT", "Binance rate-limited the request. Runner retries automatically; spaced runs help.")
    if "timeout" in s: return ("TIMEOUT", "baw call exceeded 60s. Market data endpoint may be slow; retry.")
    if "not found" in s: return ("BAW_MISSING", "baw CLI not installed in this sandbox. Install @binance/agentic-wallet globally and re-pair.")
    return ("UNKNOWN", str(err)[:160])

def fetch_scope_markets():
    """Return list of marketTopic dicts in scope, with fresh detail."""
    lst = baw("prediction","market","list","--l1Category","crypto","--l2Category","up-down","--limit","100")
    if "_err" in lst or not lst.get("success"):
        return [], lst
    topics = lst.get("data",{}).get("marketTopics", [])
    out = []
    for t in topics:
        sym, tf = t.get("symbol"), t.get("timeframe")
        key = (sym, tf)
        if key not in SCOPE: continue
        det = baw("prediction","market","detail","--marketTopicId", str(t.get("marketTopicId")))
        d = det.get("data", t) if det.get("success") else t
        d["_scope"] = SCOPE[key]
        d["_detail_err"] = None if det.get("success") else det.get("_err") or det.get("error",{})
        out.append(d)
    return out, None

import re as _re
_COIN_MAP = {"btc":"BTC","eth":"ETH","bnb":"BNB","sol":"SOL","bitcoin":"BTC","ethereum":"ETH"}

def _desk_from_position(p):
    """Map a settled position to a scope desk (or None) via imageUrl / title."""
    img = (p.get("imageUrl") or "").lower()
    m = _re.search(r"automarket-([a-z]+)-usd-(\d+)-minutes", img)
    if m:
        coin=_COIN_MAP.get(m.group(1)); iv=int(m.group(2))
    else:
        t=(p.get("marketTopicTitle") or p.get("marketTopicQuestion") or "").lower()
        coin=None
        for k,v in _COIN_MAP.items():
            if k in t: coin=v; break
        # interval from time span like 4:30am-4:35am -> 5 ; 4:30-4:45 -> 15
        span=_re.search(r"(\d{1,2}):(\d{2})\s*[ap]m\s*[-\u2013]\s*(\d{1,2}):(\d{2})\s*[ap]m", t)
        iv=None
        if span:
            mins=(int(span.group(3))*60+int(span.group(4)))-(int(span.group(1))*60+int(span.group(2)))
            iv=mins if mins in (5,15,60,1440) else None
    if not coin or not iv: return None
    for (sym,tf),meta in SCOPE.items():
        if meta["coin"]==coin and tf==f"{iv} min": return meta["desk"]
    return None

def fetch_loss_history():
    """One read-only call. Returns (per-desk counts, raw positions in scope)."""
    r = baw("prediction","position","settled-history","--l1Category","crypto","--limit","100")
    if not r.get("success"): return {}, []
    positions = r.get("data",{}).get("positions",[])
    counts={}; raw=[]
    for p in positions:
        desk=_desk_from_position(p)
        if not desk: continue
        won = (p.get("result")=="WIN") or p.get("isWinner") is True
        d=counts.setdefault(desk,{"wins":0,"losses":0,"recent_losses":[]})
        if won: d["wins"]+=1
        else:
            d["losses"]+=1
            d["recent_losses"].append({
                "desk":desk,"outcome":p.get("outcomeName"),"avgPrice":p.get("avgPrice"),
                "shares":p.get("shares"),"cost":p.get("totalCost"),"result":p.get("result"),
                "title":p.get("marketTopicTitle"),
                "hypothesis":f"Bought {p.get('outcomeName')} @ {p.get('avgPrice')} but settled {p.get('result')}; "
                             f"move went against the side. Tighten edge threshold / cooldown this desk."})
        raw.append({"desk":desk,"outcome":p.get("outcomeName"),"result":p.get("result"),
                    "avgPrice":p.get("avgPrice"),"cost":p.get("totalCost"),"shares":p.get("shares")})
    return counts, raw

def github_put(path, content_bytes, message):
    """Create-or-update a file in GH_REPO via contents API. Returns sha or None."""
    if not (GH_TOKEN and GH_REPO): return None
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    # get current sha
    sha = None
    try:
        req = urllib.request.Request(url, headers={"Authorization":f"Bearer {GH_TOKEN}","Accept":"application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            sha = json.load(r).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404: return None
    except Exception:
        pass
    import base64
    body = json.dumps({"message": message, "content": base64.b64encode(content_bytes).decode(),
                       "sha": sha}).encode()
    try:
        req = urllib.request.Request(url, data=body, method="PUT",
                headers={"Authorization":f"Bearer {GH_TOKEN}","Accept":"application/vnd.github+json","Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("content",{}).get("sha")
    except Exception as e:
        return None

def main():
    run_id = hashlib.sha256(str(now_ms()).encode()).hexdigest()[:10]
    state = {"run_id": run_id, "ts": iso(), "dry_run": DRY_RUN, "scope": [v["desk"] for v in SCOPE.values()],
             "config": {"bet_amount": BET_AMOUNT, "edge_threshold": EDGE_THRESHOLD,
                        "min_liquidity": MIN_LIQUIDITY, "min_tte_ms": MIN_TTE_MS,
                        "cooldown_losses": COOLDOWN_LOSSES},
             "auth": None, "balance": [], "desks": [], "sim_positions": [],
             "losses": {}, "errors": [], "skips": [], "pnl_dry": {"trades":0,"cost":0.0,"expected_payout":0.0,"ev":0.0},
             "summary": ""}
    log = {"run_id": run_id, "ts": iso(), "dry_run": DRY_RUN}

    # 1. auth
    st = baw("wallet","status")
    wstat = (st.get("data") or {}).get("status") if isinstance(st.get("data"), dict) else None
    if st.get("success") and wstat in ("CONNECTED", "LOGGED_IN"):
        state["auth"] = wstat or "CONNECTED"
    elif st.get("_err") == "BAW_MISSING":
        c,msg = classify("BAW_MISSING")
        state["errors"].append({"where":"baw_status","code":c,"diag":msg})
        state["auth"] = c; state["summary"]="baw CLI missing — install & re-pair"; return finish(state, log)
    else:
        c,msg = classify(st.get("_err") or st.get("error",{}).get("message") or "auth")
        state["errors"].append({"where":"baw_status","code":c,"diag":msg})
        state["auth"] = c

    # 2. balance (informational)
    bal = baw("wallet","balance")
    if bal.get("success"):
        state["balance"] = bal.get("data",[])
    has_usdt = any((b.get("symbol","")=="USDT" and float(b.get("balance","0"))>0) for b in state["balance"]) if state["balance"] else False
    if not DRY_RUN and not has_usdt:
        state["errors"].append({"where":"balance","code":"BALANCE_LOW",
            "diag":"Live mode requested but no USDT on BSC. Refusing to trade; staying dry."})
        DRY_RUN_FLAG = True
    else:
        DRY_RUN_FLAG = DRY_RUN

    # 3. loss history -> cooldowns
    counts, raw_losses = fetch_loss_history()
    state["losses"] = counts
    state["loss_history"] = raw_losses
    cooled = {k for k,v in counts.items() if v.get("losses",0) >= COOLDOWN_LOSSES}
    if cooled:
        state["errors"].append({"where":"cooldown","code":"COOLDOWN",
            "diag":f"Desks paused after >= {COOLDOWN_LOSSES} recent losses: {sorted(cooled)}. Hypothesis: adverse regime for those symbol/interval pairs; pause until losses age out."})

    # 4. enumerate + screen + simulate
    markets, merr = fetch_scope_markets()
    if merr:
        c,msg = classify(merr.get("_err") or str(merr))
        state["errors"].append({"where":"market_list","code":c,"diag":msg})
        state["summary"]=f"market list failed: {c}"; return finish(state, log)
    for m in markets:
        desk = m["_scope"]["desk"]
        tte = (m.get("endDate") or 0) - now_ms()
        liq = float(m.get("liquidity") or 0)
        outs = m.get("markets",[{}])[0].get("outcomes",[])
        if not outs:
            state["skips"].append({"desk":desk,"reason":"NO_OUTCOMES"}); continue
        # best side by edge_net
        best=None
        for o in outs:
            price=float(o.get("price",0) or 0); chance=float(o.get("chance",0) or 0)
            fee=float(m.get("feeRateBps",0))/1e4; slip=float(m.get("slippageBps",0))/1e4
            edge_net=(chance-price)-fee-slip
            if best is None or edge_net>best["edge_net"]:
                best={"side":o["name"],"price":price,"chance":chance,"edge_gross":round(chance-price,4),
                      "fee":fee,"slip":slip,"edge_net":round(edge_net,4),"tokenId":o["tokenId"]}
        desk_rec={"desk":desk,"symbol":m.get("symbol"),"timeframe":m.get("timeframe"),
                  "status":"LIVE" if m.get("liveStatus")=="LIVE" else m.get("status"),
                  "tte_ms":tte,"liquidity":liq,"best":best,"action":"SKIP","reason":None}
        if desk in cooled:
            desk_rec["action"]="SKIP"; desk_rec["reason"]="COOLDOWN_LOSSES"
        elif best is None:
            desk_rec["reason"]="NO_EDGE_DATA"
        elif best["edge_net"] < EDGE_THRESHOLD:
            desk_rec["action"]="SKIP"; desk_rec["reason"]=f"EDGE_TOO_LOW({best['edge_net']})"
        elif liq < MIN_LIQUIDITY:
            desk_rec["action"]="SKIP"; desk_rec["reason"]=f"LOW_LIQUIDITY({liq:.0f})"
        elif tte < MIN_TTE_MS:
            desk_rec["action"]="SKIP"; desk_rec["reason"]=f"TOO_CLOSE_TO_SETTLEMENT({tte}ms)"
        else:
            # DRY simulate fill (or live quote/place — but live needs USDT & is disabled here)
            if DRY_RUN_FLAG:
                shares = BET_AMOUNT / best["price"] if best["price"]>0 else 0
                payout_if_win = shares * 1.0
                ev = shares*best["chance"] - BET_AMOUNT
                pos={"run_id":run_id,"desk":desk,"side":best["side"],"price":best["price"],
                     "chance":best["chance"],"amount":BET_AMOUNT,"shares":round(shares,4),
                     "cost":BET_AMOUNT,"payout_if_win":round(payout_if_win,4),"ev":round(ev,4),
                     "edge_net":best["edge_net"],"dry":True,"ts":iso()}
                state["sim_positions"].append(pos)
                state["pnl_dry"]["trades"]+=1
                state["pnl_dry"]["cost"]+=BET_AMOUNT
                state["pnl_dry"]["expected_payout"]+=payout_if_win*best["chance"]
                state["pnl_dry"]["ev"]+=ev
                desk_rec["action"]="SIMULATE_BUY"; desk_rec["reason"]="OK (dry)"
            else:
                desk_rec["action"]="WOULD_LIVE_TRADE"; desk_rec["reason"]="live path not enabled in this build"
        state["desks"].append(desk_rec)

    state["pnl_dry"]["cost"]=round(state["pnl_dry"]["cost"],4)
    state["pnl_dry"]["expected_payout"]=round(state["pnl_dry"]["expected_payout"],4)
    state["pnl_dry"]["ev"]=round(state["pnl_dry"]["ev"],4)
    n_act=sum(1 for d in state["desks"] if d["action"].startswith("SIMULATE"))
    state["summary"]=f"{'DRY' if DRY_RUN_FLAG else 'LIVE'} run {run_id}: {len(state['desks'])} desks, {n_act} simulated, {len(state['skips'])}+skips, {len(state['errors'])} errors."
    log["desks"]=[{k:d[k] for k in ('desk','action','reason','tte_ms','liquidity')} for d in state["desks"]]
    log["errors"]=state["errors"]
    return finish(state, log)

def finish(state, log):
    os.makedirs(os.path.join(os.path.dirname(__file__),"..","files"), exist_ok=True)
    spath=os.path.join(os.path.dirname(__file__),"state.json")
    lpath=os.path.join(os.path.dirname(__file__),"log.jsonl")
    with open(spath,"w") as f: json.dump(state,f,indent=1)
    # S3 mount forbids append; read-then-rewrite the whole log
    prev=""
    try:
        with open(lpath,"r") as f: prev=f.read()
    except Exception: pass
    with open(lpath,"w") as f: f.write(prev + json.dumps(log)+"\n")
    # push to github
    if GH_TOKEN and GH_REPO:
        with open(spath,"rb") as f: github_put("state.json", f.read(), f"run {state['run_id']} state")
        with open(lpath,"rb") as f: github_put("log.jsonl", f.read(), f"run {state['run_id']} log")
    print(json.dumps({"summary":state["summary"],"auth":state["auth"],
                      "desks":len(state["desks"]),"sim":len(state["sim_positions"]),
                      "errors":len(state["errors"]),"pushed": bool(GH_TOKEN and GH_REPO)}, indent=1))
    return state

if __name__ == "__main__":
    main()