# Solana Copy Trading Bot Guide: Signals, Sizing, Latency and Deployment

This guide explains how the copy trading bot turns NoLimitNodes (NLN) decoded trades into copied orders, how sizing and proportional sells work, and how to run it 24/7.

## Which NLN topics does the bot use?

| Venue flag | Topic | Schema |
|---|---|---|
| `pumpfun` | `prod.rpc.solana.pumpfun.trade` | `TradeEntry` (`user`, `mint`, `sol_amount`, `token_amount`, `ix_name`) |
| `pumpswap` | `prod.mat.solana.program.swaps.pumpswap` | `Swap` |
| `raydium_amm_v4`, `raydium_cpmm`, `raydium_clmm` | `prod.mat.solana.program.swaps.raydium_*` | `Swap` |
| `orca_whirlpool` | `prod.mat.solana.program.swaps.orca_whirlpool` | `Swap` |
| `meteora_dlmm`, `meteora_damm`, `meteora_pools` | `prod.mat.solana.program.swaps.meteora_*` | `Swap` |

`Swap` events share one schema across DEXes: `trader`, `input_mint`, `output_mint`, `input_amount` and `output_amount`. Full references are in the [NLN schema docs](https://github.com/no-limit-nodes/solana-programs-data-streaming-reference/blob/main/docs/schemas.md). Watching fewer venues with `--venues` lowers bandwidth, because every swap on a subscribed DEX is streamed, not only your targets' swaps.

## How is a trade classified?

| Event | Rule | Result |
|---|---|---|
| PumpFun trade | `ix_name` starts with `buy` / `sell` | buy / sell of `mint` |
| Swap | `input_mint` is wSOL, `output_mint` is a token | buy of `output_mint`, SOL = `input_amount` |
| Swap | `output_mint` is wSOL, `input_mint` is a token | sell of `input_mint`, SOL = `output_amount` |
| Swap | USDC/USDT on either side, token-to-token, or a missing mint | ignored |

The `direction` field on `Swap` is optional, so the bot uses the mints instead.

## How is the copy size chosen?

```text
fixed:  size = min(--buy-sol, --max-buy)
ratio:  size = clamp(target_sol × --copy-ratio, --min-buy, --max-buy)     (when --copy-ratio > 0)
```

A target buy below `--min-target-sol` is ignored. Each token is bought once per position unless `--allow-add` is set, in which case repeat buys average into the position.

## How do proportional sells work?

For each `(target wallet, mint)` the engine tracks the tokens it has seen the target buy since the bot started. When the target sells:

```text
fraction = tokens_sold / tokens_held_before_this_sell
fraction ≥ 0.95  →  sell 100%
never saw a buy   →  sell 100%
```

The bot then sells that fraction of its own balance. Realized PnL for a partial sell is the SOL received minus the same share of the position's cost basis. Only the wallet that opened a position can trigger its copied sells.

## Which exits apply besides copied sells?

Every trade on a held token, by any wallet, updates its price, and the bot checks your own rules on each update and once a second:

- **Stop loss:** `--stop-loss` (default 50%). It protects you when a target never sells, or sells in a way the bot cannot see, such as a transfer to another wallet.
- **Take profit:** `--take-profit` (off by default)
- **Max hold:** `--max-hold` seconds (off by default)

If a sell fails, the next price update or tick retries it. If the wallet holds none of the token, the position is dropped.

## What risk limits are there?

- `--max-positions` counts open positions **and** buys in flight.
- `--max-daily-loss` stops new copies after the realized loss for the current UTC day reaches the cap. Exits keep running.
- `--max-signal-age` skips PumpFun trades whose timestamp is older than N seconds, for example after a reconnect.
- `--block-mints` never buys listed tokens.

## How are live trades executed?

Each copy calls Jupiter Swap API V2 `GET /build` with `taker` set to your wallet. It compiles the returned instructions and lookup tables into a v0 transaction with a fresh blockhash from NLN RPC, signs it locally with `solders`, and sends it with `sendTransaction` through your NLN endpoint. It then confirms with `getSignatureStatuses` and reads the actual SOL change from `getTransaction`. Sells use the wallet's real token balance across SPL Token and Token-2022 accounts.

## How can I reduce copy latency?

- **Host location:** run in the same region as your NLN stream and RPC endpoints.
- **Fewer venues:** watch only the DEXes your targets use (`--venues pumpfun,pumpswap`). This cuts stream volume and queueing.
- **Priority fees:** set `--priority veryHigh` for crowded tokens. The [Solana priority fee API](https://github.com/no-limit-nodes/solana-priority-fee-api) shows what landed PumpFun transactions paid.
- **Earlier signals:** NoLimitNodes [Shredstream](https://nolimitnodes.com/products/shredstream) delivers transactions from shreds before the block is confirmed, which is the next step past the decoded stream.

## How do I run it 24/7?

```ini
[Unit]
Description=Solana copy trading bot
After=network-online.target

[Service]
WorkingDirectory=/opt/solana-copy-trading-bot
EnvironmentFile=/opt/solana-copy-trading-bot/.env
ExecStart=/opt/solana-copy-trading-bot/.venv/bin/python run_copytrader.py --telegram
Restart=always
RestartSec=5
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
```

The process exits with code 2 when NLN rejects the API key. Positions and target holdings live in memory, so after a restart the bot does not know about earlier buys. Check your wallet and close leftovers manually.

## How do I test changes?

```bash
python run_copytrader.py --replay examples/sample-events.jsonl --wallets-file examples/wallets.txt --stop-loss 30
python -m unittest discover -s tests
```

Replay files are JSON lines shaped like `{"topic": "...", "event": {...}}`. Running in paper mode with `--json` for a few days is the cheapest way to see which wallets are actually profitable to copy after your entry delay.
