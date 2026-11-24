"""Run configuration: what to replay, with which strategy, delays, fill model and capital.

`RunConfig.from_dict` takes the dict of a YAML config file; `to_dict` gives it back (round-trips).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

ALL_COINS = ["btc", "eth", "sol", "xrp", "bnb", "doge", "hype", "zec"]
DAY_KINDS = ("train", "validation", "sealed")


@dataclass
class RunConfig:
    strategy: str  # a registry name (kebab-case)
    params: dict = field(default_factory=dict)
    coins: list[str] = field(default_factory=lambda: list(ALL_COINS))
    timeframes: list[str] = field(default_factory=lambda: ["5m", "15m", "1h"])
    days: list[str] | None = None  # ISO dates; None = every available day of `day_kinds`
    day_kinds: list[str] = field(default_factory=lambda: ["train", "validation"])
    order_latency_ms: int = 200  # orders and cancels
    latency_jitter_ms: int = 0  # + U(0, jitter), seeded
    fill_report_latency_ms: int = 126  # exchange event -> strategy's on_order
    cancel_model: str = "behind"
    impact_reset_ms: int = 5000
    capital: float = 5000.0
    capital_mode: str = "per_coin"  # or "global": one replay, one shared balance
    redeem_delay_s: int = 60
    max_gap_ms_pm: int = 5000
    max_gap_ms_bn: int = 1000
    bn_step_ms: int = 10
    seed: int = 0
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.capital_mode not in ("per_coin", "global"):
            raise ValueError(f"capital_mode must be per_coin or global, not {self.capital_mode!r}")
        if self.cancel_model not in ("behind", "proportional"):
            raise ValueError(f"cancel_model must be behind or proportional, not {self.cancel_model!r}")
        if bad := [k for k in self.day_kinds if k not in DAY_KINDS]:
            raise ValueError(f"unknown day kinds {bad}")
        for name in ("order_latency_ms", "latency_jitter_ms", "fill_report_latency_ms", "redeem_delay_s"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.bn_step_ms <= 0 or 3_600_000 % self.bn_step_ms:
            raise ValueError("bn_step_ms must divide one hour")
        if not self.coins:
            raise ValueError("no coins")

    @classmethod
    def from_dict(cls, d: dict) -> RunConfig:
        known = {f.name for f in fields(cls)}
        if unknown := sorted(set(d) - known):
            raise ValueError(f"unknown config keys {unknown}")
        d = dict(d)
        if d.get("days") is not None:
            d["days"] = [str(x) for x in d["days"]]  # YAML parses bare dates as date objects
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)
