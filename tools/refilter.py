"""Re-apply the hard filters (scripts/filters.py) to every already-detailed
listing, using stored data — no re-fetching. Idempotent.

- Listings that now fail a filter are auto-rejected (status='rejected' + reason).
- Listings previously AUTO-rejected (have a reject_reason) that now pass are
  restored to 'new'. Manual statuses (interested/contacted) are left alone.

    py tools/refilter.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db  # noqa: E402
import filters  # noqa: E402

conn = db.connect()
rows = conn.execute("SELECT * FROM listings WHERE detail_fetched_at IS NOT NULL").fetchall()

rejected = restored = kept = 0
for r in rows:
    reason = filters.auto_reject_reason(
        title=r["title"], description=r["description"],
        neighborhood=r["neighborhood"], image_count=r["image_count"],
        lat=r["lat"], lng=r["lng"], price=r["price"],
    )
    if reason:
        if r["status"] != "rejected" or r["reject_reason"] != reason:
            db.auto_reject(conn, r["id"], reason)
            rejected += 1
    else:
        # restore a previously auto-rejected listing that now passes
        if r["status"] == "rejected" and r["reject_reason"]:
            conn.execute(
                "UPDATE listings SET status='new', reject_reason=NULL WHERE id=?",
                (r["id"],))
            restored += 1
        else:
            kept += 1
conn.commit()
conn.close()
print(f"refilter: {rejected} rejected, {restored} restored, {kept} kept clean")
