"""Quick pipeline status: counts by status, detail-fetched, vetted, candidates."""
import os, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db

conn = db.connect()
rows = conn.execute("SELECT * FROM listings").fetchall()
print("TOTAL:", len(rows))
print("status:", dict(collections.Counter(r["status"] for r in rows)))
print("detail-fetched:", sum(1 for r in rows if r["detail_fetched_at"]))
print("vetted (has fit):", sum(1 for r in rows if r["fit_score"] is not None))
print("reject reasons:", dict(collections.Counter(
    r["reject_reason"] for r in rows if r["status"] == "rejected")))
print("\nnot-yet-vetted survivors (status='new', detail fetched):")
surv = [r for r in rows if r["status"] == "new" and r["detail_fetched_at"]]
print("  count:", len(surv))
