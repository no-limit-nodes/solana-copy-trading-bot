# Solana Copy Trading Bot: Mirror Any Wallet on PumpFun, PumpSwap, Raydium, Orca and Meteora

An open-source **Solana copy trading bot** written in Python. Give it the wallets you want to follow, and it copies their buys and sells in real time. Sells are proportional, so if a wallet sells half its bag, you sell half of yours. It covers PumpFun bonding curves, PumpSwap, Raydium (AMM v4, CPMM, CLMM), Orca Whirlpools and Meteora (DLMM, DAMM).

Trades are detected from the [NoLimitNodes](https://nolimitnodes.com) decoded Solana gRPC streams, so the bot needs no wallet polling, transaction parsing or per-DEX decoders. Copies are routed with the Jupiter Swap API, then signed locally and sent through your [NoLimitNodes Solana RPC](https://nolimitnodes.com/products/rpc-nodes).

**Paper trading is the default.** Nothing is signed or sent until you pass `--live`.

```text
[09:41:05] 🟢 PAPER COPY BUY Toke…pump 0.0500 SOL: alpha bought 1.500 SOL on pumpfun
[09:41:23] 💰 PAPER COPY SELL Toke…pump (50% of position) +0.0210 SOL (+84.6%): alpha sold 50% on pumpfun
[09:41:33] 💰 PAPER COPY SELL Toke…pump +0.0204 SOL (+81.1%): alpha sold 100% on pumpfun
[09:42:10] 🔴 PAPER COPY SELL Toke…pump -0.0159 SOL (-31.8%): stop loss -31%
```

## What is a Solana copy trading bot?

A copy trading bot watches one or more "smart money" wallets and repeats their trades from your own wallet. The hard part is seeing the trade fast and understanding it on every DEX. This bot subscribes to NoLimitNodes streams that already normalize PumpFun trades and DEX swaps into one shape (`trader`, `input_mint`, `output_mint`, amounts). It filters for your target wallets and sends a matching swap, usually within the same few slots.

## Features

| Feature | What it does | Flag |
|---|---|---|
| 👛 Multi-wallet copy | Follow any number of wallets, with labels | `--wallets ADDR:alpha,ADDR2:beta`, `--wallets-file` |
| 🔀 Multi-DEX | PumpFun, PumpSwap, Raydium AMM v4/CPMM/CLMM, Orca Whirlpool, Meteora DLMM/DAMM/pools | `--venues pumpfun,pumpswap,...` |
| ⚖️ Position sizing | Fixed SOL per copy, or a ratio of the target's size with min and max | `--buy-sol 0.05`, `--copy-ratio 0.1 --min-buy 0.02 --max-buy 0.3` |
| ✂️ Proportional sells | Sell the same share of your position that the target sold | on by default, `--no-copy-sells` |
| 🧹 Noise filters | Skip dust buys, stale signals, blocked mints and non-SOL routes | `--min-target-sol 0.1 --max-signal-age 20 --block-mints ...` |
| 🛡 Your own exits | Stop loss, take profit and max hold that apply even if the target holds | `--stop-loss 50 --take-profit 200 --max-hold 3600` |
| 💸 Risk caps | Max open positions and a daily realized-loss cap | `--max-positions 5 --max-daily-loss 1` |
| 📊 Per-wallet PnL | Summary of realized PnL by copied wallet, so you know who is worth following | printed on exit |
| 🧪 Paper trading and replay | Simulated fills at the last traded price, or offline replay | default, `--replay` |
| 📣 Telegram alerts | Posts every copy and its PnL | `--telegram` |

## Quick start

```bash
git clone https://github.com/no-limit-nodes/solana-copy-trading-bot.git
cd solana-copy-trading-bot
python -m pip install -r requirements.txt

export NLN_API_KEY=sk_live_...     # https://app.nolimitnodes.com/register
python run_copytrader.py --wallets <WALLET_TO_COPY>:alpha
```

### Try it offline (no API key)

```bash
python run_copytrader.py --replay examples/sample-events.jsonl --wallets-file examples/wallets.txt --stop-loss 30
```

### Copy trades for real

Use a **dedicated hot wallet** that holds only what you are willing to lose.

```bash
export NLN_RPC_URL=https://...                   # your NoLimitNodes RPC URL
export SOLANA_KEYPAIR_PATH=~/.config/solana/copy.json
# optional: export JUPITER_API_KEY=...           # https://developers.jup.ag/portal

python run_copytrader.py --live --wallets-file wallets.txt \
  --copy-ratio 0.05 --min-buy 0.02 --max-buy 0.2 --max-positions 3 --max-daily-loss 0.5
```

`wallets.txt` takes one wallet per line, with an optional label:

```text
# ADDRESS [label]
<WALLET_ADDRESS_1> alpha
<WALLET_ADDRESS_2> beta
```

## Configuration

Every flag can also be set as an environment variable (see [`.env.example`](.env.example)).

| Flag | Env | Default | Meaning |
|---|---|---|---|
| `--wallets` | `COPY_WALLETS` | none | Comma-separated `ADDRESS` or `ADDRESS:label` |
| `--wallets-file` | `COPY_WALLETS_FILE` | none | File of `ADDRESS [label]` lines |
| `--venues` | `VENUES` | all except `meteora_pools` | Streams to watch |
| `--buy-sol` | `BUY_SOL` | `0.05` | Fixed SOL per copied buy |
| `--copy-ratio` | `COPY_RATIO` | `0` | If set, copy size is the target's SOL times this ratio |
| `--min-buy` / `--max-buy` | `MIN_BUY_SOL` / `MAX_BUY_SOL` | `0.01` / `0.5` | Clamp for ratio sizing (max also caps fixed size) |
| `--min-target-sol` | `MIN_TARGET_SOL` | `0.1` | Ignore target buys smaller than this |
| `--no-copy-sells` | | off | Copy buys only, and exit with your own rules |
| `--allow-add` | | off | Copy repeat buys into a token you already hold |
| `--max-positions` | `MAX_POSITIONS` | `5` | Open plus pending positions |
| `--stop-loss` | `STOP_LOSS_PCT` | `50` | Percent loss that triggers a sell (0 = off) |
| `--take-profit` | `TAKE_PROFIT_PCT` | `0` | Percent gain that triggers a sell (0 = off) |
| `--max-hold` | `MAX_HOLD_S` | `0` | Seconds before a forced exit (0 = off) |
| `--max-signal-age` | `MAX_SIGNAL_AGE_S` | `20` | Skip target trades older than this (PumpFun events carry a timestamp) |
| `--block-mints` | `BLOCKED_MINTS` | none | Tokens never to buy |
| `--max-daily-loss` | `MAX_DAILY_LOSS_SOL` | `1.0` | Stop opening positions after this realized loss (UTC day) |
| `--live` | | off | Sign and send real transactions |
| `--slippage-bps` | `SLIPPAGE_BPS` | `1000` | Max slippage for live swaps |
| `--priority` | `PRIORITY` | `high` | Priority fee level: `medium`, `high`, `veryHigh` or `0`–`10000` |
| `--telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | off | Telegram alerts |

## How it works

```text
NoLimitNodes gRPC (stream-1.nln.clr3.org:443)
  ├─ prod.rpc.solana.pumpfun.trade
  ├─ prod.mat.solana.program.swaps.pumpswap
  ├─ prod.mat.solana.program.swaps.raydium_amm_v4 / raydium_cpmm / raydium_clmm
  ├─ prod.mat.solana.program.swaps.orca_whirlpool
  └─ prod.mat.solana.program.swaps.meteora_dlmm / meteora_damm
        │
        ▼  normalize → Trade(wallet, mint, side, SOL, tokens)
  CopyEngine: target filter, sizing, proportional sells, exits, risk caps
        │
        ▼
  Executor ─┬─ PaperTrader (default)
            └─ LiveTrader: Jupiter /build → sign locally → NLN RPC
```

- **Buys:** a target swaps SOL or wSOL for a token. The bot buys that token with the configured size.
- **Sells:** a target swaps a token back to SOL. The bot sells `tokens_sold / tokens_the_target_held` of its own position. If the bot never saw the target buy, it sells the whole position.
- **Prices:** every trade on a held token, by any wallet, updates the price used for stop loss and take profit.
- **Duplicates:** the same transaction can appear on more than one topic, so each signature is copied at most once.

See [`docs/guide.md`](docs/guide.md) for sizing math, latency and deployment.

## Repository layout

- [`run_copytrader.py`](run_copytrader.py): command-line entry point, wallet parsing, executor and replay.
- [`copytrader.py`](copytrader.py): trade normalization and copy engine. No network I/O.
- [`trader.py`](trader.py): `PaperTrader` and `LiveTrader` (Jupiter Swap API V2 plus NoLimitNodes RPC). Reusable.
- [`nln_stream.py`](nln_stream.py): NoLimitNodes gRPC client with reconnect and multi-topic fan-in.
- [`notify.py`](notify.py): console, JSON and Telegram output.
- [`examples/`](examples): recorded-shape events and a sample wallet list for offline replay.
- [`tests/`](tests): normalization, copy logic, transaction assembly and replay tests (`python -m unittest discover -s tests`).

## FAQ

### How do I copy trade a Solana wallet?

Install the bot, set `NLN_API_KEY`, and run `python run_copytrader.py --wallets <ADDRESS>`. It paper-trades by default, so you can watch what it would have done. When you are happy with the results, add `NLN_RPC_URL`, a keypair and `--live`.

### What is the fastest Solana copy trading setup?

Latency comes from three places: seeing the target's trade, building your swap, and landing it. This bot sees trades from a gRPC push stream instead of polling `getSignaturesForAddress`. It builds the swap in a single Jupiter `/build` request, and sends it through a dedicated NoLimitNodes RPC with a priority fee. Running it on a host in the same region as your NLN endpoint removes most of the remaining network delay. NLN plans include VPS credit.

### Will I get the same price as the wallet I copy?

No. You always buy after the target, usually a few slots later. On thin PumpFun curves the target's own buy moves the price, so a copy of a large buy can fill noticeably higher. Use `--copy-ratio` to keep your size small relative to the target's, `--slippage-bps` to cap the damage, and `--min-target-sol` to skip trades too small to matter.

### How do I find wallets worth copying?

Look for wallets with consistent realized profit over many trades, not one big win. The NoLimitNodes `prod.rpc.solana.pumpfun.trade` stream includes every PumpFun buy and sell with its wallet, so you can score wallets yourself, or watch candidates first with the [PumpFun alert bot](https://github.com/no-limit-nodes/pumpfun-telegram-alert-bot)'s `--watch` option. Run several candidates in paper mode, then use the per-wallet PnL summary to keep the ones that work.

### Does it copy sells as well as buys?

Yes, proportionally. If a target sells 30% of the tokens the bot saw them buy, the bot sells 30% of its position. When the target sells 95% or more, the bot exits fully. Use `--no-copy-sells` to manage exits yourself with `--stop-loss`, `--take-profit` and `--max-hold`.

### Can it copy trades on Jupiter or other aggregators?

Usually, yes. When a target swaps through Jupiter, the actual swap still runs on an underlying pool such as Raydium, Orca, Meteora or PumpSwap, and that pool's swap event appears on the matching NLN topic. Only SOL-quoted trades are copied. Token-to-token and USDC routes are ignored.

### Is it safe?

Run it with a separate wallet that holds a small balance, keep `--max-positions` and `--max-daily-loss` low, and start in paper mode. Copied wallets can dump on their copiers, and memecoins can go to zero in minutes.

## Related NoLimitNodes resources

- [PumpFun sniper bot](https://github.com/no-limit-nodes/pumpfun-sniper-bot): buy new pump.fun launches with momentum filters and automatic exits
- [PumpFun Telegram alert bot](https://github.com/no-limit-nodes/pumpfun-telegram-alert-bot): launch, graduation, whale and wallet-watch alerts
- [Solana priority fee API](https://github.com/no-limit-nodes/solana-priority-fee-api): compute-unit price from landed PumpFun transactions
- [Solana streaming reference](https://github.com/no-limit-nodes/solana-programs-data-streaming-reference): every NLN topic with clients in 7 languages
- [Every Solana program decoded](https://github.com/no-limit-nodes/every-solana-program-decoded): 1,074 events across 37 programs

## Disclaimer

This software is provided for education and research. It is not financial advice. Copy trading memecoins can lose all of your funds. You are responsible for your keys, your trades and complying with the laws where you live.

## License

Released under the [MIT License](LICENSE).
