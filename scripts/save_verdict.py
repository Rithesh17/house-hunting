"""Persist Claude's vetting verdict for a listing.

Claude Code calls this after viewing the photos + reading the description.

Usage:
    py scripts/save_verdict.py <post_id> --json '{...}'
    py scripts/save_verdict.py <post_id> --file verdict.json

Expected JSON shape:
    {
      "legit_score": 0-100,
      "legit_label": "likely-legit" | "unverified-amateur" | "likely-scam",
      "red_flags": ["...", "..."],
      "low_polish": true/false,
      "fit_score": 0-100,
      "is_1br1ba": true/false,
      "verdict_summary": "one or two sentences",
      "recommendation": "what the user should do"
    }
"""
from __future__ import annotations

import argparse
import json
import sys

import db

VALID_LABELS = {"likely-legit", "unverified-amateur", "likely-scam"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("id")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--json", help="verdict as a JSON string")
    g.add_argument("--file", help="path to a JSON file")
    args = ap.parse_args()

    raw = open(args.file, encoding="utf-8").read() if args.file else args.json
    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    label = verdict.get("legit_label")
    if label and label not in VALID_LABELS:
        print(f"Warning: legit_label {label!r} not in {VALID_LABELS}",
              file=sys.stderr)

    conn = db.connect()
    if not db.get(conn, args.id):
        print(f"No listing {args.id} in DB.", file=sys.stderr)
        sys.exit(1)
    db.save_verdict(conn, args.id, verdict)
    conn.commit()
    conn.close()

    print(f"Saved verdict for {args.id}: "
          f"legit={verdict.get('legit_score')} ({label}), "
          f"fit={verdict.get('fit_score')}, "
          f"1br1ba={verdict.get('is_1br1ba')}")


if __name__ == "__main__":
    main()
