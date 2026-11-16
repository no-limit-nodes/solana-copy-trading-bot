"""Order execution for Solana trading bots: paper fills and live swaps.

Two traders share one interface (``buy`` and ``sell``):

* ``PaperTrader`` fills orders at the last price the strategy saw, minus a
  configurable cost for fees and slippage. It never touches the network, so
  it is the default and is safe to run with no wallet.
* ``LiveTrader`` builds a swap with the Jupiter Swap API V2 ``/build``
  endpoint, compiles and signs a v0 transaction locally with ``solders``, and
  sends it through your NoLimitNodes Solana RPC endpoint. Jupiter never sees
  your private key, and you control where the transaction is sent.

Environment variables (live mode only):
    NLN_RPC_URL           NoLimitNodes Solana RPC URL (https://app.nolimitnodes.com)
    JUPITER_API_KEY       optional; a free key from https://developers.jup.ag/portal raises rate limits
    SOLANA_PRIVATE_KEY    base58 secret key, or
    SOLANA_KEYPAIR_PATH   path to a solana-keygen JSON keypair file
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

log = logging.getLogger("trader")

LAMPORTS_PER_SOL = 1_000_000_000
WSOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_BUILD_URL = "https://api.jup.ag/swap/v2/build"


class TradeError(RuntimeError):
    """An order could not be built, sent or confirmed."""


@dataclass
class Fill:
    side: str  # buy | sell
    mint: str
    sol_lamports: int  # SOL spent on a buy, SOL received on a sell
    token_raw: int  # tokens received on a buy, tokens sold on a sell
    signature: str = ""
    paper: bool = True

    @property
    def sol(self) -> float:
        return self.sol_lamports / LAMPORTS_PER_SOL


# -- Paper trading ------------------------------------------------------------------


class PaperTrader:
    """Simulated fills at the strategy's last seen price.

    ``price_fn(mint)`` returns lamports per raw token unit, or ``None`` when the
    price is unknown. ``cost_bps`` is charged on both sides to approximate
    protocol fees, creator fees and slippage (default 2%).
    """

    paper = True

    def __init__(self, price_fn: Callable[[str], Optional[float]], cost_bps: int = 200):
        self.price_fn = price_fn
        self.cost = cost_bps / 10_000
        self.balances: Dict[str, int] = {}

    def buy(self, mint: str, sol_lamports: int) -> Fill:
        price = self.price_fn(mint)
        if not price or price <= 0:
            raise TradeError(f"no price for {mint}")
        tokens = int(sol_lamports * (1 - self.cost) / price)
        self.balances[mint] = self.balances.get(mint, 0) + tokens
        return Fill("buy", mint, sol_lamports, tokens, f"paper-buy-{mint[:8]}-{time.time_ns()}")

    def sell(self, mint: str, fraction: float = 1.0) -> Fill:
        held = self.balances.get(mint, 0)
        if held <= 0:
            raise TradeError(f"no {mint} balance to sell")
        price = self.price_fn(mint)
        if not price or price <= 0:
            raise TradeError(f"no price for {mint}")
        amount = held if fraction >= 0.999 else int(held * max(fraction, 0.0))
        if amount <= 0:
            raise TradeError("sell amount rounds to zero")
        self.balances[mint] = held - amount
        sol = int(amount * price * (1 - self.cost))
        return Fill("sell", mint, sol, amount, f"paper-sell-{mint[:8]}-{time.time_ns()}")


# -- Live trading -------------------------------------------------------------------


def _http_json(url: str, body: Optional[dict] = None, headers: Optional[dict] = None,
               timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "nln-solana-bot/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise TradeError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise TradeError(f"request to {urllib.parse.urlsplit(url).netloc} failed: {exc}") from exc


class NlnRpc:
    """Minimal JSON-RPC client for a NoLimitNodes Solana RPC endpoint."""

    def __init__(self, url: Optional[str] = None, http=_http_json):
        self.url = url or os.environ.get("NLN_RPC_URL", "")
        if not self.url:
            raise SystemExit("NLN_RPC_URL is not set. Copy your RPC URL from https://app.nolimitnodes.com")
        self._http = http

    def call(self, method: str, params: Optional[list] = None):
        out = self._http(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
        if "error" in out:
            raise TradeError(f"{method}: {out['error']}")
        return out.get("result")

    def latest_blockhash(self) -> str:
        return self.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]

    def balance(self, owner: str) -> int:
        return int(self.call("getBalance", [owner, {"commitment": "confirmed"}])["value"])

    def token_balance(self, owner: str, mint: str) -> int:
        """Raw token balance of ``owner`` for ``mint`` across SPL Token and Token-2022 accounts."""
        res = self.call("getTokenAccountsByOwner",
                        [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        total = 0
        for acc in res.get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total

    def send(self, tx_b64: str) -> str:
        return self.call("sendTransaction", [tx_b64, {"encoding": "base64", "skipPreflight": True,
                                                      "maxRetries": 3}])

    def confirm(self, signature: str, timeout_s: float = 45.0, poll_s: float = 0.5) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            res = self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
            status = (res or {}).get("value", [None])[0]
            if status:
                if status.get("err"):
                    raise TradeError(f"transaction {signature} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            time.sleep(poll_s)
        raise TradeError(f"transaction {signature} not confirmed after {timeout_s:.0f}s")

    def sol_delta(self, signature: str, owner: str) -> Optional[int]:
        """Net lamports change for ``owner`` in a confirmed transaction (fees included)."""
        try:
            tx = self.call("getTransaction", [signature, {"encoding": "json", "commitment": "confirmed",
                                                          "maxSupportedTransactionVersion": 0}])
        except TradeError:
            return None
        if not tx:
            return None
        keys = tx["transaction"]["message"]["accountKeys"]
        if owner not in keys:
            return None
        i = keys.index(owner)
        return int(tx["meta"]["postBalances"][i]) - int(tx["meta"]["preBalances"][i])


def load_keypair():
    """Load the signing keypair from SOLANA_PRIVATE_KEY or SOLANA_KEYPAIR_PATH."""
    from solders.keypair import Keypair

    secret = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    if secret:
        return Keypair.from_base58_string(secret)
    path = os.environ.get("SOLANA_KEYPAIR_PATH", "").strip()
    if path:
        with open(os.path.expanduser(path)) as fh:
            return Keypair.from_bytes(bytes(json.load(fh)))
    raise SystemExit("Live mode needs SOLANA_PRIVATE_KEY or SOLANA_KEYPAIR_PATH. Use a dedicated hot wallet.")


def _instruction(ix: dict):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    metas = [AccountMeta(Pubkey.from_string(a["pubkey"]), bool(a["isSigner"]), bool(a["isWritable"]))
             for a in ix["accounts"]]
    return Instruction(Pubkey.from_string(ix["programId"]), base64.b64decode(ix["data"]), metas)


def assemble_transaction(build: dict, keypair, blockhash: str):
    """Compile a Jupiter ``/build`` response into a signed v0 ``VersionedTransaction``."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    raw: List[dict] = []
    raw += build.get("computeBudgetInstructions") or []
    raw += build.get("setupInstructions") or []
    raw.append(build["swapInstruction"])
    if build.get("cleanupInstruction"):
        raw.append(build["cleanupInstruction"])
    raw += build.get("otherInstructions") or []
    if build.get("tipInstruction"):
        raw.append(build["tipInstruction"])
    tables = [AddressLookupTableAccount(Pubkey.from_string(k), [Pubkey.from_string(a) for a in v])
              for k, v in (build.get("addressesByLookupTableAddress") or {}).items()]
    msg = MessageV0.try_compile(keypair.pubkey(), [_instruction(i) for i in raw], tables,
                                Hash.from_string(blockhash))
    return VersionedTransaction(msg, [keypair])


