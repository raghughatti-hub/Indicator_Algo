# Review rectification

The changes address the reproducible execution, strategy and credential defects from the repository review. They remain a proposed working-tree change, without live broker certification.

- Orders maintain a persisted broker-order ledger. Actual cumulative fills set position size and realised P&L. Partial entries and exits retain remaining exposure. Concurrent exits reserve one submission; cancellation must be confirmed before replacement. Submission timeouts are retained as unknown and block new exposure. Exact client-tag reconciliation and idempotent registration support recovery.
- Manual reductions cannot exceed available quantity; derivative quantities require verified lots. Missing positions and failed position responses do not imply closure. Raw untracked live-order submission is blocked; live operations use the runner's tracked controls.
- Pausing stops entries while keeping protection running. Restart loads durable private state with entries paused. Different broker/account attachment is blocked with exposure. Unreadable state is preserved and blocks trading. A process lock prevents two engines owning the same runtime directory.
- Exchange times use Asia/Kolkata. Current incomplete candles are excluded; fabricated warmup candles are removed. Supertrend now initializes correctly, Wilder smoothing has an SMA seed, and Supertrend filtering is an explicit opt-in. Terminal strategy exits are distinct from intermediate profit levels. Backtests prioritize pre-existing stops on ambiguous candles and apply raised trailing levels on the next candle.
- Spot risk uses underlying prices and signal direction. Option risk uses premium. Daily limits pause entries and request liquidation. Manual cost stops use the correct risk basis. Exact contract selection honors future expiry, rejects expired contracts, and avoids loose derivative searches.
- Upstox MIS maps to intraday product I. Numeric mappings are exchange-scoped, current-session candles come from the intraday endpoint, and historical requests are authenticated. Stale streaming quotes fall back to REST. Legacy broker SDK HTTP calls have bounded connection/read timeouts without replacing the global requests module.
- Broker status no longer returns session tokens. API controls require a local HttpOnly cookie or explicit bearer token. Remote control requires configured authentication; cross-origin requests are rejected. OAuth state expires and is single-use. Upstox uses browser OAuth rather than an undeclared TOTP package. Runtime token saves are atomic and private, and environment variables retain priority.
- Credential files and bytecode are removed from the proposed Git index and ignored, with local credential files preserved. README now documents actual behavior. Dashboard alerts use confirmed exposure instead of submitted IDs, and pausing preserves dashboard polling.

## Verification

72 automated regression tests pass with simulated broker responses. Dashboard, authenticated API, CSV ingestion and a 600-bar backtest with signals pass HTTP smoke tests. Authentication rejection, cross-origin rejection, token-free status and blocking untracked live orders pass HTTP checks. Python compilation, undefined-name checks, JavaScript syntax, dependency consistency and patch whitespace checks pass.

## Remaining work and limits

The account owner must revoke/rotate credentials committed previously. They remain in historical Git objects; coordinated history cleanup has not been performed.

Live broker connectivity and actual order fills have not been tested. Static outbound IP provisioning and broker allowlisting must be configured in hosting and with the broker. Zebu/FlatTrade callbacks must return OAuth state; missing state is rejected. Upstox streaming requires the optional official SDK, which was not installed or tested in this environment. REST behavior has regression coverage with mocks.

Continuous execution uses reserved workers; risk quotes and position refreshes run outside the state lock. Some legacy/manual operations still hold the lock during bounded network requests. Broker-wide rate budgeting and native stop-limit support remain future improvements.

Local protection depends on a running process and broker connectivity; a persisted state file cannot protect trades during an outage. Risk exits use limit orders and may remain unfilled. Manual changes made outside the application need broker reconciliation. Exchange holidays, actual liquidity, fees/slippage, exact Pine indicator parity and profitability are not certified by these checks.

## Continuous execution optimisation

Added fixed, persisted entry/normal-exit/protective-exit boundaries, point-or-percentage allowances, entry expiry, spread checks, continuous pending-order management and latched exits. Market orders are rejected at API schema and broker-adapter level. Partial entries are protected and exit cancellation reconciles late entry fills before choosing exit quantity. Dashboard settings, price boundaries, alerts and voice messages expose unresolved execution. Completed-candle eligibility now uses candle close time when deciding whether a signal predates runner startup.

Regression cases include price gaps, non-widening boundaries, partial/recovered fills, cancellation uncertainty, restart preservation, concurrent managed exits, missing fill prices, stale order events, paper/live boundary consistency and responsiveness during slow quote calls. Execution limits remain deliberately configurable; no live broker test or native stop-limit deployment was performed.
