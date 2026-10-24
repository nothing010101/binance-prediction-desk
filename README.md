# binance-prediction-desk

Binance Web3 **prediction Up/Down** trading desk (BTC 5m) + monitoring dashboard.

## Architecture — 4 phases, 1 wallet
One scheduled agent runs 4 phases per tick. Only the **Execute** phase may call
`baw prediction trade place-order`; phases 1-3 only write proposals/state.
Channel separation (propose vs execute) is enforced in code.

| Phase | Role | Touches wallet? |
|---|---|---|
| 1 Settle | realize PnL from newly-settled positions -> `pnl_history.json` | read-only (positions) |
| 2 Analisa | read live BTC-5m round (`baw prediction market list/detail`) | read-only |
| 3 Rencanakan | net edge (after fee + slippage), filter liquidity/TTE/cooldown | none |
| 4 Memutuskan+Eksekusi | pick side or SKIP; live `place-order` OR dry simulate | **only this one** |

## Files
- `index.html` — static dashboard. Polls `state.json` + `pnl_history.json` from raw GitHub.
- `predict_runner.py` — the 4-phase desk runner (stdlib only, drives `baw`).
- `state.json` — latest run snapshot (phases, market, PnL, errors/diagnosis).
- `pnl_history.json` — realized PnL curve (one point per run).
- `pnl_state.json` — cross-run seen-position set (de-dupe realized PnL).
- `log.jsonl` — append-only run log.

## Loss-minimizing rules
- Enter only if `edge_net >= EDGE_THRESHOLD`, liquidity ok, time-to-settlement ok, not cooled down.
- Cooldown after `COOLDOWN_LOSSES` recent realized losses (cause logged as adverse-regime hypothesis).
- Errors classified with actionable diagnosis (AUTH_REQUIRED / BALANCE_LOW / RATE_LIMIT / TIMEOUT / BAW_MISSING).

## Env
`DRY_RUN`(1) `BET_AMOUNT`(1) `EDGE_THRESHOLD`(0.03) `MIN_LIQUIDITY`(2000)
`MIN_TIME_TO_END_MS`(60000) `COOLDOWN_LOSSES`(3) `GH_REPO` `GITHUB_TOKEN` `BAW_BIN`

## Persistence reality (honest)
`baw` session lives at `~/.baw/session.json` (local disk) — wiped on sandbox reset,
**not** captured in the agent snapshot. The `baw` binary (npm global) **is** captured.
So a scheduled run may need re-pairing (QR) after a reset; until then it reports
AUTH_REQUIRED and skips execution. Full unattended autonomy needs a persistent
volume (e.g. Railway) for the pairing session.

Live trading also needs USDT on BSC (chain 56). Dry mode works without funds.

> Not financial advice. Prediction-market trading can lose the entire stake.
