from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import chart as chart_mod
from . import config as config_mod
from . import db as db_mod
from . import download as download_mod
from . import edgar
from . import parse as parse_mod
from . import diff as diff_mod
from . import queried as queried_mod
from . import runner as runner_mod
from . import score as score_mod
from . import sections as sections_mod
from . import sp500 as sp500_mod
from .config import stack_clean


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


def cmd_download(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")

    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    counts = download_mod.download_all(
        conn, cfg,
        stack=args.stack,
        ciks=args.cik or None,
        limit=args.limit,
        refresh_submissions=args.refresh_submissions,
    )

    raw_root = config_mod.stack_raw(cfg, args.stack)
    raw_size = _disk_usage(raw_root)
    log.info("CIKs processed: %d", counts.ciks_processed)
    log.info("New filings:    %d", counts.new_filings)
    log.info("Skipped:        %d", counts.skipped)
    log.info("Failed:         %d", counts.failed)
    log.info("Raw disk usage: %s (stack=%s)", _human_size(raw_size), args.stack)
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)
    clean_root = stack_clean(cfg, args.stack)

    if args.stats:
        print(parse_mod.render_stats(parse_mod.compute_stats(conn, clean_root)))
        return 0

    if args.sample:
        samples = parse_mod.pick_samples(clean_root, args.sample, suspicious_only=args.suspicious)
        if not samples:
            print("(no samples available — run `sibyl parse` first or relax filters)")
            return 0
        for cik, accession, sections in samples:
            print(parse_mod.render_sample(clean_root, cik, accession, sections))
        return 0

    counts = parse_mod.parse_all(
        conn, cfg,
        stack=args.stack,
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
        workers=args.workers,
    )
    clean_size = _disk_usage(clean_root)
    log.info("Filings parsed: %d (stack=%s)", counts.parsed, args.stack)
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
    clean_root = stack_clean(cfg, args.stack)

    if args.stats:
        print(sections_mod.render_stats(sections_mod.compute_stats(conn, clean_root)))
        return 0

    if args.sample:
        samples = sections_mod.pick_samples(clean_root, args.sample, suspicious_only=args.suspicious)
        if not samples:
            print("(no samples available — run `sibyl sections` first or relax filters)")
            return 0
        for cik, accession, sec in samples:
            print(sections_mod.render_sample(clean_root, cik, accession, sec))
        return 0

    if args.pick_validation_set:
        path = sections_mod.pick_validation_set(conn, clean_root, args.pick_validation_set)
        print(f"Validation candidates written to: {path}")
        print("Open each filing's full.txt, fill in the start/end substrings, then run --validate.")
        return 0

    if args.validate:
        results = sections_mod.validate_against_labels(clean_root, clean_root / "validation_labels.csv")
        print(sections_mod.render_validation(results))
        return 0

    counts = sections_mod.extract_all(
        conn, cfg,
        stack=args.stack,
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
        workers=args.workers,
    )
    clean_size = _disk_usage(clean_root)
    log.info("Filings processed:  %d (stack=%s)", counts.processed, args.stack)
    log.info("Sections OK (both): %d", counts.both_ok)
    log.info("Partial:            %d", counts.partial)
    log.info("section_fail:       %d", counts.section_fail)
    log.info("Suspicious:         %d", counts.suspicious)
    log.info("Skipped:            %d", counts.skipped)
    log.info("Clean disk:         %s", _human_size(clean_size))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    if args.stats:
        print(score_mod.render_stats(score_mod.compute_stats(conn)))
        return 0

    counts = score_mod.score_all(
        conn, cfg,
        stack=args.stack,
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
    )
    log.info("Filings scored:    %d", counts.processed)
    log.info("Rows written:      %d", counts.rows_written)
    log.info("Skipped (at v%s):   %d", score_mod.SCORER_VERSION, counts.skipped)
    log.info("Errors:            %d", counts.errors)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    if args.stats:
        print(diff_mod.render_stats(diff_mod.compute_stats(conn)))
        return 0

    counts = diff_mod.compute_all(
        conn, cfg,
        stack=args.stack,
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
    )
    log.info("Filings processed: %d", counts.processed)
    log.info("Rows written:      %d", counts.rows_written)
    log.info("Skipped (at v%s):   %d", diff_mod.DIFF_VERSION, counts.skipped)
    log.info("No prior filing:   %d", counts.no_prior)
    log.info("All sections skipped: %d", counts.section_skipped)
    log.info("Errors:            %d", counts.errors)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)
    c = db_mod.counts(conn)

    print(f"data_root:        {cfg.paths.data_root}")
    print(f"db:               {cfg.paths.db}  ({_human_size(cfg.paths.db.stat().st_size if cfg.paths.db.exists() else 0)})")
    print(f"sp500 raw:        {_human_size(_disk_usage(cfg.paths.sp500_raw))}")
    print(f"sp500 clean:      {_human_size(_disk_usage(cfg.paths.sp500_clean))}")
    print(f"queried raw:      {_human_size(_disk_usage(cfg.paths.queried_raw))}")
    print(f"queried clean:    {_human_size(_disk_usage(cfg.paths.queried_clean))}")
    print("counts:")
    for table, n in c.items():
        print(f"  {table:<22} {n}")
    return 0


