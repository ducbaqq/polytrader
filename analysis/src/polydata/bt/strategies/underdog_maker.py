"""`underdog-maker` (family `fair-value`; the edge is market calibration, not a model): rest a bid on the underdog
with 2-5 minutes left in a 5m window.

The calibration study (docs/research/calibration-study.md, section 1) found 5m prices too extreme with 2-5 min
left (slope 0.86 [0.75, 0.98]): favourites are overpriced, underdogs underpriced. `endgame-maker` exploits the
opposite late underconfidence; this mirrors it on the early side. See docs/strategies/underdog-maker.md.

Rule. For each market of `timeframes` with time left in [`stop_tau_s`, `start_tau_s`]: the underdog is the token with
the lower mid (Up terms mirrored for Down). Quote only when its book is two-sided with spread <= `max_spread` and its
mid is in [`band_lo`, `band_hi`]. Then rest one post-only BUY of `size` shares at its best bid (join), hold any fill to
settlement. Cancelled when the conditions stop holding (mid out of the band, book widened, underdog flipped,
`stop_tau_s` reached), never chased. After any fill the market gets no new order. Inputs come from `ctx` only.
"""

from __future__ import annotations

from ..strategy import Ctx, OrderView, Strategy, register
from ..types import MarketInfo

DEPTH = 5


def pick_underdog(book, band_lo: float, band_hi: float, max_spread: float):
    """(token, bid) of the underdog when it qualifies, else None. `book` is an Up-terms BookView."""
    best = None
    for token in ("Up", "Down"):
        v = book.for_token(token)
        if v.mid is None or v.best_bid is None or v.spread is None:
            continue
        if best is None or v.mid < best[2]:
            best = (token, v.best_bid, v.mid, v.spread)
    if best is None:
        return None
    token, bid, mid, spread = best
    if spread > max_spread + 1e-9 or not band_lo - 1e-9 <= mid <= band_hi + 1e-9:
        return None
    return token, bid


@register
class UnderdogMaker(Strategy):
    name = "underdog-maker"
    family = "fair-value"
    default_params = {
        "timeframes": ["5m"],
        "start_tau_s": 300,
        "stop_tau_s": 120,
        "band_lo": 0.15,
        "band_hi": 0.35,
        "max_spread": 0.02,
        "size": 10.0,
        "check_ms": 1000,
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.mkts: dict[str, MarketInfo] = {}
        self.oid: dict[str, tuple[int, str]] = {}  # slug -> (order id, token) in flight or resting
        self.cancelling: set[str] = set()
        self.filled: dict[str, float] = {}

    def on_market_open(self, ctx: Ctx, market: MarketInfo) -> None:
        if market.timeframe not in self.params["timeframes"]:
            return
        self.mkts[market.slug] = market
        self.filled[market.slug] = 0.0
        ctx.set_timer(f"chk:{market.slug}", self.params["check_ms"])

    def on_book(self, ctx: Ctx, market: MarketInfo, book) -> None:
        if market.slug in self.mkts:
            self._decide(ctx, market)

    def on_timer(self, ctx: Ctx, name: str) -> None:
        m = self.mkts.get(name[4:]) if name.startswith("chk:") else None
        if m is None:
            return
        self._decide(ctx, m)
        if self._tau(ctx, m) > self.params["stop_tau_s"]:
            ctx.set_timer(name, self.params["check_ms"])

    def on_order(self, ctx: Ctx, order: OrderView) -> None:
        slug = order.slug
        if order.event == "fill" and order.last_fill is not None and slug in self.filled:
            self.filled[slug] += order.last_fill.size
        cur = self.oid.get(slug)
        if cur is not None and cur[0] == order.id and not order.is_open:
            del self.oid[slug]
            self.cancelling.discard(slug)

    def on_market_close(self, ctx: Ctx, market: MarketInfo) -> None:
        for d in (self.mkts, self.oid, self.filled):
            d.pop(market.slug, None)
        self.cancelling.discard(market.slug)

    # ------------------------------------------------------------------------------------------
    @staticmethod
    def _tau(ctx: Ctx, m: MarketInfo) -> float:
        return (m.window_end_ms - ctx.now) / 1000.0

    def _qualifies(self, ctx: Ctx, m: MarketInfo):
        """(token, bid) when a bid should be resting now, else None."""
        p = self.params
        if not p["stop_tau_s"] <= self._tau(ctx, m) <= p["start_tau_s"]:
            return None
        book = ctx.book(m.slug, DEPTH)
        if book is None:
            return None
        pick = pick_underdog(book, p["band_lo"], p["band_hi"], p["max_spread"])
        if pick is None:
            return None
        token, bid = pick
        return token, bid

    def _decide(self, ctx: Ctx, m: MarketInfo) -> None:
        slug = m.slug
        cur = self.oid.get(slug)
        q = self._qualifies(ctx, m)
        if cur is not None:
            if slug not in self.cancelling and (q is None or q[0] != cur[1]):
                self.cancelling.add(slug)
                ctx.cancel(cur[0])
            return
        if q is None or self.filled[slug] > 0:
            return
        token, bid = q
        if self.params["size"] < m.min_order_size:
            return
        self.oid[slug] = (ctx.place(slug, token, "BUY", bid, self.params["size"]), token)


__all__ = ["UnderdogMaker", "pick_underdog"]
