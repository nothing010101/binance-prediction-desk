# binance-prediction-desk

Binance Web3 **prediction Up/Down** trading desk + monitoring dashboard.

## Scope
BTC / ETH / BNB × 5m / 15m. (SOL is NOT offered by the Binance Web3 prediction market — only BTC/ETH/BNB.)

## Layout
- `index.html` — static monitoring dashboard. Polls `state.json` from this repo (raw GitHub) every 15s.
- `predict_runner.py` — desk runner (stdlib only). Drives the `baw` CLI (`@binance/agentic-wallet`).
- `state.json` — latest run snapshot (auth, desks, simulated positions, loss analysis, errors/diagnosis).
- `log.jsonl` — append-only run history.

## Runner behaviour (loss-minimizing)
1. Auth check via `baw wallet status`.
2. Enumerate crypto up/down rounds via `baw prediction market list` + `detail` (read-only).
3. Edge model: `edge_net = (modelChance - impliedPrice) - fee - slippage`. Enter only if `edge_net >= EDGE_THRESHOLD`, liquidity ok, time-to-settlement ok, and desk not cooled down. Otherwise SKIP. (Placeholder edge — mostly skips, which is the correct conservative default until real alpha is wired in.)
4. **Dry mode (default):** simulates fill from price/chance. NEVER calls `baw prediction trade place-order`.
5. **Loss analysis:** pulls `baw prediction position settled-history`; maps to desks via `imageUrl`; cools down a desk after `COOLDOWN_LOSSES` recent losses, with the likely cause logged.
6. **Error handling:** each `baw` call is wrapped; failures are classified (AUTH_REQUIRED / BALANCE_LOW / RATE_LIMIT / TIMEOUT / BAW_MISSING) with an actionable diagnosis. Auto-fixes what it can (retry on rate-limit); logs the rest for a human.
7. Pushes `state.json` + `log.jsonl` back to this repo via the GitHub contents API when `GITHUB_TOKEN` + `GH_REPO` are set.

## Env
| var | default | meaning |
|---|---|---|
| `DRY_RUN` | `1` | dry simulate (no `place-order`) |
| `BET_AMOUNT` | `1` | placeholder USDT for dry fills (bet sizing decided later) |
| `EDGE_THRESHOLD` | `0.03` | net edge required to enter |
| `MIN_LIQUIDITY` | `2000` | min USDT liquidity |
| `MIN_TIME_TO_END_MS` | `60000` | don't enter if < this to settlement |
| `COOLDOWN_LOSSES` | `3` | pause desk after N recent losses |
| `GH_REPO` | — | `owner/repo` to push state |
| `GITHUB_TOKEN` | — | token for the contents API |
| `BAW_BIN` | `baw` | path to the baw CLI |

## Live trading prerequisites (not enabled yet)
- `baw` CLI installed (`@binance/agentic-wallet`) and paired (`baw auth signin`).
- Wallet funded with USDT on BSC (chain 56). The wallet currently has only gas BNB (~$0.07), no USDT.
- `DRY_RUN=0`. Bet sizing to be set after the strategy is validated.

> Not financial advice. Prediction-market trading can lose the entire stake.
