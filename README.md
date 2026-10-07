# Indicator Algo

Local FastAPI trading dashboard with Zebu, FlatTrade and Upstox adapters. Live broker execution still requires broker-specific testing before unattended use.

## Setup

Use Python 3.12, create a virtual environment, and install `requirements.txt`. For optional Upstox streaming, install `requirements-upstox.txt`. Copy `.env.example` to `.env` and supply your own credentials locally. Environment variables take priority over local configuration. Start one server process:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8005
```

Open http://127.0.0.1:8005/ to obtain the local HttpOnly control cookie. API requests require this cookie or a bearer `CONTROL_API_TOKEN` of at least 32 characters. Remote access requires an explicitly configured control token; use HTTPS and enter the token in the dashboard prompt. Keep the token private. Cross-origin control requests are rejected.

A broker requiring a registered static IP needs an approved fixed outbound address. This application's code cannot assign a static cloud IP or bypass the broker restriction. Configure hosting egress and broker allowlisting separately.

Upstox uses the official browser OAuth flow. The redirect must exactly match the broker registration; default is http://localhost:8005/upstox/callback. An optional loopback bridge on port 8090 forwards to this callback. OAuth requires an expiring, single-use state. Zebu and FlatTrade browser flows also require the provider to return that state; a callback without it fails closed. Browser dependencies and provider access must be available in the host environment.

## Execution safeguards

Confirmed cumulative fills determine local quantity and realised P&L. An unconfirmed cancellation blocks replacement. An ambiguous submission pauses entries and is retained for reconciliation by its client tag; the Reconcile orders button only adopts an exact broker-tag match. An unresolved outcome requires checking the broker terminal before any further action.

Pause entries stops new exposure while monitoring and protective exits continue for open positions. Disconnecting or changing brokers is blocked with unsettled positions. Open state is saved atomically under `.runtime/` (override with `ALGO_RUNTIME_DIR`) and recovered with new entries paused. Reconnect the same account to resume monitoring; protection cannot run while the process or broker connection is down. Keep the state directory on durable storage and run exactly one engine process; a runtime lock rejects a second owner. Never delete execution state to clear a pending trade.

Daily profit/loss limits flatten tracked positions and pause entries. Spot risk uses underlying prices and signal direction; option risk uses traded premium. Quantities must match verified derivative lot sizes. Protective exits use marketable limits and can remain unfilled; review the broker terminal whenever monitoring reports a failure.

Times use Asia/Kolkata, and only completed candles enter the live strategy. The long indicator warmup requires approximately 400 real bars; shorter histories are reported as incomplete without fabricated padding. Supertrend filtering is optional and disabled by default. Backtests conservatively prioritise the existing stop when a candle hits both stop and target, apply newly raised trailing stops on the next candle, and exclude trading costs. Underlying strategy results do not represent actual option-premium returns. Paper fills require marketable bid/ask prices and cannot simulate exchange queues or actual liquidity.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use simulated broker responses and never submit live orders. They cover control authentication, OAuth state, bounded SDK HTTP calls, partial fills, uncertain cancellations/submissions, concurrent exits, quantity validation, recovery, risk direction, indicator warmup and conservative backtests. Passing them is not broker certification.

## Credentials

Local credential files, runtime tokens and bytecode are ignored and removed from the proposed Git index while local credential files are preserved. Previously committed credentials remain in Git history and must be revoked/rotated by the account owner. Do not publish reusable credentials, passwords, access tokens or TOTP secrets. History cleanup is a separate coordinated action.

Legacy SDK HTTP calls now have connection/read timeouts. Risk quote and position refreshes run outside the state lock. The continuous execution manager reserves one worker per position for polling, cancellation and submission. Legacy synchronous/manual operations still have some broker calls under the lock.

## Bounded continuous limit execution

Live runs require `continuous_execution=true` (the default). The dashboard uses this manager. Only LMT/LIMIT orders are accepted by broker adapters; there is no market-order fallback.

Entry, normal-exit and protective-exit price allowances are separate. Defaults are 0.3%, 0.3% and 0.5%, respectively; these are starting settings, not calibrated recommendations. Optional point allowances override the corresponding percentage. For example, entry reference 100 with 0.30 points sets a maximum BUY price of 100.30. A premium SL of 95 with 0.50 points sets a minimum SELL price of 94.50. Boundaries round inward to verified ticks and remain fixed across retries and recovery. For spot/strategy exits there is no exact conversion from underlying trigger to premium; the first observed traded-premium reference is used. Execution may therefore differ from the underlying stop distance.

The default entry lifetime is 10 seconds, maximum entry spread 1%, order check interval 1 second and maximum submission attempts 10. An unfilled entry remainder expires or is cancelled when its price boundary is exceeded. Partial fills remain protected. Exit instructions stay active even if price rebounds and remain visible until filled or reconciled. An exit beyond its price boundary pauses new entries and displays an alert; any existing bounded limit stays tracked. Rejections stop after the configured attempt count without widening prices. Resuming entries is blocked with an active exit instruction.

A marketable limit can fill at available prices better than its boundary. It cannot guarantee a fill when the market gaps beyond that boundary. The boundary is an execution-price constraint, not a guaranteed maximum loss. Broker-held stop-limit orders are not installed; SL/TSL monitoring still depends on the application and broker connection.

Zebu and FlatTrade order-update callbacks update the fill ledger and wake monitoring, with polling retained as fallback. Upstox uses polling for order updates. Paper execution follows the same price boundaries but does not model actual queue position/depth. Older chasing fields are retained for compatibility; the historical sweep setting refers only to a final limit attempt and does not control the dashboard's continuous manager.
