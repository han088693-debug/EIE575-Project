"""Check that new results actually reached the report.

Two gaps this catches, both of which happened for real:
  * a results note committed without being cited by REPORT_*.md
  * the report updated hours after the deck, so the deck still shows old numbers

Usage:
    python tools/doc_sync.py             # exit 1 if any gap
    python tools/doc_sync.py --quiet     # only failures

Add a new results source to RESULT_SOURCES in the same commit that adds it.
"""
import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "TABR_project" / "docs"

# Every directory or document that carries experiment results must be cited in the report.
RESULT_SOURCES = [
    "BSWEEP_20261124.md",
    "2026-11-22_补充实验归档",
]

# How far the report may run ahead of the deck before we call the deck stale.
DECK_LAG_S = 30 * 60


def latest_report():
    found = sorted(DOCS.glob("REPORT_*.md"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def check_sources(report_text, rows):
    for name in RESULT_SOURCES:
        ok = name in report_text
        rows.append(("source cited", name, ok, "" if ok else "not mentioned in " + latest_report().name))


def check_deck(report, rows):
    decks = sorted(DOCS.glob("deck/*.pptx"), key=lambda p: p.stat().st_mtime)
    if not decks:
        rows.append(("deck freshness", "(no pptx)", False, "deck missing"))
        return
    deck = decks[-1]
    lag = report.stat().st_mtime - deck.stat().st_mtime
    ok = lag <= DECK_LAG_S
    rows.append((
        "deck freshness", deck.name, ok,
        "" if ok else "report is %d min newer; re-export the deck" % (lag // 60),
    ))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    report = latest_report()
    if report is None:
        print("no REPORT_*.md under", DOCS)
        return 1
    text = io.open(report, encoding="utf-8", errors="replace").read()

    rows = []
    check_sources(text, rows)
    check_deck(report, rows)

    bad = [r for r in rows if not r[2]]
    if not args.quiet:
        print("report:", report.relative_to(ROOT))
        for kind, name, ok, note in rows:
            print("  %-16s %-40s %s %s" % (kind, name, "OK" if ok else "GAP", note))
    if bad:
        print("\n%d gap(s): new results or doc edits did not reach all deliverables." % len(bad))
        return 1
    print("\nall deliverables in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
