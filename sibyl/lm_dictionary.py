from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

CATEGORIES = (
    "Negative",
    "Positive",
    "Uncertainty",
    "Litigious",
    "Strong_Modal",
    "Weak_Modal",
    "Constraining",
)


def load_master_dictionary(path: str | Path) -> dict[str, set[str]]:
    """Loughran-McDonald master dictionary -> {category: {WORD, ...}}.

    Membership rule per spec §10: a non-zero value in the category column
    (which stores the year of assignment) means the word belongs.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Download Loughran-McDonald_MasterDictionary_*.csv "
            f"from https://sraf.nd.edu/loughranmcdonald-master-dictionary/ and save it there."
        )

    out: dict[str, set[str]] = {c: set() for c in CATEGORIES}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in CATEGORIES if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"L&M CSV is missing expected columns: {missing}. "
                f"Found columns: {reader.fieldnames}"
            )
        word_col = "Word" if "Word" in reader.fieldnames else reader.fieldnames[0]
        for row in reader:
            word = row[word_col].strip().lower()
            if not word:
                continue
            for cat in CATEGORIES:
                val = row[cat].strip()
                try:
                    flagged = int(val) != 0
                except ValueError:
                    flagged = False
                if flagged:
                    out[cat].add(word)
    return out


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/lm_master_dictionary.csv")
    try:
        d = load_master_dictionary(path)
    except FileNotFoundError as e:
        print(e)
        return 1
    total_rows = sum(1 for _ in path.open(encoding="utf-8")) - 1
    print(f"Loaded {path}")
    print(f"  Rows (excluding header): {total_rows}")
    for cat in CATEGORIES:
        print(f"  {cat:<14} {len(d[cat])} words")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
