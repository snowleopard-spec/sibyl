from pathlib import Path

from sibyl.config import load_config


def test_load_example_config(tmp_path: Path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / "config.example.yaml"

    monkeypatch.delenv("SIBYL_UNICORN_TOKEN", raising=False)
    cfg = load_config(example, load_env=False)

    assert cfg.sec.rate_limit_per_sec == 8
    assert cfg.unicorn.universe_path == "/api/universe"
    assert cfg.unicorn.expected_contract_version == "1.0"
    assert cfg.unicorn.token is None
    assert "10-K" in cfg.universe.form_types
    assert cfg.universe.include_amendments is False
    assert cfg.download_gzip is True
    # data_root resolves to a real absolute path
    assert cfg.paths.data_root.is_absolute()
    assert cfg.paths.raw.name == "raw"
    assert cfg.paths.db.name == "sibyl.db"


def test_token_picked_up_from_env(monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / "config.example.yaml"
    monkeypatch.setenv("SIBYL_UNICORN_TOKEN", "test-token-xyz")
    cfg = load_config(example, load_env=False)
    assert cfg.unicorn.token == "test-token-xyz"
