from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import db as db_mod
from . import download as download_mod
from . import edgar, universe
from . import parse as parse_mod
from . import sections as sections_mod

STUB_COMMANDS = ("score", "diff", "prices", "panel", "eval", "export")


def _configure_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs_dir / f"sibyl_{stamp}.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_path), logging.StreamHandler(sys.stderr)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _disk_usage(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PiB"


def cmd_universe(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")

    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    payload = universe.fetch_unicorn_universe(cfg)
    snapshot_path = universe.snapshot_universe(payload, cfg.paths.snapshots, cfg.paths.universe_json)
    as_of, n_rows = universe.upsert_membership(conn, payload)

    limiter = edgar.RateLimiter(cfg.sec.rate_limit_per_sec)
    edgar.download_company_tickers(
        cfg.paths.company_tickers,
        user_agent=cfg.sec.user_agent,
        limiter=limiter,
        refresh=args.refresh_tickers,
    )
    ticker_map = edgar.load_ticker_to_cik(cfg.paths.company_tickers)
    unresolved = universe.resolve_ciks(conn, ticker_map, as_of)

    log.info("Snapshot:    %s", snapshot_path)
    log.info("as_of_date:  %s", as_of)
    log.info("Rows:        %d", n_rows)
    log.info("Resolved:    %d", n_rows - len(unresolved))
    log.info("Unresolved:  %d", len(unresolved))
    if unresolved:
        sample = ", ".join(unresolved[:20])
        log.info("Unresolved sample (first 20): %s", sample)
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")

    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    counts = download_mod.download_all(
        conn, cfg,
        ciks=args.cik or None,
        limit=args.limit,
        refresh_submissions=args.refresh_submissions,
    )

    raw_size = _disk_usage(cfg.paths.raw)
    log.info("CIKs processed: %d", counts.ciks_processed)
    log.info("New filings:    %d", counts.new_filings)
    log.info("Skipped:        %d", counts.skipped)
    log.info("Failed:         %d", counts.failed)
    log.info("Raw disk usage: %s", _human_size(raw_size))
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    if args.stats:
        print(parse_mod.render_stats(parse_mod.compute_stats(conn, cfg.paths.clean)))
        return 0

    if args.sample:
        samples = parse_mod.pick_samples(cfg.paths.clean, args.sample, suspicious_only=args.suspicious)
        if not samples:
            print("(no samples available — run `sibyl parse` first or relax filters)")
            return 0
        for cik, accession, sections in samples:
            print(parse_mod.render_sample(cfg.paths.clean, cik, accession, sections))
        return 0

    counts = parse_mod.parse_all(
        conn, cfg,
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
    )
    clean_size = _disk_usage(cfg.paths.clean)
    log.info("Filings parsed: %d", counts.parsed)
    log.info("Failed:         %d", counts.failed)
    log.info("Suspicious:     %d", counts.suspicious)
    log.info("Clean disk:     %s", _human_size(clean_size))
    return 0


def cmd_sections(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    if args.stats:
        print(sections_mod.render_stats(sections_mod.compute_stats(conn, cfg.paths.clean)))
        return 0

    if args.sample:
        samples = sections_mod.pick_samples(cfg.paths.clean, args.sample, suspicious_only=args.suspicious)
        if not samples:
            print("(no samples available — run `sibyl sections` first or relax filters)")
            return 0
        for cik, accession, sec in samples:
            print(sections_mod.render_sample(cfg.paths.clean, cik, accession, sec))
        return 0

    if args.pick_validation_set:
        path = sections_mod.pick_validation_set(conn, cfg.paths.clean, args.pick_validation_set)
        print(f"Validation candidates written to: {path}")
        print("Open each filing's full.txt, fill in the start/end substrings, then run --validate.")
        return 0

    if args.validate:
        results = sections_mod.validate_against_labels(cfg.paths.clean, cfg.paths.clean / "validation_labels.csv")
        print(sections_mod.render_validation(results))
        return 0

    counts = sections_mod.extract_all(
        conn, cfg,
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
        workers=args.workers,
    )
    clean_size = _disk_usage(cfg.paths.clean)
    log.info("Filings processed:  %d", counts.processed)
    log.info("Sections OK (both): %d", counts.both_ok)
    log.info("section_fail:       %d", counts.section_fail)
    log.info("Suspicious:         %d", counts.suspicious)
    log.info("Skipped:            %d", counts.skipped)
    log.info("Clean disk:         %s", _human_size(clean_size))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)
    c = db_mod.counts(conn)

    print(f"data_root:    {cfg.paths.data_root}")
    print(f"db:           {cfg.paths.db}  ({_human_size(cfg.paths.db.stat().st_size if cfg.paths.db.exists() else 0)})")
    print(f"raw size:     {_human_size(_disk_usage(cfg.paths.raw))}")
    print(f"clean size:   {_human_size(_disk_usage(cfg.paths.clean))}")
    print("counts:")
    for table, n in c.items():
        print(f"  {table:<22} {n}")
    return 0


def cmd_stub(args: argparse.Namespace) -> int:
    print(f"`sibyl {args.subcommand}` is not implemented yet (planned stage).", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sibyl", description="SEC filing signal research engine")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_universe = sub.add_parser("universe", help="Stage 0: fetch + snapshot universe; resolve CIKs")
    p_universe.add_argument(
        "--refresh-tickers", action="store_true",
        help="Re-download SEC company_tickers.json even if cached.",
    )
    p_universe.set_defaults(func=cmd_universe)

    p_sections = sub.add_parser("sections", help="Stage 3: isolate Item 1A and Item 7 via edgartools")
    p_sections.add_argument("--cik", type=int, action="append", default=[],
                            help="Process only this CIK (repeatable).")
    p_sections.add_argument("--limit", type=int, default=None, help="Stop after N filings.")
    p_sections.add_argument("--force", action="store_true",
                            help="Re-extract even if section_extractor_version matches.")
    p_sections.add_argument("--stats", action="store_true",
                            help="Print bulk diagnostics; skip extraction.")
    p_sections.add_argument("--sample", type=int, default=None,
                            help="Emit N random extractions for eyeballing.")
    p_sections.add_argument("--suspicious", action="store_true",
                            help="With --sample, draw from flagged or section_fail filings.")
    p_sections.add_argument("--pick-validation-set", type=int, default=None, metavar="N",
                            dest="pick_validation_set",
                            help="Emit data/clean/validation_labels.csv with N candidate filings.")
    p_sections.add_argument("--validate", action="store_true",
                            help="Run accuracy check against validation_labels.csv.")
    p_sections.add_argument("--workers", type=int, default=None,
                            help="Worker process count (default: min(8, cpu_count-1)).")
    p_sections.set_defaults(func=cmd_sections)

    p_parse = sub.add_parser("parse", help="Stage 2: clean raw HTML -> full.txt")
    p_parse.add_argument("--cik", type=int, action="append", default=[],
                         help="Parse only this CIK (repeatable).")
    p_parse.add_argument("--limit", type=int, default=None, help="Stop after N filings.")
    p_parse.add_argument("--force", action="store_true",
                         help="Re-parse even if parse_status is set.")
    p_parse.add_argument("--stats", action="store_true",
                         help="Print bulk diagnostics on already-parsed corpus; skip parsing.")
    p_parse.add_argument("--sample", type=int, default=None,
                         help="Emit N random successful filings to stdout for eyeballing.")
    p_parse.add_argument("--suspicious", action="store_true",
                         help="With --sample, draw from filings with at least one suspicious flag.")
    p_parse.set_defaults(func=cmd_parse)

    p_download = sub.add_parser("download", help="Stage 1: pull SEC filings for the universe")
    p_download.add_argument("--cik", type=int, action="append", default=[],
                            help="Download only this CIK (repeatable; useful for smoke tests).")
    p_download.add_argument("--limit", type=int, default=None,
                            help="Stop after N new filings total.")
    p_download.add_argument("--refresh-submissions", action="store_true",
                            help="Re-fetch submissions JSON even if cached.")
    p_download.set_defaults(func=cmd_download)

    p_status = sub.add_parser("status", help="DB and disk counts")
    p_status.set_defaults(func=cmd_status)

    for name in STUB_COMMANDS:
        p = sub.add_parser(name, help=f"(stub) Stage for `{name}`; not implemented yet")
        p.set_defaults(func=cmd_stub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
