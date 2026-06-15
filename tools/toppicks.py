"""Print the top-N primary (deduped, non-scam, non-rejected) listing ids by match
score, one per line — for sending to Telegram.

    py tools/toppicks.py [N]
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
conn = db.connect()
rows = conn.execute(
    "SELECT id FROM listings "
    "WHERE status NOT IN ('rejected','removed') AND legit_label != 'likely-scam' "
    "AND dup_group = id AND fit_score IS NOT NULL "
    "ORDER BY fit_score DESC, legit_score DESC LIMIT ?", (N,)
).fetchall()
for r in rows:
    print(r["id"])
