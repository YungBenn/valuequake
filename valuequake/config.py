import os
import tomllib
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path


@dataclass(frozen=True)
class OutcomeParams:
    label_version: str
    beta_window: tuple[int, int]
    beta_shrink: float
    unshrunk_beta: frozenset[str]
    ewma_lambda: float
    sigma_floor_ratio: float
    label_thresholds: tuple[float, float]


@dataclass(frozen=True)
class ReleaseParams:
    fred: dict[str, int]  # Scheduled Release name -> FRED release id
    fred_time_et: time
    fomc: list[date]
    fomc_time_et: time


@dataclass(frozen=True)
class BaseRateParams:
    placebo_n: int
    history_years: int
    seed: int


@dataclass(frozen=True)
class Config:
    targets: list[str]
    sensitivity: list[str]
    outcome: OutcomeParams
    releases: ReleaseParams
    macro_series: list[str]
    base_rates: BaseRateParams

    @property
    def tickers(self) -> list[str]:
        return self.targets + self.sensitivity


def load_config(path: str | Path | None = None) -> Config:
    path = path or os.environ.get("VALUEQUAKE_CONFIG", "config.toml")
    raw = tomllib.loads(Path(path).read_text())
    o, r = raw["outcome"], raw["scheduled_releases"]
    return Config(
        targets=raw["tickers"]["targets"],
        sensitivity=raw["tickers"]["sensitivity"],
        outcome=OutcomeParams(
            label_version=o["label_version"],
            beta_window=tuple(o["beta_window"]),
            beta_shrink=o["beta_shrink"],
            unshrunk_beta=frozenset(o["unshrunk_beta"]),
            ewma_lambda=o["ewma_lambda"],
            sigma_floor_ratio=o["sigma_floor_ratio"],
            label_thresholds=tuple(o["label_thresholds"]),
        ),
        releases=ReleaseParams(
            fred=r["fred"],
            fred_time_et=time.fromisoformat(r["fred_time_et"]),
            fomc=[date.fromisoformat(d) for d in r["fomc"]],
            fomc_time_et=time.fromisoformat(r["fomc_time_et"]),
        ),
        macro_series=raw["macro"]["series"],
        base_rates=BaseRateParams(**raw["base_rates"]),
    )
