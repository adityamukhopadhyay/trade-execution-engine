# Portfolio Trade Execution Engine

A FastAPI service that takes a target portfolio (a first-time list of stocks and quantities, or explicit SELL / BUY / REBALANCE instructions), connects to the user's broker and places the delivery orders in one request. The paper broker is the default, so it runs in Docker with no broker account; Zerodha, Fyers, Angel One, Upstox and Groww sit behind the same interface.

## Run

```sh
docker compose up --build
```

UI at http://localhost:8000, OpenAPI at /docs. Demo:

1. Connect "Paper (simulated)" in the Broker panel; no credentials.
2. "Load sample: first-time", keep "Dry run" ticked, Execute: PLANNED, four BUY legs, nothing placed.
3. Untick dry run, Execute: 202 at once; the page follows the run over the WebSocket to FILLED and MATCH.
4. Execute the same body again: the bound `idempotency_key` returns the stored run with `Idempotent-Replay: true`.
5. Disconnect, put `INFY:10,TCS:5,RELIANCE:8` in `seed_holdings`, Connect again, "Load sample: rebalance", Execute: SELLs finish before BUYs start.
6. `/mock/webhook` holds the `run.completed` body the app posted to itself.

Tests: `docker compose --profile test run --rm tests`, or `pip install -e ".[dev]"` then `pytest -q`; no network. `docker compose --profile chaos up --build` adds a copy on port 8001 with rejections, throttling, ambiguous outcomes and partial fills on. `.env.example` lists every setting and default.

## Architecture

```
POST /executions
  -> validate     symbols, duplicates, oversells, checked against live holdings; any issue is a 422, nothing placed
  -> plan         instructions become tagged MARKET / delivery / DAY orders, SELLs first
  -> SELL phase   place concurrently, poll until terminal
  -> gate         every SELL filled? otherwise halt (BUYs SKIPPED) or continue
  -> BUY phase    same as the SELL phase
  -> reconcile    re-read holdings, compare with what the fills imply
  -> notify       console, webhook, WebSocket
```

| Path | What it does |
|---|---|
| `app/core/models.py` | Every shared model: payloads, statuses, the report, sessions |
| `app/core/instruments.py` | Symbol table from `data/instruments.json` |
| `app/brokers/base.py` | The adapter contract, `BrokerMeta`, the `BrokerError` family |
| `app/brokers/http.py` | One `send()` that classifies transport failures |
| `app/brokers/ratelimit.py` | Per-broker token buckets and in-flight cap |
| `app/brokers/paper.py` | Simulated broker with fault injection |
| `app/brokers/{zerodha,fyers,angelone,upstox,groww}.py` | One thin httpx adapter each |
| `app/brokers/registry.py` | Name to adapter; the one line a sixth broker adds |
| `app/execution/validator.py` | Pure checks against the holdings snapshot |
| `app/execution/planner.py` | Instructions to legs to tagged orders |
| `app/execution/engine.py` | Phases, retries, ambiguity resolution, the gate |
| `app/execution/poller.py` | Polls open orders until terminal or deadline |
| `app/execution/reconciler.py` | Advisory holdings check after a run |
| `app/execution/service.py` | Idempotency, session, validate, plan, start the run |
| `app/execution/store.py` | `RunRepository` and its in-memory implementation |
| `app/api/`, `app/notifications/` | Routers and error mapping; console, webhook and WebSocket sinks |
| `app/static/` | Single-page UI, no build step, two sample payloads |

Reading order: `core/models.py → brokers/base.py → brokers/paper.py → execution/validator.py → execution/planner.py → execution/engine.py → execution/poller.py → api/executions.py → notifications/base.py`.

Design decisions:

- Adapters implement five methods (`complete_login`, `get_holdings`, `get_order`, `list_orders`, `place_order`) behind two auth shapes: redirect-and-exchange (Zerodha, Fyers, Upstox) and direct credentials (Angel One, Groww, Paper).
- SELLs run as a phase before BUYs: sale proceeds fund the buys; sending both together draws RMS margin rejections.
- Every exception ends inside one `OrderResult`: a bad order never touches its neighbours and the run always ends terminal.
- Retries only for what provably never reached the broker (connect errors, 429s), with capped backoff and jitter. A timeout after sending becomes AMBIGUOUS, looked up by client tag, never resent; not found means UNKNOWN.
- Per-adapter token buckets (per second and per minute, orders and reads apart) plus an in-flight cap keep a 200-order run within limits.
- Same `idempotency_key` and payload: the stored run; different payload: 409.

## Rebalance logic

`first_time` lines (`{symbol, exchange, quantity}`) become BUYs; the validator requires an empty account unless `allow_existing_holdings` is set. `rebalance` instructions: `SELL` and `BUY` carry a `quantity`, `REBALANCE` a signed `quantity_delta` on a held symbol, split by sign into a SELL of `|delta|` or a BUY of `delta`.

