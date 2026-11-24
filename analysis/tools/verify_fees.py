"""Check polydata.fees.taker_fee against the fees actually charged on-chain.

Samples recorded taker trades (stratified by fee rate, side and price), fetches each transaction
receipt from a public Polygon RPC, decodes the exchange's OrderFilled event for the taker order and
compares its `fee` with the formula. Fills routed through third-party apps ("builder" code set) may
carry the app's own fee on top; they are reported separately.

    cd analysis && uv run python tools/verify_fees.py [--per-bucket 8] [--rpc URL]

Re-run whenever Polymarket announces fee changes. Last run 2026-11-24: 236/236 fills without a
builder code matched exactly (to the 0.00001 rounding); 18 of 32 builder fills had app fees on top.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from Crypto.Hash import keccak

import polydata
from polydata.fees import taker_fee

EXCHANGE = "0xe111180000d2663c0091e4f400237545b87b996b"  # Polymarket CTF exchange (Polygon)
_k = keccak.new(digest_bits=256)
_k.update(b"OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)")
ORDER_FILLED = "0x" + _k.hexdigest()


def receipt(rpc: str, tx: str) -> dict | None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx]}).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(rpc, body, {"content-type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=20).read())["result"]
        except Exception:
            time.sleep(1 + attempt)
    return None


def taker_fills(rc: dict) -> list[dict]:
    """OrderFilled events of taker orders (their counterparty is the exchange itself)."""
    out = []
    for log in rc.get("logs", []):
        if log["address"].lower() != EXCHANGE or log["topics"][0] != ORDER_FILLED:
            continue
        if not log["topics"][3].lower().endswith(EXCHANGE[2:]):
            continue
        w = [int(log["data"][2 + i * 64: 2 + (i + 1) * 64], 16) for i in range(7)]
        side, maker_amt, taker_amt, fee, builder = w[0], w[2], w[3], w[4], w[5]
        shares, usdc = (taker_amt, maker_amt) if side == 0 else (maker_amt, taker_amt)
        out.append({"side": "BUY" if side == 0 else "SELL", "shares": shares / 1e6, "usdc": usdc / 1e6,
                    "fee": fee / 1e6, "builder": builder != 0})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-bucket", type=int, default=8, help="trades per (fee rate, side, price decile)")
    ap.add_argument("--rpc", default="https://polygon-bor-rpc.publicnode.com")
    a = ap.parse_args()

    con = polydata.connect()
    rows = con.sql(f"""
        SELECT t.tx_hash, m.fee_rate, m.fee_exponent
        FROM pm_trade t JOIN markets m USING (slug)
        WHERE t.tx_hash IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY m.fee_rate, t.side, floor(t.price * 10) ORDER BY hash(t.tx_hash)) <= {a.per_bucket}
    """).fetchall()
    print(f"{len(rows)} sampled trades; fetching receipts...")
    with ThreadPoolExecutor(4) as pool:
        receipts = list(pool.map(lambda r: receipt(a.rpc, r[0]), rows))

    stats: Counter = Counter()
    worst = []
    for (tx, rate, exponent), rc in zip(rows, receipts):
        if not rc:
            stats["receipt failed"] += 1
            continue
        for f in taker_fills(rc):
            p = f["usdc"] / f["shares"]
            err = f["fee"] - taker_fee(f["shares"], p, rate, exponent)
            ok = abs(err) <= 1.5e-5  # one unit of the 5-decimal rounding
            stats[("builder" if f["builder"] else "no builder") + (" / matches" if ok else " / differs")] += 1
            if not ok and not f["builder"]:
                worst.append((abs(err), tx, f, rate))
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    if worst:
        print("\nFORMULA MISMATCH on fills without builder fees (formula or rates changed?):")
        for e, tx, f, rate in sorted(worst, reverse=True)[:10]:
            print(f"  {tx} {f} rate={rate} err={e:.5f}")
    else:
        print("\nOK: every fill without a builder fee matches taker_fee().")


if __name__ == "__main__":
    main()
