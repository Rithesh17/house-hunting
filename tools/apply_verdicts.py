"""Apply Claude/subagent verdicts into the DB.

Reads either:
  - data/_verdicts.json  (a dict {id: verdict} OR a list [{"id":..,...}]), or
  - every data/_verdicts_*.json batch file (each a list or dict), merged.

    py tools/apply_verdicts.py

Each verdict may include: legit_score, legit_label, red_flags, low_polish,
fit_score, is_1br1ba, verdict_summary, recommendation, sqft_estimate, room_type,
category, disposition ("keep"/"reject"), reject_reason.

It may ALSO include an `enrich` block — the Stage-1 "semantic detail entry". Since
the subagent has already read the body + photos + research, it decides the CANONICAL
value of every display field and we trust it over the raw scraped value: e.g. fill
an address the scraper missed but the body states, normalize a price quoted weekly
into a real monthly number, fix a wrong neighborhood/room_type/bed-bath count, clean
a junk title. Only the keys the subagent actually corrects need be present; each is
written over the stored value. (area/neighborhood/address changes are re-classified
into avoid/caution/ok at sync time by geo.classify, so the dashboard area follows.)
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db  # noqa: E402

# Canonical display fields the subagent may overwrite, with a coercion fn. Anything
# the verdict's `enrich` block supplies for these is written over the scraped value.
ENRICH_FIELDS = {
    "title": str,
    "price": int,
    "bedrooms": int,
    "bathrooms": float,
    "sqft": int,
    "room_type": str,
    "housing_type": str,
    "area": str,
    "neighborhood": str,
    "address": str,
    "lat": float,
    "lng": float,
}


def apply_enrich(conn, pid: str, enrich: dict) -> list:
    """Write each corrected field; return the names actually changed."""
    changed = []
    for field, coerce in ENRICH_FIELDS.items():
        if field not in enrich:
            continue
        val = enrich[field]
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        try:
            val = coerce(val)
        except (TypeError, ValueError):
            print(f"  ! {pid}: bad {field}={enrich[field]!r}, skipped")
            continue
        if isinstance(val, str):
            val = val.strip()
        conn.execute(f"UPDATE listings SET {field}=? WHERE id=?", (val, pid))
        changed.append(field)
    return changed


def load_all() -> dict:
    merged: dict = {}
    paths = [os.path.join(db.DATA_DIR, "_verdicts.json")]
    paths += sorted(glob.glob(os.path.join(db.DATA_DIR, "_verdicts_*.json")))
    for p in paths:
        if not os.path.exists(p):
            continue
        data = json.load(open(p, encoding="utf-8"))
        items = data.values() if isinstance(data, dict) else data
        for v in items:
            if v.get("id"):
                merged[v["id"]] = v
    return merged


def main() -> None:
    verdicts = load_all()
    if not verdicts:
        print("No verdict files found (data/_verdicts.json or _verdicts_*.json).")
        return
    conn = db.connect()
    applied = rejected = enriched = 0
    for pid, v in verdicts.items():
        if not db.get(conn, pid):
            print(f"  skip {pid} (not in DB)")
            continue
        db.save_verdict(conn, pid, v)
        # Stage-1 semantic detail entry: trust the subagent's corrected fields.
        enrich = dict(v.get("enrich") or {})
        # Back-compat: a top-level room_type / sqft_estimate folds into enrich.
        if v.get("room_type") and "room_type" not in enrich:
            enrich["room_type"] = v["room_type"]
        if v.get("sqft_estimate") and "sqft" not in enrich and not db.get(conn, pid)["sqft"]:
            enrich["sqft"] = v["sqft_estimate"]
        changed = apply_enrich(conn, pid, enrich)
        if changed:
            enriched += 1
            print(f"  {pid}: enriched {', '.join(changed)}")
        if v.get("disposition") == "reject" and v.get("legit_label") != "likely-scam":
            db.auto_reject(conn, pid, v.get("reject_reason", "filtered in manual review"))
            rejected += 1
        applied += 1
    conn.commit()
    conn.close()
    print(f"Applied {applied} verdict(s); enriched {enriched}; "
          f"{rejected} dispositioned as reject.")


if __name__ == "__main__":
    main()
