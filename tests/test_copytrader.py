import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from copytrader import (SWAP_TOPICS, TOPIC_PUMPFUN_TRADE, CopyConfig, CopyEngine,  # noqa: E402
                        normalize, topics_for)
from trader import WSOL_MINT, Fill  # noqa: E402

TARGET = "A1phaWa11et111111111111111111111111111111111"
OTHER = "0therTrader33333333333333333333333333333333"
MINT = "9FYVRJAFa1DwtjZao34Cyfd9GYHwgDzRiQTC864ppump"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T0 = 1_776_160_000


def pf(user, side, sol, tokens, sig="s1", ts=T0):
    return {"signature": sig, "mint": MINT, "user": user, "ix_name": side, "sol_amount": str(sol),
            "token_amount": str(tokens), "timestamp": str(ts)}


def swap(trader, in_mint, out_mint, in_amt, out_amt, sig="w1"):
    return {"signature": sig, "dex": "pumpswap", "trader": trader, "input_mint": in_mint,
            "output_mint": out_mint, "input_amount": str(in_amt), "output_amount": str(out_amt)}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class NormalizeTest(unittest.TestCase):
    def test_pumpfun_trade(self):
        t = normalize(TOPIC_PUMPFUN_TRADE, pf(TARGET, "buy", 10**9, 3 * 10**10))
        self.assertEqual((t.wallet, t.side, t.mint, t.venue), (TARGET, "buy", MINT, "pumpfun"))
        self.assertAlmostEqual(t.price, 1 / 30)

    def test_swap_directions(self):
        topic = SWAP_TOPICS["pumpswap"]
        buy = normalize(topic, swap(TARGET, WSOL_MINT, MINT, 10**9, 5 * 10**9))
        sell = normalize(topic, swap(TARGET, MINT, WSOL_MINT, 5 * 10**9, 10**9))
        self.assertEqual((buy.side, buy.mint, buy.sol_lamports), ("buy", MINT, 10**9))
        self.assertEqual((sell.side, sell.mint, sell.sol_lamports, sell.token_raw), ("sell", MINT, 10**9, 5 * 10**9))

    def test_non_sol_routes_ignored(self):
        topic = SWAP_TOPICS["raydium_clmm"]
        self.assertIsNone(normalize(topic, swap(TARGET, WSOL_MINT, USDC, 10**9, 150 * 10**6)))
        self.assertIsNone(normalize(topic, swap(TARGET, USDC, MINT, 10**6, 10**9)))
        self.assertIsNone(normalize(topic, swap(TARGET, "", MINT, 1, 1)))

    def test_topics_for(self):
        self.assertEqual(topics_for(["pumpfun", "pumpswap"]), [TOPIC_PUMPFUN_TRADE, SWAP_TOPICS["pumpswap"]])
        with self.assertRaises(ValueError):
            topics_for(["uniswap"])


class CopyTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cfg = CopyConfig(targets={TARGET: "alpha"}, buy_sol=0.1, min_target_sol=0.2, stop_loss_pct=40)
        self.e = CopyEngine(self.cfg, clock=self.clock)

    def buy_and_fill(self, tokens=3 * 10**10):
        orders = self.e.handle(TOPIC_PUMPFUN_TRADE, pf(TARGET, "buy", 10**9, tokens))
        self.assertEqual(len(orders), 1)
        self.e.on_fill(orders[0], Fill("buy", MINT, orders[0].sol_lamports, 3 * 10**9, "b"))
        return orders[0]

    def test_copies_target_buy_with_fixed_size(self):
        order = self.buy_and_fill()
        self.assertEqual(order.sol_lamports, 100_000_000)
        self.assertIn("alpha bought 1.000 SOL", order.reason)

    def test_ignores_other_wallets_small_buys_and_duplicates(self):
        self.assertEqual(self.e.handle(TOPIC_PUMPFUN_TRADE, pf(OTHER, "buy", 10**9, 10**10)), [])
        self.assertEqual(self.e.handle(TOPIC_PUMPFUN_TRADE, pf(TARGET, "buy", 10**8, 10**9, sig="s2")), [])
        ev = pf(TARGET, "buy", 10**9, 10**10, sig="s3")
        self.assertEqual(len(self.e.handle(TOPIC_PUMPFUN_TRADE, ev)), 1)
        self.assertEqual(self.e.handle(TOPIC_PUMPFUN_TRADE, ev), [])

    def test_stale_signal_skipped(self):
        self.clock.t = T0 + 60
        self.assertEqual(self.e.handle(TOPIC_PUMPFUN_TRADE, pf(TARGET, "buy", 10**9, 10**10)), [])

    def test_ratio_sizing_is_clamped(self):
        cfg = CopyConfig(targets={TARGET: ""}, copy_ratio=0.1, min_buy_sol=0.05, max_buy_sol=0.2)
        e = CopyEngine(cfg)
        self.assertEqual(e.size_for(10 * 10**9), 200_000_000)
        self.assertEqual(e.size_for(1 * 10**9), 100_000_000)
        self.assertEqual(e.size_for(1 * 10**8), 50_000_000)

    def test_proportional_sell_then_full_exit(self):
        self.buy_and_fill(tokens=3 * 10**10)
        sell1 = self.e.handle(TOPIC_PUMPFUN_TRADE, pf(TARGET, "sell", 6 * 10**8, 10**10, sig="x1"))
        self.assertAlmostEqual(sell1[0].fraction, 1 / 3)
        rec = self.e.on_fill(sell1[0], Fill("sell", MINT, 40_000_000, 10**9, "x1"))
        self.assertFalse(rec["closed"])
        self.assertAlmostEqual(rec["pnl_sol"], 0.04 - 0.1 / 3, places=6)
        sell2 = self.e.handle(TOPIC_PUMPFUN_TRADE, pf(TARGET, "sell", 10**9, 2 * 10**10, sig="x2"))
        self.assertEqual(sell2[0].fraction, 1.0)
        rec = self.e.on_fill(sell2[0], Fill("sell", MINT, 80_000_000, 2 * 10**9, "x2"))
        self.assertTrue(rec["closed"])
        self.assertNotIn(MINT, self.e.positions)

    def test_sell_without_seen_buy_sells_everything(self):
        self.buy_and_fill()
        self.e.target_tokens.clear()
        sell = self.e.handle(TOPIC_PUMPFUN_TRADE, pf(TARGET, "sell", 10**8, 10**9, sig="x3"))
        self.assertEqual(sell[0].fraction, 1.0)

    def test_no_copy_sells(self):
        self.cfg.copy_sells = False
        self.buy_and_fill()
        self.assertEqual(self.e.handle(TOPIC_PUMPFUN_TRADE, pf(TARGET, "sell", 10**8, 10**9, sig="x4")), [])

    def test_stop_loss_from_any_trader(self):
        self.buy_and_fill()  # entry 0.1 SOL / 3e9 tokens
        orders = self.e.handle(SWAP_TOPICS["pumpswap"], swap(OTHER, MINT, WSOL_MINT, 10**10, 10**8, sig="d"))
        self.assertEqual(orders[0].side, "sell")
        self.assertIn("stop loss", orders[0].reason)

    def test_max_positions_and_daily_cap(self):
        self.cfg.max_positions = 1
        self.buy_and_fill()
        other = dict(pf(TARGET, "buy", 10**9, 10**10, sig="m2"), mint="Other1111111111111111111111111111pump")
        self.assertEqual(self.e.handle(TOPIC_PUMPFUN_TRADE, other), [])
        self.cfg.max_positions = 5
        self.e.realized_lamports = -2 * 10**9
        other["signature"] = "m3"
        self.assertEqual(self.e.handle(TOPIC_PUMPFUN_TRADE, other), [])


class ReplayTest(unittest.TestCase):
    def test_sample_replay(self):
        import run_copytrader

        root = os.path.join(os.path.dirname(__file__), "..", "examples")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = run_copytrader.main(["--replay", os.path.join(root, "sample-events.jsonl"),
                                      "--wallets-file", os.path.join(root, "wallets.txt"),
                                      "--stop-loss", "30", "--quiet"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn('"sells": 3', out)
        self.assertIn('"alpha"', out)

    def test_parse_wallets(self):
        from run_copytrader import parse_wallets

        self.assertEqual(parse_wallets([f"{TARGET}:alpha", OTHER]), {TARGET: "alpha", OTHER: ""})
        with self.assertRaises(SystemExit):
            parse_wallets(["nope"])


if __name__ == "__main__":
    unittest.main()
