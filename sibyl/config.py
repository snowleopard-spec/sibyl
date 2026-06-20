from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class SecConfig:
    user_agent: str
    rate_limit_per_sec: int


@dataclass(frozen=True)
class UnicornConfig:
    base_url: str
    universe_path: str
    expected_contract_version: str
    token: str | None


@dataclass(frozen=True)
class UniverseConfig:
    form_types: list[str]
    include_amendments: bool
    history_start: str


@dataclass(frozen=True)
class Paths:
    data_root: Path
    # Stack-aware paths (canonical for the research tool).
    sp500_raw: Path
    sp500_clean: Path
    sp500_record: Path
    sp500_snapshots: Path
    queried_raw: Path
    queried_clean: Path
    queried_record: Path
    # Shared infra
    logs: Path
    snapshots: Path
    universe_json: Path
    db: Path
    company_tickers: Path
    lm_dictionary: Path
    prices: Path
    exports: Path
    # Deprecated single-stack aliases. Kept temporarily so unmigrated callers
    # (existing download/parse/sections/score/diff and their tests) keep working
    # while the stack-aware refactor lands module-by-module. Aliased to sp500_*.
    # Remove in the cleanup pass once no callers remain.
    raw: Path
    clean: Path


VALID_STACKS = ("sp500", "queried")


def stack_raw(cfg: "Config", stack: str) -> Path:
    if stack == "sp500":
        return cfg.paths.sp500_raw
    if stack == "queried":
        return cfg.paths.queried_raw
    raise ValueError(f"unknown stack {stack!r}; expected one of {VALID_STACKS}")


def stack_clean(cfg: "Config", stack: str) -> Path:
    if stack == "sp500":
        return cfg.paths.sp500_clean
    if stack == "queried":
        return cfg.paths.queried_clean
    raise ValueError(f"unknown stack {stack!r}; expected one of {VALID_STACKS}")


def stack_record(cfg: "Config", stack: str) -> Path:
    if stack == "sp500":
        return cfg.paths.sp500_record
    if stack == "queried":
        return cfg.paths.queried_record
    raise ValueError(f"unknown stack {stack!r}; expected one of {VALID_STACKS}")


@dataclass(frozen=True)
class Config:
    paths: Paths
    sec: SecConfig
    unicorn: UnicornConfig
    universe: UniverseConfig
    download_gzip: bool


def _resolve_paths(data_root: Path, snapshots_override: str | None, universe_file_override: str | None) -> Paths:
    data_root = data_root.resolve()
    snapshots = Path(snapshots_override).resolve() if snapshots_override else data_root / "universe_snapshots"
    universe_json = Path(universe_file_override).resolve() if universe_file_override else data_root / "universe.json"
    sp500_root = data_root / "sp500"
    queried_root = data_root / "queried"
    return Paths(
        data_root=data_root,
        sp500_raw=sp500_root / "raw",
        sp500_clean=sp500_root / "clean",
        sp500_record=sp500_root / "record.jsonl",
        sp500_snapshots=sp500_root / "membership_snapshots",
        queried_raw=queried_root / "raw",
        queried_clean=queried_root / "clean",
        queried_record=queried_root / "record.jsonl",
        logs=data_root / "logs",
        snapshots=snapshots,
        universe_json=universe_json,
        db=data_root / "sibyl.db",
        company_tickers=data_root / "company_tickers.json",
        lm_dictionary=data_root / "lm_master_dictionary.csv",
        prices=data_root / "prices",
        exports=data_root / "exports",
        # Deprecated aliases — point at sp500 so legacy callers degrade to
        # operating on the S&P stack. Will be removed in cleanup pass.
        raw=sp500_root / "raw",
        clean=sp500_root / "clean",
    )


def load_config(config_path: str | os.PathLike[str] = "config.yaml", *, load_env: bool = True) -> Config:
    if load_env:
        load_dotenv()

    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} not found. Copy config.example.yaml to config.yaml and edit it."
        )

    with config_path.open() as f:
        raw = yaml.safe_load(f)

    data_root = Path(raw["paths"]["data_root"])
    if not data_root.is_absolute():
        data_root = (config_path.parent / data_root).resolve()

    paths = _resolve_paths(
        data_root,
        raw.get("universe", {}).get("snapshots_dir"),
        raw.get("universe", {}).get("file"),
    )

    return Config(
        paths=paths,
        sec=SecConfig(
            user_agent=raw["sec"]["user_agent"],
            rate_limit_per_sec=int(raw["sec"]["rate_limit_per_sec"]),
        ),
        unicorn=UnicornConfig(
            base_url=raw["unicorn"]["base_url"].rstrip("/"),
            universe_path=raw["unicorn"]["universe_path"],
            expected_contract_version=str(raw["unicorn"]["expected_contract_version"]),
            token=os.environ.get("SIBYL_UNICORN_TOKEN"),
        ),
        universe=UniverseConfig(
            form_types=list(raw["universe"]["form_types"]),
            include_amendments=bool(raw["universe"]["include_amendments"]),
            history_start=str(raw["universe"]["history_start"]),
        ),
        download_gzip=bool(raw.get("download", {}).get("gzip", True)),
    )


def ensure_dirs(config: Config) -> None:
    for p in (
        config.paths.data_root,
        config.paths.sp500_raw,
        config.paths.sp500_clean,
        config.paths.sp500_snapshots,
        config.paths.queried_raw,
        config.paths.queried_clean,
        config.paths.logs,
        config.paths.snapshots,
        config.paths.prices,
        config.paths.exports,
    ):
        p.mkdir(parents=True, exist_ok=True)
    # Touch the record files so JSONL appenders never have to special-case
    # first-run.
    for rec in (config.paths.sp500_record, config.paths.queried_record):
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.touch(exist_ok=True)