class LiveTrader:
    """Real swaps: Jupiter ``/build`` for routing, NoLimitNodes RPC for sending."""

    paper = False

    def __init__(self, keypair, rpc: NlnRpc, jupiter_api_key: Optional[str] = None,
                 slippage_bps: int = 1500, priority: str = "high", http=_http_json):
        self.keypair = keypair
        self.owner = str(keypair.pubkey())
        self.rpc = rpc
        self.api_key = jupiter_api_key if jupiter_api_key is not None else os.environ.get("JUPITER_API_KEY", "")
        self.slippage_bps = slippage_bps
        self.priority = priority
        self._http = http

    def _build(self, input_mint: str, output_mint: str, amount: int) -> dict:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "taker": self.owner,
            "slippageBps": str(self.slippage_bps),
            "computeUnitPricePercentile": self.priority,
        }
        out = self._http(f"{JUPITER_BUILD_URL}?{urllib.parse.urlencode(params)}",
                         headers={"x-api-key": self.api_key} if self.api_key else {})
        if not out or "swapInstruction" not in out:
            raise TradeError(f"Jupiter returned no route for {input_mint} -> {output_mint}: {out}")
        return out

    def _execute(self, build: dict) -> str:
        tx = assemble_transaction(build, self.keypair, self.rpc.latest_blockhash())
        sig = self.rpc.send(base64.b64encode(bytes(tx)).decode())
        log.info("sent %s", sig)
        self.rpc.confirm(sig)
        return sig

    def buy(self, mint: str, sol_lamports: int) -> Fill:
        build = self._build(WSOL_MINT, mint, sol_lamports)
        sig = self._execute(build)
        tokens = self.rpc.token_balance(self.owner, mint) or int(build.get("outAmount", 0))
        delta = self.rpc.sol_delta(sig, self.owner)
        spent = -delta if delta is not None and delta < 0 else sol_lamports
        return Fill("buy", mint, spent, tokens, sig, paper=False)

    def sell(self, mint: str, fraction: float = 1.0) -> Fill:
        held = self.rpc.token_balance(self.owner, mint)
        if held <= 0:
            raise TradeError(f"wallet holds no {mint}")
        amount = held if fraction >= 0.999 else int(held * max(fraction, 0.0))
        if amount <= 0:
            raise TradeError("sell amount rounds to zero")
        build = self._build(mint, WSOL_MINT, amount)
        sig = self._execute(build)
        delta = self.rpc.sol_delta(sig, self.owner)
        received = delta if delta is not None and delta > 0 else int(build.get("outAmount", 0))
        return Fill("sell", mint, received, amount, sig, paper=False)
