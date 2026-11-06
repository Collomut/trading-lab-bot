# Binance Spot Testnet: Protocol Limits vs Live Execution Reality (F-26)

## Executive Summary
This bot is engineered and configured exclusively for **Binance Spot Testnet** and simulated paper trading.
**Live trading is strictly disabled in code (`exchange.py`) to guarantee zero real-money capital risk.**

While Spot Testnet is invaluable for rehearsing API contracts, order lifecycles, and failure recovery, operators must understand its severe behavioral limitations compared to real live exchange trading.

---

## Key Differences

| Feature | Binance Spot Testnet | Live Production Exchange |
| :--- | :--- | :--- |
| **Capital Risk** | Zero. All balances are virtual test funds. | 100% real capital at risk. |
| **Order Book Liquidity** | Thin, synthetic, or non-existent order books. Market orders may match against stale virtual quotes. | Deep liquidity, institutional market makers, and rapid bid-ask updates. |
| **Execution Quality & Fills** | Market orders fill almost unconditionally at virtual prices. Does not simulate real-world slippage. | Subject to adverse selection, partial fills, queue priority, and slippage. |
| **Rate Limits & WAF** | Different IP throttle policies, frequent maintenance downtimes, and periodic balance wipes. | Strict IP/UID request weight limits (`-1003`, `429`, `418`), IP whitelisting. |
| **Persistence & History** | Binance periodically resets Testnet balances, trades, and open orders to default values. | Immutable trade history, permanent balances, and durable open order books. |

---

## Operating Guidelines for this Repository
1. **Never treat Testnet win rate or P&L as evidence of a profitable trading strategy.** Testnet fills are not realistic.
2. **Use Testnet strictly as an operational harness** to verify order routing, native exchange stop placement, cancel/replace synchronization, and emergency-stop mechanisms.
3. If Testnet resets occur, follow the [Testnet Reset Recovery Runbook](testnet_reset_recovery.md).
