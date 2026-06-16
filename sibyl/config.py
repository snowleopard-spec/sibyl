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
    raw: Path
    clean: Path
    logs: Path
    snapshots: Path
    universe_json: Path
    db: Path
    company_tickers: Path
    lm_dictionary: Path
    prices: Path
    exports: Path


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
    return Paths(
        data_root=data_root,
        raw=data_root / "raw",
        clean=data_root / "clean",
        logs=data_root / "logs",
        snapshots=snapshots,
        universe_json=universe_json,
        db=data_root / "sibyl.db",
        company_tickers=data_root / "company_tickers.json",
        lm_dictionary=data_root / "lm_master_dictionary.csv",
        prices=data_root / "prices",
        exports=data_root / "exports",
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
        config.paths.raw,
        config.paths.clean,
        config.paths.logs,
        config.paths.snapshots,
        config.paths.prices,
        config.paths.exports,
    ):
        p.mkdir(parents=True, exist_ok=True)
