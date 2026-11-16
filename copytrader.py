"""Solana copy trading strategy: mirror target wallets' buys and sells.

The engine is pure Python with no network I/O. It reads NoLimitNodes decoded
PumpFun trades and normalized DEX swaps (PumpSwap, Raydium, Orca, Meteora),
detects trades by the wallets you follow, and returns ``Order`` objects. The
runner executes them with a trader from ``trader.py`` and reports fills back
with ``on_fill``.

Only SOL-quoted trades are copied: a buy spends SOL (or wSOL) on a token, and a
sell turns a token back into SOL. Token-to-token and stablecoin routes are
ignored.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from trader import LAMPORTS_PER_SOL, WSOL_MINT, Fill

log = logging.getLogger("copytrader")

TOPIC_PUMPFUN_TRADE = "prod.rpc.solana.pumpfun.trade"
SWAP_TOPICS = {
    "pumpswap": "prod.mat.solana.program.swaps.pumpswap",
    "raydium_amm_v4": "prod.mat.solana.program.swaps.raydium_amm_v4",
    "raydium_cpmm": "prod.mat.solana.program.swaps.raydium_cpmm",
    "raydium_clmm": "prod.mat.solana.program.swaps.raydium_clmm",
    "orca_whirlpool": "prod.mat.solana.program.swaps.orca_whirlpool",
    "meteora_dlmm": "prod.mat.solana.program.swaps.meteora_dlmm",
    "meteora_damm": "prod.mat.solana.program.swaps.meteora_damm",
    "meteora_pools": "prod.mat.solana.program.swaps.meteora_pools",
}
DEFAULT_VENUES = ("pumpfun", "pumpswap", "raydium_amm_v4", "raydium_cpmm", "raydium_clmm",
                  "orca_whirlpool", "meteora_dlmm", "meteora_damm")
STABLE_MINTS = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def topics_for(venues) -> List[str]:
    out = []
    for v in venues:
        if v == "pumpfun":
            out.append(TOPIC_PUMPFUN_TRADE)
        elif v in SWAP_TOPICS:
            out.append(SWAP_TOPICS[v])
        else:
            raise ValueError(f"unknown venue {v!r}; choose from pumpfun, {', '.join(SWAP_TOPICS)}")
    return out


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Trade:
    """A SOL-quoted trade normalized from any NLN topic."""
    wallet: str
    mint: str
    side: str  # buy | sell
    sol_lamports: int
    token_raw: int
    venue: str
    signature: str = ""
    timestamp: Optional[float] = None

    @property
    def price(self) -> Optional[float]:
        return self.sol_lamports / self.token_raw if self.sol_lamports > 0 and self.token_raw > 0 else None


def normalize(topic: str, ev: dict) -> Optional[Trade]:
    """Turn a PumpFun TradeEntry or a Swap event into a ``Trade``, or ``None`` if not SOL-quoted."""
    if topic == TOPIC_PUMPFUN_TRADE:
        mint = ev.get("mint", "")
        if not mint:
            return None
        if "is_buy" in ev:
            side = "buy" if ev["is_buy"] else "sell"
        else:
            name = str(ev.get("ix_name", "")).lower()
            side = "buy" if name.startswith("buy") else "sell" if name.startswith("sell") else ""
        if not side:
            return None
        ts = _int(ev.get("timestamp"))
        return Trade(ev.get("user", ""), mint, side, _int(ev.get("sol_amount")), _int(ev.get("token_amount")),
                     "pumpfun", ev.get("signature", ""), float(ts) if ts else None)

    inp, outp = ev.get("input_mint", ""), ev.get("output_mint", "")
    if not inp or not outp or inp == outp:
        return None
    venue = ev.get("dex") or topic.rsplit(".", 1)[-1]
    if inp == WSOL_MINT and outp not in STABLE_MINTS:
        return Trade(ev.get("trader", ""), outp, "buy", _int(ev.get("input_amount")),
                     _int(ev.get("output_amount")), venue, ev.get("signature", ""))
    if outp == WSOL_MINT and inp not in STABLE_MINTS:
        return Trade(ev.get("trader", ""), inp, "sell", _int(ev.get("output_amount")),
                     _int(ev.get("input_amount")), venue, ev.get("signature", ""))
    return None


@dataclass
class CopyConfig:
    targets: Dict[str, str] = field(default_factory=dict)  # wallet -> label
    buy_sol: float = 0.05  # fixed size per copied buy
    copy_ratio: float = 0.0  # if > 0, size = target SOL x ratio (clamped by min/max)
    min_buy_sol: float = 0.01
    max_buy_sol: float = 0.5
    min_target_sol: float = 0.1  # ignore target buys smaller than this
    copy_sells: bool = True
    allow_add: bool = False  # buy again when a target adds to a token we already hold
    max_positions: int = 5
    take_profit_pct: float = 0.0
    stop_loss_pct: float = 50.0
    max_hold_s: int = 0
    max_signal_age_s: float = 20.0  # skip trades older than this when a timestamp is present (0 = off)
    blocked_mints: Set[str] = field(default_factory=set)
    max_daily_loss_sol: float = 1.0


@dataclass
class Order:
    side: str
    mint: str
    sol_lamports: int = 0
    fraction: float = 1.0
    reason: str = ""
    wallet: str = ""
    label: str = ""


@dataclass
class Position:
    mint: str
    wallet: str
    sol_in: int
    tokens: int
    entry_price: float
    opened_at: float
    last_price: float
    closing: bool = False

    def pnl_pct(self) -> float:
        return (self.last_price / self.entry_price - 1) * 100 if self.entry_price else 0.0


class CopyEngine:
    def __init__(self, cfg: CopyConfig, clock=time.time, dedupe_size: int = 50_000):
        self.cfg = cfg
        self.clock = clock
        self.prices: Dict[str, float] = {}
        self.positions: Dict[str, Position] = {}
        self.pending: Set[str] = set()
        self.target_tokens: Dict[Tuple[str, str], int] = {}  # (wallet, mint) -> tokens seen bought
        self.seen: "OrderedDict[Tuple[str, str, str], None]" = OrderedDict()
        self.dedupe_size = dedupe_size
        self.realized_lamports = 0
        self.day = time.strftime("%Y-%m-%d", time.gmtime(clock()))
        self.closed: List[dict] = []
        self.signals = 0
        self.lock = threading.RLock()

    # -- helpers -----------------------------------------------------------------

    def price(self, mint: str) -> Optional[float]:
        return self.prices.get(mint)

    def label(self, wallet: str) -> str:
        return self.cfg.targets.get(wallet) or f"{wallet[:4]}…{wallet[-4:]}"

    def _halted(self) -> bool:
        today = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        if today != self.day:
            self.day, self.realized_lamports = today, 0
        cap = self.cfg.max_daily_loss_sol
        return cap > 0 and -self.realized_lamports >= cap * LAMPORTS_PER_SOL

    def size_for(self, target_lamports: int) -> int:
        c = self.cfg
        if c.copy_ratio > 0:
            sol = target_lamports / LAMPORTS_PER_SOL * c.copy_ratio
            sol = min(max(sol, c.min_buy_sol), c.max_buy_sol)
        else:
            sol = min(c.buy_sol, c.max_buy_sol)
        return int(sol * LAMPORTS_PER_SOL)

    def _duplicate(self, t: Trade) -> bool:
        key = (t.signature, t.mint, t.side)
        if not t.signature:
            return False
        if key in self.seen:
            return True
        self.seen[key] = None
        while len(self.seen) > self.dedupe_size:
            self.seen.popitem(last=False)
        return False

    def _exit_reason(self, pos: Position, now: float) -> str:
        c = self.cfg
        pnl = pos.pnl_pct()
        if c.take_profit_pct and pnl >= c.take_profit_pct:
            return f"take profit {pnl:+.0f}%"
        if c.stop_loss_pct and pnl <= -c.stop_loss_pct:
            return f"stop loss {pnl:+.0f}%"
        if c.max_hold_s and now - pos.opened_at >= c.max_hold_s:
            return f"max hold {c.max_hold_s}s ({pnl:+.0f}%)"
        return ""

    # -- events ------------------------------------------------------------------

    def handle(self, topic: str, event: dict) -> List[Order]:
        t = normalize(topic, event)
        if t is None:
            return []
        with self.lock:
            return self.on_trade(t)

    def on_trade(self, t: Trade) -> List[Order]:
        orders: List[Order] = []
        if t.price:
            self.prices[t.mint] = t.price

        if t.wallet in self.cfg.targets and not self._duplicate(t):
            orders += self._copy(t)

        pos = self.positions.get(t.mint)
        if pos and not pos.closing and t.price:
            pos.last_price = t.price
            reason = self._exit_reason(pos, self.clock())
            if reason:
                pos.closing = True
                orders.append(Order("sell", t.mint, fraction=1.0, reason=reason, wallet=pos.wallet,
                                    label=self.label(pos.wallet)))
        return orders

    def _copy(self, t: Trade) -> List[Order]:
        c = self.cfg
        who = self.label(t.wallet)
        key = (t.wallet, t.mint)
        if t.side == "buy":
            self.target_tokens[key] = self.target_tokens.get(key, 0) + t.token_raw
        else:
            tracked = self.target_tokens.get(key, 0)
            self.target_tokens[key] = max(tracked - t.token_raw, 0)

        if c.max_signal_age_s and t.timestamp and self.clock() - t.timestamp > c.max_signal_age_s:
            log.info("skip stale %s by %s (%.0fs old)", t.side, who, self.clock() - t.timestamp)
            return []
        self.signals += 1

        if t.side == "buy":
            if t.mint in c.blocked_mints:
                return []
            if t.sol_lamports < c.min_target_sol * LAMPORTS_PER_SOL:
                log.debug("skip small buy by %s: %.4f SOL", who, t.sol_lamports / LAMPORTS_PER_SOL)
                return []
            if t.mint in self.pending or (t.mint in self.positions and not c.allow_add):
                return []
            if t.mint not in self.positions and len(self.positions) + len(self.pending) >= c.max_positions:
                log.info("max positions open; not copying %s buy of %s", who, t.mint)
                return []
            if self._halted():
                log.warning("daily loss cap reached; not copying %s", who)
                return []
            self.pending.add(t.mint)
            return [Order("buy", t.mint, self.size_for(t.sol_lamports), wallet=t.wallet, label=who,
                          reason=f"{who} bought {t.sol_lamports / LAMPORTS_PER_SOL:.3f} SOL on {t.venue}")]

        pos = self.positions.get(t.mint)
        if not c.copy_sells or pos is None or pos.closing or pos.wallet != t.wallet:
            return []
        tracked = self.target_tokens.get(key, 0) + t.token_raw  # balance before this sell
        fraction = 1.0 if tracked <= 0 else min(t.token_raw / tracked, 1.0)
        if fraction >= 0.95:
            fraction = 1.0
            pos.closing = True
        return [Order("sell", t.mint, fraction=fraction, wallet=t.wallet, label=who,
                      reason=f"{who} sold {fraction * 100:.0f}% on {t.venue}")]

    def tick(self) -> List[Order]:
        with self.lock:
            out: List[Order] = []
            now = self.clock()
            for pos in list(self.positions.values()):
                if not pos.closing:
                    reason = self._exit_reason(pos, now)
                    if reason:
                        pos.closing = True
                        out.append(Order("sell", pos.mint, reason=reason, wallet=pos.wallet,
                                         label=self.label(pos.wallet)))
            return out

    # -- fills -------------------------------------------------------------------

    def on_fill(self, order: Order, fill: Optional[Fill], error: str = "") -> Optional[dict]:
        with self.lock:
            if order.side == "buy":
                self.pending.discard(order.mint)
                if fill is None or fill.token_raw <= 0:
                    return None
                pos = self.positions.get(order.mint)
                if pos:  # allow_add: average in
                    pos.sol_in += fill.sol_lamports
                    pos.tokens += fill.token_raw
                    pos.entry_price = pos.sol_in / pos.tokens
                else:
                    entry = fill.sol_lamports / fill.token_raw
                    self.positions[order.mint] = Position(order.mint, order.wallet, fill.sol_lamports,
                                                          fill.token_raw, entry, self.clock(),
                                                          self.prices.get(order.mint, entry))
                return None

            pos = self.positions.get(order.mint)
            if pos is None:
                return None
            if fill is None:
                if "balance to sell" in error or "holds no" in error:
                    self.positions.pop(order.mint, None)
                else:
                    pos.closing = False
                return None
            share = min(fill.token_raw / pos.tokens, 1.0) if pos.tokens else 1.0
            cost = int(pos.sol_in * share)
            pnl = fill.sol_lamports - cost
            self.realized_lamports += pnl
            pos.sol_in -= cost
            pos.tokens -= fill.token_raw
            full = share >= 0.999 or pos.tokens <= 0
            if full:
                self.positions.pop(order.mint, None)
            else:
                pos.closing = False
            record = {
                "mint": order.mint, "wallet": pos.wallet, "label": self.label(pos.wallet),
                "reason": order.reason, "share_sold": round(share, 4),
                "sol_in": cost / LAMPORTS_PER_SOL, "sol_out": fill.sol_lamports / LAMPORTS_PER_SOL,
                "pnl_sol": pnl / LAMPORTS_PER_SOL, "pnl_pct": pnl / cost * 100 if cost else 0.0,
                "closed": full, "signature": fill.signature,
            }
            self.closed.append(record)
            return record

    def summary(self) -> dict:
        with self.lock:
            per_wallet: Dict[str, dict] = {}
            for r in self.closed:
                w = per_wallet.setdefault(r["label"], {"sells": 0, "pnl_sol": 0.0})
                w["sells"] += 1
                w["pnl_sol"] = round(w["pnl_sol"] + r["pnl_sol"], 6)
            return {
                "signals": self.signals,
                "sells": len(self.closed),
                "realized_pnl_sol": round(sum(r["pnl_sol"] for r in self.closed), 6),
                "open_positions": len(self.positions),
                "by_wallet": per_wallet,
            }
