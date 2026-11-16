#!/usr/bin/env python3
"""Run the Solana copy trading bot on NoLimitNodes decoded trade and swap streams.

Paper trading is the default. Nothing is signed or sent unless you pass --live.

Examples:
    # paper-copy two wallets with 0.05 SOL per buy
    python run_copytrader.py --wallets WALLET1:alpha,WALLET2:beta

    # size each copy at 10% of the target's buy, between 0.02 and 0.3 SOL
    python run_copytrader.py --wallets-file wallets.txt --copy-ratio 0.1 --min-buy 0.02 --max-buy 0.3

    # offline replay (no API key needed)
    python run_copytrader.py --replay examples/sample-events.jsonl --wallets-file examples/wallets.txt

    # real trades with a dedicated hot wallet
    python run_copytrader.py --live --wallets WALLET1 --buy-sol 0.02 --max-positions 3
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import queue
import signal
import sys
import threading
from typing import Dict

from copytrader import DEFAULT_VENUES, CopyConfig, CopyEngine, Order, topics_for
from notify import Notifier
from trader import LAMPORTS_PER_SOL, LiveTrader, NlnRpc, PaperTrader, TradeError, load_keypair

log = logging.getLogger("run_copytrader")


def csv(value: str):
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_wallets(items, path: str = "") -> Dict[str, str]:
    """Parse ``ADDRESS`` or ``ADDRESS:label`` entries, plus an optional file of ``ADDRESS [label]`` lines."""
    out: Dict[str, str] = {}
    lines = list(items)
    if path:
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    parts = line.split(None, 1)
                    lines.append(parts[0] + (":" + parts[1] if len(parts) > 1 else ""))
    for item in lines:
        addr, _, label = item.partition(":")
        addr = addr.strip()
        if not (32 <= len(addr) <= 44):
            raise SystemExit(f"not a Solana address: {addr!r}")
        out[addr] = label.strip()
    return out


def parse_args(argv=None):
    env = os.environ.get
    p = argparse.ArgumentParser(description="Solana copy trading bot on NoLimitNodes gRPC streams.")
    p.add_argument("--wallets", type=csv, default=csv(env("COPY_WALLETS", "")),
                   help="comma-separated wallets to copy, optionally ADDRESS:label")
    p.add_argument("--wallets-file", default=env("COPY_WALLETS_FILE", ""),
                   help="file with one 'ADDRESS [label]' per line")
    p.add_argument("--venues", type=csv, default=csv(env("VENUES", ",".join(DEFAULT_VENUES))),
                   help="pumpfun, pumpswap, raydium_amm_v4, raydium_cpmm, raydium_clmm, orca_whirlpool, "
                        "meteora_dlmm, meteora_damm, meteora_pools")
    p.add_argument("--buy-sol", type=float, default=float(env("BUY_SOL", 0.05)), help="fixed SOL per copy")
    p.add_argument("--copy-ratio", type=float, default=float(env("COPY_RATIO", 0)),
                   help="size copies at this fraction of the target's SOL (0 = use --buy-sol)")
    p.add_argument("--min-buy", type=float, default=float(env("MIN_BUY_SOL", 0.01)))
    p.add_argument("--max-buy", type=float, default=float(env("MAX_BUY_SOL", 0.5)))
    p.add_argument("--min-target-sol", type=float, default=float(env("MIN_TARGET_SOL", 0.1)),
                   help="ignore target buys smaller than this many SOL")
    p.add_argument("--no-copy-sells", action="store_true", help="only copy buys; exit with your own rules")
    p.add_argument("--allow-add", action="store_true", help="copy repeat buys into a token already held")
    p.add_argument("--max-positions", type=int, default=int(env("MAX_POSITIONS", 5)))
    p.add_argument("--take-profit", type=float, default=float(env("TAKE_PROFIT_PCT", 0)))
    p.add_argument("--stop-loss", type=float, default=float(env("STOP_LOSS_PCT", 50)))
    p.add_argument("--max-hold", type=int, default=int(env("MAX_HOLD_S", 0)))
    p.add_argument("--max-signal-age", type=float, default=float(env("MAX_SIGNAL_AGE_S", 20)),
                   help="skip target trades older than this many seconds (0 = off)")
    p.add_argument("--block-mints", type=csv, default=csv(env("BLOCKED_MINTS", "")))
    p.add_argument("--max-daily-loss", type=float, default=float(env("MAX_DAILY_LOSS_SOL", 1.0)))
    p.add_argument("--live", action="store_true", help="sign and send real transactions")
    p.add_argument("--slippage-bps", type=int, default=int(env("SLIPPAGE_BPS", 1000)))
    p.add_argument("--priority", default=env("PRIORITY", "high"))
    p.add_argument("--paper-cost-bps", type=int, default=int(env("PAPER_COST_BPS", 150)))
    p.add_argument("--telegram", action="store_true", help="send trade alerts to Telegram")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--replay", metavar="FILE")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def build_config(a) -> CopyConfig:
    targets = parse_wallets(a.wallets, a.wallets_file)
    if not targets:
        raise SystemExit("no wallets to copy: pass --wallets or --wallets-file")
    return CopyConfig(
        targets=targets, buy_sol=a.buy_sol, copy_ratio=a.copy_ratio, min_buy_sol=a.min_buy,
        max_buy_sol=a.max_buy, min_target_sol=a.min_target_sol, copy_sells=not a.no_copy_sells,
        allow_add=a.allow_add, max_positions=a.max_positions, take_profit_pct=a.take_profit,
        stop_loss_pct=a.stop_loss, max_hold_s=a.max_hold, max_signal_age_s=a.max_signal_age,
        blocked_mints=set(a.block_mints), max_daily_loss_sol=a.max_daily_loss)


class Executor:
    """Runs orders off the stream thread. Sells are executed before buys."""

    def __init__(self, engine: CopyEngine, trader, notifier: Notifier, inline: bool = False):
        self.engine, self.trader, self.notifier, self.inline = engine, trader, notifier, inline
        self.q: "queue.PriorityQueue" = queue.PriorityQueue()
        self._seq = itertools.count()
        if not inline:
            threading.Thread(target=self._run, daemon=True, name="executor").start()

    def submit(self, orders) -> None:
        for order in orders:
            if self.inline:
                self.execute(order)
            else:
                self.q.put((0 if order.side == "sell" else 1, next(self._seq), order))

    def _run(self) -> None:
        while True:
            self.execute(self.q.get()[2])

    def execute(self, order: Order) -> None:
        tag = "PAPER" if self.trader.paper else "LIVE"
        short = f"{order.mint[:4]}…{order.mint[-4:]}"
        try:
            fill = (self.trader.buy(order.mint, order.sol_lamports) if order.side == "buy"
                    else self.trader.sell(order.mint, order.fraction))
        except TradeError as exc:
            self.engine.on_fill(order, None, str(exc))
            self.notifier.send("error", f"⚠️ {tag} {order.side} {short} failed: {exc}", {"mint": order.mint})
            return
        record = self.engine.on_fill(order, fill)
        if order.side == "buy":
            self.notifier.send("buy", f"🟢 {tag} COPY BUY {short} {fill.sol:.4f} SOL: {order.reason}\n"
                               f"https://dexscreener.com/solana/{order.mint}",
                               {"mint": order.mint, "sol": fill.sol, "wallet": order.wallet,
                                "signature": fill.signature})
        elif record:
            icon = "💰" if record["pnl_sol"] > 0 else "🔴"
            part = "" if record["closed"] else f" ({record['share_sold'] * 100:.0f}% of position)"
            self.notifier.send("sell", f"{icon} {tag} COPY SELL {short}{part} {record['pnl_sol']:+.4f} SOL "
                               f"({record['pnl_pct']:+.1f}%): {order.reason}", record)


def replay(path: str, engine: CopyEngine, executor: Executor, clock_box: list) -> None:
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rec = json.loads(line)
            ts = rec["event"].get("timestamp") or rec["event"].get("block_time")
            if ts:
                clock_box[0] = float(ts)
            executor.submit(engine.handle(rec["topic"], rec["event"]))
            executor.submit(engine.tick())


def main(argv=None) -> int:
    a = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = build_config(a)
    topics = topics_for(a.venues)
    notifier = Notifier(json_lines=a.json, quiet=a.quiet,
                        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "") if a.telegram else "",
                        telegram_chat=os.environ.get("TELEGRAM_CHAT_ID", "") if a.telegram else "")

    if a.replay:
        clock_box = [0.0]
        engine = CopyEngine(cfg, clock=lambda: clock_box[0])
        replay(a.replay, engine, Executor(engine, PaperTrader(engine.price, a.paper_cost_bps), notifier,
                                          inline=True), clock_box)
        print(json.dumps(engine.summary(), indent=2))
        return 0

    import nln_stream

    api_key = nln_stream.require_api_key()
    engine = CopyEngine(cfg)
    if a.live:
        keypair = load_keypair()
        rpc = NlnRpc()
        bal = rpc.balance(str(keypair.pubkey())) / LAMPORTS_PER_SOL
        log.warning("LIVE mode: wallet %s holds %.4f SOL", keypair.pubkey(), bal)
        if bal < min(cfg.buy_sol, cfg.min_buy_sol) + 0.01:
            raise SystemExit(f"wallet balance {bal:.4f} SOL is too low to copy trades")
        trader = LiveTrader(keypair, rpc, slippage_bps=a.slippage_bps, priority=a.priority)
    else:
        log.info("PAPER mode: no transactions will be sent (use --live for real trades)")
        trader = PaperTrader(engine.price, a.paper_cost_bps)
    executor = Executor(engine, trader, notifier)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def ticker():
        while not stop.wait(1.0):
            executor.submit(engine.tick())

    threading.Thread(target=ticker, daemon=True, name="ticker").start()
    log.info("copying %d wallet(s) on %s", len(cfg.targets), ", ".join(a.venues))
    try:
        for topic, event in nln_stream.merge(topics, api_key, stop=stop):
            executor.submit(engine.handle(topic, event))
    except nln_stream.StreamAuthError as exc:
        log.error("NoLimitNodes rejected the API key: %s", exc)
        return 2
    finally:
        print(json.dumps(engine.summary(), indent=2), file=sys.stderr)
        if engine.positions:
            log.warning("%d position(s) still open", len(engine.positions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
