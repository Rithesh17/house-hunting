"""Delete locally-cached listing images to free disk. Safe once listings have
remote image_urls (the dashboard embeds those). Local images are only needed
transiently during subagent vetting.

    py tools/purge_images.py            # purge listings that have remote image_urls
    py tools/purge_images.py --all      # purge everything under data/images
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import common  # noqa: E402
import db  # noqa: E402

all_mode = "--all" in sys.argv
conn = db.connect()
freed = 0
if all_mode:
    if os.path.isdir(common.IMAGES_DIR):
        for d in os.listdir(common.IMAGES_DIR):
            p = os.path.join(common.IMAGES_DIR, d)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True); freed += 1
        conn.execute("UPDATE listings SET image_dir=NULL, image_count=0")
else:
    rows = conn.execute("SELECT id, image_dir FROM listings "
                        "WHERE image_urls IS NOT NULL AND image_urls != ''").fetchall()
    for r in rows:
        p = r["image_dir"] or os.path.join(common.IMAGES_DIR, r["id"])
        if p and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True); freed += 1
        conn.execute("UPDATE listings SET image_dir=NULL, image_count=0 WHERE id=?", (r["id"],))
conn.commit()
conn.close()
print(f"purged local images for {freed} listing(s); dashboard now uses remote URLs")
