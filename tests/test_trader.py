import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader import (WSOL_MINT, LiveTrader, NlnRpc, PaperTrader, TradeError,  # noqa: E402
                    assemble_transaction)

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    HAVE_SOLDERS = True
except ImportError:  # pragma: no cover
    HAVE_SOLDERS = False

MINT = "9FYVRJAFa1DwtjZao34Cyfd9GYHwgDzRiQTC864ppump"
BLOCKHASH = "4sGjMW1sUnHzSxGspuhpqLDx6wiyjNtZAMdL4VZHirAn"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
JUP_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
CB_PROGRAM = "ComputeBudget111111111111111111111111111111"


def fake_build(owner: str) -> dict:
    ix = lambda prog, data, accs: {"programId": prog, "data": base64.b64encode(data).decode(),  # noqa: E731
                                   "accounts": accs}
    me = {"pubkey": owner, "isSigner": True, "isWritable": True}
    return {
        "inAmount": "50000000", "outAmount": "1234567",
        "computeBudgetInstructions": [ix(CB_PROGRAM, bytes([2, 64, 13, 3, 0]), [])],
        "setupInstructions": [ix(TOKEN_PROGRAM, b"\x01", [me])],
        "swapInstruction": ix(JUP_PROGRAM, b"swapdata", [me, {"pubkey": MINT, "isSigner": False,
                                                               "isWritable": False}]),
        "cleanupInstruction": None,
        "otherInstructions": [],
        "tipInstruction": None,
        "addressesByLookupTableAddress": {},
    }


class PaperTraderTest(unittest.TestCase):
    def test_round_trip_charges_cost_both_sides(self):
        prices = {MINT: 0.03}
        t = PaperTrader(prices.get, cost_bps=200)
        buy = t.buy(MINT, 1_000_000_000)
        self.assertEqual(buy.token_raw, int(1e9 * 0.98 / 0.03))
        prices[MINT] = 0.06  # price doubles
        sell = t.sell(MINT)
        self.assertAlmostEqual(sell.sol_lamports / 1e9, 2 * 0.98 * 0.98, places=3)
        with self.assertRaises(TradeError):
            t.sell(MINT)

    def test_no_price(self):
        with self.assertRaises(TradeError):
            PaperTrader(lambda m: None).buy(MINT, 1000)


@unittest.skipUnless(HAVE_SOLDERS, "solders not installed")
class LiveTraderTest(unittest.TestCase):
    def setUp(self):
        self.kp = Keypair()
        self.owner = str(self.kp.pubkey())
        self.sent = []
        self.jup_calls = []
        self.balance = 0

        def rpc_http(url, body=None, headers=None, timeout=10.0):
            m, p = body["method"], body["params"]
            if m == "getLatestBlockhash":
                return {"result": {"value": {"blockhash": BLOCKHASH}}}
            if m == "sendTransaction":
                self.sent.append(p[0])
                tx = VersionedTransaction.from_bytes(base64.b64decode(p[0]))
                return {"result": str(tx.signatures[0])}
            if m == "getSignatureStatuses":
                return {"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}}
            if m == "getTokenAccountsByOwner":
                amt = {"amount": str(self.balance)}
                return {"result": {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": amt}}}}}]}}
            if m == "getTransaction":
                return {"result": {"transaction": {"message": {"accountKeys": [self.owner]}},
                                   "meta": {"preBalances": [2_000_000_000], "postBalances": [1_947_000_000]}}}
            raise AssertionError(m)

        def jup_http(url, body=None, headers=None, timeout=10.0):
            self.jup_calls.append((url, headers))
            return fake_build(self.owner)

        self.trader = LiveTrader(self.kp, NlnRpc("https://rpc.example", http=rpc_http),
                                 jupiter_api_key="test-key", http=jup_http)

    def test_assemble_signs_with_wallet(self):
        tx = assemble_transaction(fake_build(self.owner), self.kp, BLOCKHASH)
        self.assertEqual(str(tx.message.account_keys[0]), self.owner)
        self.assertEqual(str(tx.message.recent_blockhash), BLOCKHASH)
        self.assertEqual(len(tx.message.instructions), 3)

    def test_buy_uses_jupiter_build_and_nln_rpc(self):
        self.balance = 1_500_000
        fill = self.trader.buy(MINT, 50_000_000)
        url, headers = self.jup_calls[0]
        self.assertIn("api.jup.ag/swap/v2/build", url)
        self.assertIn(f"inputMint={WSOL_MINT}", url)
        self.assertIn(f"outputMint={MINT}", url)
        self.assertIn(f"taker={self.owner}", url)
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(fill.token_raw, 1_500_000)
        self.assertEqual(fill.sol_lamports, 53_000_000)  # from the confirmed balance change
        self.assertFalse(fill.paper)

    def test_sell_without_balance_fails_before_sending(self):
        self.balance = 0
        with self.assertRaises(TradeError):
            self.trader.sell(MINT)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
