"""Apply Claude/subagent verdicts into the DB.

Reads either:
  - data/_verdicts.json  (a dict {id: verdict} OR a list [{"id":..,...}]), or
  - every data/_verdicts_*.json batch file (each a list or dict), merged.

    py tools/apply_verdicts.py

Each verdict may include: legit_score, legit_label, red_flags, low_polish,
fit_score, is_1br1ba, verdict_summary, recommendation, sqft_estimate, room_type,
category, disposition ("keep"/"reject"), reject_reason.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db  # noqa: E402


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
    applied = rejected = 0
    for pid, v in verdicts.items():
        if not db.get(conn, pid):
            print(f"  skip {pid} (not in DB)")
            continue
        db.save_verdict(conn, pid, v)
        if v.get("room_type"):
            conn.execute("UPDATE listings SET room_type=? WHERE id=?", (v["room_type"], pid))
        if v.get("sqft_estimate") and not db.get(conn, pid)["sqft"]:
            conn.execute("UPDATE listings SET sqft=? WHERE id=?", (v["sqft_estimate"], pid))
        if v.get("disposition") == "reject" and v.get("legit_label") != "likely-scam":
            db.auto_reject(conn, pid, v.get("reject_reason", "filtered in manual review"))
            rejected += 1
        applied += 1
    conn.commit()
    conn.close()
    print(f"Applied {applied} verdict(s); {rejected} dispositioned as reject.")


if __name__ == "__main__":
    main()
