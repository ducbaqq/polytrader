"""Cash, locks, positions and settlement (spec: "Capital and positions").

- Budgets are per coin (`"*"` = one global budget) and fixed: profits don't grow them, losses shrink
  them. Free cash = min(cash, budget - open_cost) - cash locked by open buys, where open_cost is the
  net cash spent on markets not settled yet (sum of max(0, -net cash flow) per market). So money
  already deployed counts against the budget even when realised profits sit in `cash`.
- **Buying** a token at q locks q x size; a buy that can take liquidity (a `take`, or a limit that is
  not post-only) also locks a fee bound. Fees peak at p = 0.5, so the bound is the fee at
  min(limit, 0.5): fills happen at or below the limit.
- **Selling** locks shares of that token already held. Selling shares we don't hold is refused (the
  equivalent is buying the other token).
- **Merge:** equal unlocked Up + Down in a market are turned into $1 each, at once.
- **Settlement:** winning shares pay $1 each, credited to free cash `redeem_delay_ms` after the
  resolution was received; until then the amount is pending (it counts in equity, not in free cash).

The engine applies fills when the strategy learns of them (report time), so what `free_cash` and
`positions` say never runs ahead of what the strategy could know. Prices are int px (1/1000).
"""

from __future__ import annotations

from .exchange import Fill, Order
from .types import PX_SCALE, MarketInfo

EPS = 1e-9