def cmd_sp500(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    if args.sp500_action == "refresh":
        summary = runner_mod.refresh_sp500(cfg, conn, download=not args.no_download)
        print(runner_mod.render_sp500_summary(summary))
        return 0

    if args.sp500_action == "status":
        stat = sp500_mod.status(conn)
        print(sp500_mod.render_status(stat))
        return 0

    # argparse should prevent this
    print(f"unknown sp500 action: {args.sp500_action}", file=sys.stderr)
    return 2


def cmd_research(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    try:
        summary = runner_mod.query(
            cfg, conn, args.ticker,
            download=not args.no_download,
            chart=not args.no_chart,
            form_type=args.form_type,
        )
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(runner_mod.render_query_summary(summary))
    return 0


def cmd_chart_sp500(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    n_agg = conn.execute("SELECT COUNT(*) FROM sp500_aggregates").fetchone()[0]
    if n_agg == 0:
        print(
            "sp500_aggregates is empty. Run `sibyl sp500 refresh --no-download` "
            "first to populate it.",
            file=sys.stderr,
        )
        return 1

    if args.output:
        out_path = Path(args.output)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = cfg.paths.queried_raw.parent / f"chart_sp500_{stamp}.png"

    chart_mod.render_sp500_chart(conn, output_path=out_path, form_type=args.form_type)
    print(f"Wrote: {out_path}")
    return 0


def cmd_queried(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    if args.queried_action == "status":
        records = queried_mod.read_records(cfg.paths.queried_record)
        # Group by ticker for a quick summary.
        from collections import Counter
        by_ticker: dict[str, int] = Counter(r.get("ticker", "?") for r in records)
        print(f"queried/record.jsonl: {len(records)} filing records, "
              f"{len(by_ticker)} distinct tickers")
        for ticker, n in sorted(by_ticker.items(), key=lambda x: -x[1]):
            print(f"  {ticker:<10} {n}")
        return 0

    print(f"unknown queried action: {args.queried_action}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sibyl", description="SEC filing signal research engine")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_sections = sub.add_parser("sections", help="Stage 3: isolate Item 1A and Item 7 via edgartools")
    p_sections.add_argument(
        "--stack", choices=("sp500", "queried"), default="sp500",
        help="Stack to operate on (default: sp500).",
    )
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
    p_parse.add_argument(
        "--stack", choices=("sp500", "queried"), default="sp500",
        help="Stack to operate on (default: sp500).",
    )
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
    p_parse.add_argument("--workers", type=int, default=None,
                         help="Worker process count (default: min(8, cpu_count-1)). Use 1 for inline path.")
    p_parse.set_defaults(func=cmd_parse)

    p_download = sub.add_parser("download", help="Stage 1: pull SEC filings for the universe")
    p_download.add_argument(
        "--stack", choices=("sp500", "queried"), default="sp500",
        help="Stack to operate on (default: sp500).",
    )
    p_download.add_argument("--cik", type=int, action="append", default=[],
                            help="Download only this CIK (repeatable; useful for smoke tests).")
    p_download.add_argument("--limit", type=int, default=None,
                            help="Stop after N new filings total.")
    p_download.add_argument("--refresh-submissions", action="store_true",
                            help="Re-fetch submissions JSON even if cached.")
    p_download.set_defaults(func=cmd_download)

    p_diff = sub.add_parser("diff", help="Stage 5: yoy similarity + sentiment deltas -> filing_signals")
    p_diff.add_argument(
        "--stack", choices=("sp500", "queried"), default="sp500",
        help="Stack to operate on (default: sp500).",
    )
    p_diff.add_argument("--cik", type=int, action="append", default=[],
                        help="Diff only this CIK (repeatable).")
    p_diff.add_argument("--limit", type=int, default=None, help="Stop after N filings.")
    p_diff.add_argument("--force", action="store_true",
                        help="Re-diff even if filing already has rows at current diff_version.")
    p_diff.add_argument("--stats", action="store_true",
                        help="Print per-section similarity/delta distributions + alignment audit; skip diffing.")
    p_diff.set_defaults(func=cmd_diff)

    p_score = sub.add_parser("score", help="Stage 4: tokenize + L&M counts -> filing_scores")
    p_score.add_argument(
        "--stack", choices=("sp500", "queried"), default="sp500",
        help="Stack to operate on (default: sp500).",
    )
    p_score.add_argument("--cik", type=int, action="append", default=[],
                         help="Score only this CIK (repeatable).")
    p_score.add_argument("--limit", type=int, default=None, help="Stop after N filings.")
    p_score.add_argument("--force", action="store_true",
                         help="Re-score even if filing already has rows at current scorer_version.")
    p_score.add_argument("--stats", action="store_true",
                         help="Print per-section averages; skip scoring.")
    p_score.set_defaults(func=cmd_score)

    # --- Research-tool top-level commands ---

    p_sp500 = sub.add_parser(
        "sp500", help="Manage the S&P 500 stack (membership + filings + aggregates)",
    )
    sp500_sub = p_sp500.add_subparsers(dest="sp500_action", required=True)
    p_sp500_refresh = sp500_sub.add_parser(
        "refresh", help="Pull IVV membership; optionally pull missing filings; rebuild aggregates.",
    )
    p_sp500_refresh.add_argument(
        "--no-download", action="store_true",
        help="Membership + aggregate rebuild only; do not download new SEC filings.",
    )
    p_sp500_status = sp500_sub.add_parser(
        "status", help="Print current S&P membership counts + per-sector breakdown.",
    )
    p_sp500.set_defaults(func=cmd_sp500)

    p_research = sub.add_parser(
        "research", help="Query a single ticker → compare against S&P + sector averages.",
    )
    p_research.add_argument("ticker", help="Ticker symbol (BRK.B / BRK-B both work).")
    p_research.add_argument(
        "--no-download", action="store_true",
        help="Use cached filings only; do not download anything missing.",
    )
    p_research.add_argument(
        "--no-chart", action="store_true",
        help="Skip PNG chart rendering; return summary JSON to stdout.",
    )
    p_research.add_argument(
        "--form-type", choices=("10-K", "10-Q"), default="10-K",
        help="Filing type to chart (default: 10-K for clean annual trend).",
    )
    p_research.set_defaults(func=cmd_research)

    p_chart_sp500 = sub.add_parser(
        "chart-sp500",
        help="Render a 6-panel S&P 500 aggregate trend chart (all sectors + S&P mean).",
    )
    p_chart_sp500.add_argument(
        "--output", help="PNG output path (default: data/queried/chart_sp500_<stamp>.png).",
    )
    p_chart_sp500.add_argument(
        "--form-type", choices=("10-K", "10-Q"), default="10-K",
        help="Filing type to chart (default: 10-K for clean annual trend).",
    )
    p_chart_sp500.set_defaults(func=cmd_chart_sp500)

    p_queried = sub.add_parser(
        "queried", help="Inspect the queried-stack cache (tickers fetched on demand).",
    )
    queried_sub = p_queried.add_subparsers(dest="queried_action", required=True)
    p_queried_status = queried_sub.add_parser(
        "status", help="Print queried-stack record counts grouped by ticker.",
    )
    p_queried.set_defaults(func=cmd_queried)

    p_status = sub.add_parser("status", help="DB and disk counts")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
