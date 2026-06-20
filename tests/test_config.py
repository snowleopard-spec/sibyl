from pathlib import Path

from sibyl.config import load_config


def test_load_example_config(tmp_path: Path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / "config.example.yaml"

    cfg = load_config(example, load_env=False)

    assert cfg.sec.rate_limit_per_sec == 8
    assert "10-K" in cfg.universe.form_types
    assert "10-Q" in cfg.universe.form_types
    assert cfg.universe.include_amendments is False
    assert cfg.download_gzip is True
    # data_root resolves to a real absolute path
    assert cfg.paths.data_root.is_absolute()
    assert cfg.paths.sp500_raw.name == "raw"
    assert cfg.paths.sp500_clean.name == "clean"
    assert cfg.paths.queried_raw.name == "raw"
    assert cfg.paths.db.name == "sibyl.db"