class Portfolio:
    def __init__(self, budgets: dict[str, float], redeem_delay_ms: int = 60_000):
        self.budget: dict[str, float] = {k: float(v) for k, v in budgets.items()}
        self.cash: dict[str, float] = dict(self.budget)  # includes locked cash
        self.locked: dict[str, float] = {k: 0.0 for k in budgets}
        self.redeem_delay_ms = int(redeem_delay_ms)
        self.positions: dict[str, tuple[float, float]] = {}  # slug -> (Up, Down) shares
        self.pnl: dict[str, float] = {}  # slug -> realised cash flow (+ settlement payout)
        self._global = "*" in budgets
        self._info: dict[str, MarketInfo] = {}
        self._lock_cash: dict[int, float] = {}  # order id -> cash still locked
        self._lock_sh: dict[int, float] = {}  # order id -> shares still locked
        self._sh_locked: dict[tuple[str, str], float] = {}  # (slug, token) -> shares locked by sells
        self._pending: list[tuple[int, str, float]] = []  # (credit_ms, budget key, amount)
        self._net: dict[str, float] = {}  # slug -> net cash flow since its last settlement

    # ------------------------------------------------------------------------------------- setup
    def add_market(self, info: MarketInfo) -> None:
        self._info[info.slug] = info
        self.positions.setdefault(info.slug, (0.0, 0.0))
        self.pnl.setdefault(info.slug, 0.0)
        if self.key(info.slug) not in self.cash:
            raise KeyError(f"no budget for {self.key(info.slug)!r}")

    def key(self, slug: str) -> str:
        return "*" if self._global else self._info[slug].coin

    # ------------------------------------------------------------------------------------- cash
    def free_cash(self, key: str) -> float:
        return min(self.cash[key], self.budget[key] - self.open_cost(key)) - self.locked[key]

    def open_cost(self, key: str) -> float:
        """Net cash spent on this budget's markets that are not settled yet."""
        return sum(max(0.0, -v) for s, v in self._net.items() if self.key(s) == key)

    def pending(self, key: str | None = None) -> float:
        return sum(a for _, k, a in self._pending if key is None or k == key)

    def reserve_buy(self, order: Order) -> bool:
        info = self._info[order.slug]
        amount = order.px / PX_SCALE * order.size
        if order.kind == "take" or not order.post_only:
            from ..fees import taker_fee
            amount += taker_fee(order.size, min(order.px, PX_SCALE // 2) / PX_SCALE, info.fee_rate, info.fee_exponent)
        k = self.key(order.slug)
        if amount > self.free_cash(k) + EPS:
            return False
        self.locked[k] += amount
        self._lock_cash[order.id] = amount
        return True

    def reserve_sell(self, order: Order) -> bool:
        held = self.positions[order.slug][0 if order.token == "Up" else 1]
        lk = (order.slug, order.token)
        if order.size > held - self._sh_locked.get(lk, 0.0) + EPS:
            return False
        self._sh_locked[lk] = self._sh_locked.get(lk, 0.0) + order.size
        self._lock_sh[order.id] = order.size
        return True

    def release(self, order: Order) -> None:
        """Unlock whatever the order still holds (it is done: filled, cancelled, expired, rejected)."""
        c = self._lock_cash.pop(order.id, 0.0)
        if c:
            self.locked[self.key(order.slug)] -= c
        s = self._lock_sh.pop(order.id, 0.0)
        if s:
            lk = (order.slug, order.token)
            self._sh_locked[lk] = max(0.0, self._sh_locked.get(lk, 0.0) - s)

    # ------------------------------------------------------------------------------------- fills
    def _add(self, slug: str, token: str, q: float) -> None:
        up, dn = self.positions[slug]
        self.positions[slug] = (up + q, dn) if token == "Up" else (up, dn + q)

    def apply_fill(self, order: Order, fill: Fill) -> None:
        k, slug, q = self.key(order.slug), order.slug, fill.size
        notional = fill.px / PX_SCALE * q
        if order.side == "BUY":
            spend = notional + fill.fee
            self.cash[k] -= spend
            lk = self._lock_cash.get(order.id, 0.0)
            # the lock covers limit x size (+ fee bound); this fill used its own part of it
            used = min(lk, order.px / PX_SCALE * q + fill.fee)
            self._lock_cash[order.id] = lk - used
            self.locked[k] -= used
            self._add(slug, order.token, q)
            self.pnl[slug] -= spend
            self._net[slug] = self._net.get(slug, 0.0) - spend
        else:
            got = notional - fill.fee
            self.cash[k] += got
            ls = self._lock_sh.get(order.id, 0.0)
            used = min(ls, q)
            self._lock_sh[order.id] = ls - used
            lk = (slug, order.token)
            self._sh_locked[lk] = max(0.0, self._sh_locked.get(lk, 0.0) - used)
            self._add(slug, order.token, -q)
            self.pnl[slug] += got
            self._net[slug] = self._net.get(slug, 0.0) + got

    def merge(self, slug: str) -> float:
        """Turn equal unlocked Up + Down into $1 each. Returns the number of pairs merged."""
        up, dn = self.positions[slug]
        n = min(up - self._sh_locked.get((slug, "Up"), 0.0), dn - self._sh_locked.get((slug, "Down"), 0.0))
        if n <= EPS:
            return 0.0
        self.positions[slug] = (up - n, dn - n)
        self.cash[self.key(slug)] += n
        self.pnl[slug] += n
        self._net[slug] = self._net.get(slug, 0.0) + n
        return n

    # ------------------------------------------------------------------------------------- settlement
    def settle(self, slug: str, outcome: str | None, t_ms: int) -> int:
        """Winning shares pay $1, credited at `t_ms` (resolution receive time) + redeem delay.
        Returns the credit time."""
        if outcome not in ("Up", "Down"):
            raise ValueError(f"{slug}: cannot settle without an outcome ({outcome!r}); void it instead")
        up, dn = self.positions[slug]
        payout = up if outcome == "Up" else dn
        self.positions[slug] = (0.0, 0.0)
        self.pnl[slug] += payout
        self._net.pop(slug, None)
        credit = int(t_ms) + self.redeem_delay_ms
        if payout > 0:
            self._pending.append((credit, self.key(slug), payout))
        return credit

    def redeem(self, now_ms: int) -> float:
        """Credit every pending payout due at or before `now_ms`. Returns the amount credited."""
        due = [p for p in self._pending if p[0] <= now_ms]
        if not due:
            return 0.0
        self._pending = [p for p in self._pending if p[0] > now_ms]
        for _, k, a in due:
            self.cash[k] += a
        return sum(a for _, _, a in due)

    # ------------------------------------------------------------------------------------- marks
    def has_exposure(self) -> bool:
        return bool(self._pending) or any(abs(u) > EPS or abs(d) > EPS for u, d in self.positions.values())

    def held_slugs(self) -> list[str]:
        return [s for s, (u, d) in self.positions.items() if abs(u) > EPS or abs(d) > EPS]

    def equity(self, marks: dict[str, float], key: str | None = None) -> float:
        """Cash (free + locked) + pending redemptions + shares at marks (Up mid; Down = 1 - mid).
        A held market without a mark counts at 0."""
        cash = sum(v for k, v in self.cash.items() if key is None or k == key)
        val = 0.0
        for slug in self.held_slugs():
            if key is not None and self.key(slug) != key:
                continue
            m = marks.get(slug)
            if m is None or m != m:
                continue
            up, dn = self.positions[slug]
            val += up * m + dn * (1.0 - m)
        return cash + self.pending(key) + val