The engine does not compute the delta: the payload says what to trade, and the engine checks it against live holdings before placing anything (unknown symbols, mismatched ISIN, duplicates, oversells, REBALANCEs below zero, BUYs of a held symbol), all reported in one 422.

The gate after the SELL phase requires every SELL to be FILLED; with `on_sell_shortfall: "halt"` (default) the BUYs are SKIPPED, with `"continue"` they go ahead and the shortfall lands in `plan.warnings`. `dry_run: true` returns the plan and places nothing.

```json
{"session_id": "…", "idempotency_key": "demo-first-001",
 "portfolio": {"mode": "first_time",
   "lines": [{"symbol": "INFY", "exchange": "NSE", "quantity": 10},
             {"symbol": "TCS", "exchange": "NSE", "quantity": 5}]}}
```

```json
{"session_id": "…", "idempotency_key": "demo-rebal-001", "on_sell_shortfall": "halt",
 "portfolio": {"mode": "rebalance",
   "instructions": [{"action": "SELL", "symbol": "TCS", "quantity": 5},
                    {"action": "REBALANCE", "symbol": "INFY", "quantity_delta": -4},
                    {"action": "REBALANCE", "symbol": "RELIANCE", "quantity_delta": 2},
                    {"action": "BUY", "symbol": "HDFCBANK", "quantity": 6}]}}
```

## Brokers

| Broker | Login | Symbol sent | Product | Tag field | Limits used | Status read from |
|---|---|---|---|---|---|---|
| Paper | none | symbol | n/a | `tag` | 10/s, 400/min | in-memory book |
| Zerodha | redirect, paste `request_token` | `INFY` + `NSE` | `CNC` | `tag` (20) | 10/s, 400/min; reads 10/s | `GET /orders/{id}`, last history row |
| Fyers | redirect, paste `auth_code` | `NSE:INFY-EQ` | `CNC` | `orderTag` (30) | 10/s, 100/min; reads 10/s | `GET /orders?id=` |
| Angel One | client code + MPIN + TOTP | `INFY-EQ` + `symboltoken` | `DELIVERY` | `ordertag` (19) | 10/s, 500/min; reads 1/s | per-order endpoint by `uniqueorderid`, else the book |
| Upstox | redirect, paste `code` | `NSE_EQ\|<ISIN>` | `D` | `tag` (40) | 10/s, 500/min; reads 50/s | `GET /order/details` |
| Groww | pasted token, or API key + TOTP | `INFY` + `NSE` + `CASH` | `CNC` | `order_reference_id` (20) | 10/s, 250/min; reads 20/s | `GET /order/detail/{id}`; by reference for tag lookups |

The five real adapters are not live-tested: written from the documented contracts and verified against recorded fixtures with respx; no order has gone through a real account. Open points are marked UNVERIFIED in the source; `GET /brokers` reports `live_tested`.

Why thin adapters and not a library: the official SDKs are synchronous and pin against each other. fyers-apiv3 pins `requests==2.31.0` and `aiohttp==3.9.3` while growwapi needs `requests>=2.32.3` and `aiohttp>=3.11.18`, so they cannot coexist; kiteconnect pins `autobahn[twisted]==19.11.2`, pulling in Twisted for a ticker I never use. OpenAlgo is a Flask platform with its own login UI, one broker and one account per container, and AGPL, which does not fit per-user broker sessions. fenix is GPL and synchronous. Each adapter is 115 to 160 lines of httpx; OpenAlgo's broker plugins were my reference for endpoint paths and status mappings. The 63-name NSE instrument seed is rebuilt from the official masters with `scripts/refresh_instruments.py`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/brokers` | Brokers, required credentials, `live_tested` |
| POST | `/brokers/{broker}/sessions` | Connect; returns a `session_id`, never the token |
| GET | `/sessions/{session_id}/holdings` | Live holdings through the adapter |
| POST | `/executions` | Submit; 202 running, 200 dry run or replay, 409 key conflict, 422 invalid |
| GET | `/executions/{run_id}` | The report, live while the run is in progress |
| WS | `/ws/executions/{run_id}` | A `run.snapshot` frame, then every event |
| GET | `/mock/webhook` | Bodies the webhook sink posted; `WEBHOOK_URL` sends them elsewhere |
| GET | `/health` | Liveness, version, registered brokers |

## Limitations

- In-memory state: sessions and runs vanish on restart and a mid-run crash leaves orders at the broker with no report; a persistent `RunRepository` is the first production step.
- No OAuth callback route; open the login URL and paste the returned token.
- MARKET, delivery, DAY only; no limit prices, cancel or modify. Orders open at the poll deadline are reported TIMED_OUT or PARTIALLY_FILLED, not cancelled.
- No funds check; the broker's RMS decides and its rejection is reported verbatim.
- Market-hours warning only, no holiday calendar; symbols outside the 63-name seed are rejected until the refresh script runs.
