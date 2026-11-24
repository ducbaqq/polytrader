from __future__ import annotations

import pytest

from polydata.bt.exchange import Fill, Order
from polydata.bt.portfolio import Portfolio
from polydata.bt.types import MarketInfo
from polydata.fees import taker_fee

T0 = 1_795_519_800_000
SLUG = "btc-updown-5m-1795519800"


def info(slug: str = SLUG, coin: str = "btc") -> MarketInfo:
    return MarketInfo(slug=slug, coin=coin, timeframe="5m", series=f"{coin}-up-or-down-5m", condition_id="0xabc",
                      window_start_ms=T0, window_end_ms=T0 + 300_000, price_to_beat=60000.0, fee_rate=0.05,
                      fee_exponent=1.0, tick_px=10, min_order_size=5.0, outcome="Up",
                      resolution_recv_ms=T0 + 360_000)


_ids = iter(range(1, 10**6))


def order(token="Up", side="BUY", px=400, size=10.0, kind="limit", post_only=True, slug=SLUG) -> Order:
    return Order(id=next(_ids), slug=slug, token=token, side=side, px=px, size=size, kind=kind,
                 post_only=post_only, placed_ms=T0, arrival_ms=T0 + 200)


def fill(o: Order, px: int | None = None, size: float | None = None, fee: float = 0.0) -> Fill:
    return Fill(o.id, T0 + 1000, o.px if px is None else px, o.size if size is None else size, fee, 0.0,
                "maker" if fee == 0 else "taker", T0 + 1000)


def pf(budget: float = 100.0) -> Portfolio:
    p = Portfolio({"btc": budget}, redeem_delay_ms=60_000)
    p.add_market(info())
    return p


def test_portfolio_locks_and_merge():
    p = pf()
    a, b = order("Up", "BUY", 400, 10), order("Down", "BUY", 550, 10)
    assert p.reserve_buy(a) and p.free_cash("btc") == pytest.approx(96.0)
    assert p.reserve_buy(b) and p.free_cash("btc") == pytest.approx(90.5)
    p.apply_fill(a, fill(a))
    p.release(a)
    assert p.positions[SLUG] == (10.0, 0.0)
    p.apply_fill(b, fill(b))
    p.release(b)
    assert p.positions[SLUG] == (10.0, 10.0)
    assert p.merge(SLUG) == pytest.approx(10.0)
    assert p.positions[SLUG] == (0.0, 0.0)
    assert p.cash["btc"] == pytest.approx(100.5)  # 100 - 9.5 + 10
    assert p.free_cash("btc") == pytest.approx(100.0)  # the budget doesn't grow with profits
    assert p.pnl[SLUG] == pytest.approx(0.5)
    assert p.equity({}) == pytest.approx(100.5)


def test_partial_merge_leaves_the_excess():
    p = pf()
    a, b = order("Up", "BUY", 400, 10), order("Down", "BUY", 550, 6)
    for o in (a, b):
        assert p.reserve_buy(o)
        p.apply_fill(o, fill(o))
        p.release(o)
    assert p.merge(SLUG) == pytest.approx(6.0)
    assert p.positions[SLUG] == (4.0, 0.0)
    assert p.equity({SLUG: 0.5}) == pytest.approx(100 - 4.0 - 3.3 + 6 + 4 * 0.5)


def test_reject_insufficient_cash():
    p = pf(10.0)
    assert not p.reserve_buy(order("Up", "BUY", 400, 30))  # 12 > 10
    assert p.free_cash("btc") == pytest.approx(10.0)
    assert p.reserve_buy(order("Up", "BUY", 400, 25))  # exactly 10
    assert p.free_cash("btc") == pytest.approx(0.0)
    assert not p.reserve_buy(order("Up", "BUY", 10, 5))


def test_take_locks_a_fee_bound_and_settles_the_actual_fee():
    p = pf(5.0)
    # 10 @ limit 0.50 needs 5.00 + fee bound: refused with exactly $5.
    assert not p.reserve_buy(order("Up", "BUY", 500, 10, kind="take", post_only=False))
    p = pf(100.0)
    o = order("Up", "BUY", 900, 10, kind="take", post_only=False)
    assert p.reserve_buy(o)
    # Fees peak at p = 0.5, so a limit of 0.90 must lock the bound at 0.5, not at 0.9.
    bound = taker_fee(10, 0.5, 0.05)
    assert p.free_cash("btc") == pytest.approx(100 - 9.0 - bound)
    fee = taker_fee(10, 0.6, 0.05)
    p.apply_fill(o, fill(o, px=600, fee=fee))
    p.release(o)
    assert p.free_cash("btc") == pytest.approx(100 - 6.0 - fee)
    assert p.pnl[SLUG] == pytest.approx(-6.0 - fee)


def test_sell_unheld_rejected():
    p = pf()
    assert not p.reserve_sell(order("Up", "SELL", 600, 5))
    b = order("Up", "BUY", 400, 5)
    assert p.reserve_buy(b)
    p.apply_fill(b, fill(b))
    p.release(b)
    s = order("Up", "SELL", 600, 5)
    assert p.reserve_sell(s)
    assert not p.reserve_sell(order("Up", "SELL", 600, 1))  # the 5 shares are locked by s
    assert not p.reserve_sell(order("Down", "SELL", 600, 1))  # holding Up is not holding Down
    p.apply_fill(s, fill(s))
    p.release(s)
    assert p.positions[SLUG] == (0.0, 0.0)
    assert p.cash["btc"] == pytest.approx(101.0) and p.free_cash("btc") == pytest.approx(100.0)


