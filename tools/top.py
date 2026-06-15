"""List the top non-scam candidates by match score, plus address/geocode stats."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db

conn = db.connect()
rows = conn.execute(
    "SELECT * FROM listings WHERE status != 'rejected' AND legit_label != 'likely-scam'"
).fetchall()
rows = [r for r in rows if r["fit_score"] is not None]
rows.sort(key=lambda r: -r["fit_score"])

print(f"{len(rows)} non-scam candidates. Top 20 by match:\n")
print(f"{'fit':>3} {'trust':>5} {'price':>6}  {'type':<8} {'area':<26} addr/phone")
for r in rows[:20]:
    extras = []
    if r["address"]:
        extras.append("addr")
    if r["phone"]:
        extras.append("ph:" + r["phone"])
    print(f"{r['fit_score']:>3} {r['legit_score']:>5} ${r['price'] or '?':<5} "
          f"{(r['room_type'] or '?'):<8} {(r['area'] or '?')[:26]:<26} {' '.join(extras)}")

with_addr = sum(1 for r in conn.execute("SELECT address FROM listings").fetchall() if r["address"])
with_phone = sum(1 for r in conn.execute("SELECT phone FROM listings").fetchall() if r["phone"])
scams = conn.execute("SELECT count(*) c FROM listings WHERE legit_label='likely-scam'").fetchone()["c"]
print(f"\naddresses captured: {with_addr} | phones captured: {with_phone} | scams flagged: {scams}")
