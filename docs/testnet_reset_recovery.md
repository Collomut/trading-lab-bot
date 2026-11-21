# Binance Paper Account Reset Recovery

## Safety rule

An exchange reset or manual account change can make Binance balances, open orders,
and the bot's durable records disagree. The bot must remain paused until the
exchange account is freshly read and all differences are understood. A successful
web health check means the dashboard process is alive; it does not mean trading is
safe or account reconciliation succeeded.

**Do not delete the database, clear positions, mark intents canceled, change a
balance baseline, or restart the service to try to clear a reconciliation halt.**
An order request can time out after Binance accepted it, and an order can be
partially filled. Binance documents that some server errors leave execution status
unknown; the order must be queried before any retry. See the [Spot REST API
guidance](https://developers.binance.com/en/docs/products/spot/rest-api).

## Binance Demo reset

For the configured Binance Demo environment:

1. Confirm the account was deliberately reset in Binance Demo.
2. Confirm Binance shows no open orders and inspect holdings for unexpected assets.
3. Open Trading Lab Overview and choose **Review Demo reset**.
4. The bot performs a fresh private account read, checks its own positions and
   unresolved order intents, checks Binance open orders, and records a new asset
   baseline and equity reference.
5. The review leaves entries paused. Review the dashboard result before separately
   choosing **Resume entries**.

The review must fail closed if any order, position, account read, or persistence
check is uncertain. It never submits or cancels an exchange order.

## Binance Spot Testnet reset

There is no automatic Testnet reset-and-resume workflow. Keep the bot paused and
inspect Binance balances, order history, and open orders alongside the bot's
positions and order-intent history. If any local position or order intent remains,
do not run a database reset helper: the helper now creates a backup and audit
record while preserving all trading state. Resume only after an engineer has
reconciled each item against Binance and the dashboard shows a fresh successful
account check.

The legacy `snapshot_and_reset_for_testnet_wipe()` method now records evidence
only. It deliberately does not clear positions, mark intents canceled, or remove
baselines. Its name is retained so older scripts fail safe rather than silently
losing their audit state.

## Automatic change detection and limits

The worker performs authenticated balance and open-order reconciliation on its
periodic cycle. External changes are therefore detected on the next cycle rather
than guaranteed to appear instantly. Binance also provides user data streams for
account balance and order updates; these can improve event latency but do not
replace periodic REST reconciliation or durable order-state checks. See the
[Binance Spot User Data Stream documentation](https://developers.binance.com/en/docs/products/spot/user-data-stream).

An external balance change is not automatically adopted as bot capital. The bot
cannot reliably distinguish a deliberate funding reset from a manual trade,
withdrawal, partial fill, or an order that is still active. It blocks new orders
and asks for reconciliation instead of guessing.