def test_release_unlocks_cash_and_shares():
    p = pf()
    o = order("Up", "BUY", 400, 10)
    assert p.reserve_buy(o)
    p.apply_fill(o, fill(o, size=4))
    assert p.free_cash("btc") == pytest.approx(100 - 1.6 - 2.4)
    p.release(o)
    assert p.free_cash("btc") == pytest.approx(98.4)
    s = order("Up", "SELL", 600, 4)
    assert p.reserve_sell(s)
    p.release(s)
    assert p.reserve_sell(order("Up", "SELL", 600, 4))


def test_settlement_after_redeem_delay():
    p = pf()
    o = order("Up", "BUY", 400, 10)
    assert p.reserve_buy(o)
    p.apply_fill(o, fill(o))
    p.release(o)
    credit = p.settle(SLUG, "Up", T0 + 360_000)
    assert credit == T0 + 420_000
    assert p.positions[SLUG] == (0.0, 0.0)
    assert p.pnl[SLUG] == pytest.approx(6.0)
    assert p.equity({}) == pytest.approx(106.0)  # pending redemption counts in equity
    p.redeem(T0 + 419_999)
    assert p.free_cash("btc") == pytest.approx(96.0)  # still locked until the redeem delay passes
    p.redeem(T0 + 420_000)
    assert p.cash["btc"] == pytest.approx(106.0) and p.equity({}) == pytest.approx(106.0)
    assert p.free_cash("btc") == pytest.approx(100.0)  # capped at the budget


def test_budget_fixed_profits_do_not_grow_it_losses_shrink_it():
    p = pf(10.0)
    o = order("Up", "BUY", 400, 25)
    assert p.reserve_buy(o)
    p.apply_fill(o, fill(o))
    p.release(o)
    p.settle(SLUG, "Up", T0)
    p.redeem(T0 + 10**9)
    assert p.cash["btc"] == pytest.approx(25.0) and p.free_cash("btc") == pytest.approx(10.0)
    assert not p.reserve_buy(order("Up", "BUY", 500, 21))  # 10.50 > budget, though cash is 25
    q = pf(10.0)
    o = order("Down", "BUY", 400, 10)
    assert q.reserve_buy(o)
    q.apply_fill(o, fill(o))
    q.release(o)
    q.settle(SLUG, "Up", T0)
    q.redeem(T0 + 10**9)
    assert q.free_cash("btc") == pytest.approx(6.0)  # losses reduce it


def test_losing_side_settles_at_zero():
    p = pf()
    o = order("Down", "BUY", 300, 10)
    assert p.reserve_buy(o)
    p.apply_fill(o, fill(o))
    p.release(o)
    p.settle(SLUG, "Up", T0 + 360_000)
    p.redeem(T0 + 10**9)
    assert p.free_cash("btc") == pytest.approx(97.0)
    assert p.pnl[SLUG] == pytest.approx(-3.0)


def test_settle_without_outcome_refused():
    p = pf()
    with pytest.raises(ValueError):
        p.settle(SLUG, None, T0)


def test_global_budget_shared_across_coins():
    p = Portfolio({"*": 10.0})
    p.add_market(info())
    p.add_market(info("eth-updown-5m-1795519800", "eth"))
    assert p.reserve_buy(order("Up", "BUY", 500, 10))
    assert not p.reserve_buy(order("Up", "BUY", 500, 11, slug="eth-updown-5m-1795519800"))
    assert p.free_cash("*") == pytest.approx(5.0)


def test_budget_not_grown_by_profit_after_win_then_buy():
    p = pf(10.0)
    o = order("Up", "BUY", 400, 25)
    assert p.reserve_buy(o)
    p.apply_fill(o, fill(o))
    p.release(o)
    p.settle(SLUG, "Up", T0)
    p.redeem(T0 + 10**9)
    assert p.cash["btc"] == pytest.approx(25.0) and p.free_cash("btc") == pytest.approx(10.0)
    p.add_market(info("btc-updown-5m-1795520100"))
    b = order("Up", "BUY", 900, 10, slug="btc-updown-5m-1795520100")
    assert p.reserve_buy(b)
    p.apply_fill(b, fill(b))
    p.release(b)
    assert p.cash["btc"] == pytest.approx(16.0)
    assert p.free_cash("btc") == pytest.approx(1.0)  # 9 deployed out of the 10 budget
    assert not p.reserve_buy(order("Up", "BUY", 900, 10, slug="btc-updown-5m-1795520100"))
    # a loss of 4 elsewhere: cash 6, free 6
    q = pf(10.0)
    o = order("Down", "BUY", 400, 10)
    assert q.reserve_buy(o)
    q.apply_fill(o, fill(o))
    q.release(o)
    assert q.free_cash("btc") == pytest.approx(6.0)  # 4 deployed
    q.settle(SLUG, "Up", T0)
    q.redeem(T0 + 10**9)
    assert q.cash["btc"] == pytest.approx(6.0) and q.free_cash("btc") == pytest.approx(6.0)
