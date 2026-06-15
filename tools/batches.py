"""Print survivor ids (status='new', detail-fetched) ordered by area preference
then price, split into comma-separated groups for subagent vetting."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db, common

cfg = common.load_config()
weight = {a["name"]: a.get("weight", 5) for a in cfg["areas"]}
conn = db.connect()
rows = [r for r in conn.execute(
    "SELECT * FROM listings WHERE status='new' AND detail_fetched_at IS NOT NULL").fetchall()]

def key(r):
    return (-weight.get(r["area"], 0), r["price"] or 99999)

rows.sort(key=key)
ids = [r["id"] for r in rows]
print(f"{len(ids)} survivors\n")
N = 13
for i in range(0, len(ids), N):
    print(f"[batch {i//N + 1}] " + ",".join(ids[i:i+N]))
