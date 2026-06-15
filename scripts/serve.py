"""Local dashboard server (Flask).

    py scripts/serve.py            # http://localhost:8000

Routes:
    GET  /                         -> dashboard page
    GET  /api/listings            -> JSON of all listings (+ verdicts)
    POST /api/listings/<id>/status -> {"status": "..."} update
    GET  /images/<id>/<file>      -> downloaded listing photo
"""
from __future__ import annotations

import json
import os
from collections import defaultdict

from flask import Flask, jsonify, request, send_from_directory, abort

import common
import db

DASHBOARD_DIR = os.path.join(common.ROOT, "dashboard")
app = Flask(__name__, static_folder=None)


@app.get("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "index.html")


@app.get("/<path:fname>")
def static_files(fname: str):
    full = os.path.join(DASHBOARD_DIR, fname)
    if os.path.isfile(full):
        return send_from_directory(DASHBOARD_DIR, fname)
    abort(404)


@app.get("/api/listings")
def api_listings():
    conn = db.connect()
    rows = conn.execute("SELECT * FROM listings").fetchall()
    conn.close()
    objs = {}
    for r in rows:
        d = db.row_to_dict(r)
        # Prefer remote Craigslist image URLs (no local copies needed); fall
        # back to any locally-cached files only if remote URLs aren't stored.
        photos = []
        if d.get("image_urls"):
            try:
                photos = json.loads(d["image_urls"])
            except (ValueError, TypeError):
                photos = []
        if not photos and d.get("image_dir") and os.path.isdir(d["image_dir"]):
            for fn in sorted(os.listdir(d["image_dir"])):
                if fn.lower().endswith((".jpg", ".jpeg", ".png")):
                    photos.append(f"/images/{d['id']}/{fn}")
        d["photos"] = photos
        objs[d["id"]] = d

    # Collapse duplicate clusters: one tile per dup_group (the primary), with
    # the other reposts attached as `duplicates`, ordered best-first.
    groups = defaultdict(list)
    for d in objs.values():
        groups[d.get("dup_group") or d["id"]].append(d)

    items = []
    for gid, members in groups.items():
        members.sort(key=lambda m: (m.get("fit_score") or -1,
                                    m.get("legit_score") or -1), reverse=True)
        primary = objs.get(gid, members[0])
        others = [m for m in members if m["id"] != primary["id"]]
        primary["dup_count"] = len(members)
        primary["duplicates"] = [
            {"id": m["id"], "url": m["url"], "price": m["price"],
             "fit_score": m["fit_score"], "legit_score": m["legit_score"],
             "legit_label": m["legit_label"], "area": m["area"],
             "title": m["title"], "room_type": m["room_type"],
             "source": m["source"]}
            for m in others
        ]
        items.append(primary)
    return jsonify(items)


@app.post("/api/listings/<post_id>/status")
def api_set_status(post_id: str):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in db.STATUSES:
        return jsonify({"error": f"status must be one of {db.STATUSES}"}), 400
    conn = db.connect()
    if not db.get(conn, post_id):
        conn.close()
        return jsonify({"error": "not found"}), 404
    db.set_status(conn, post_id, status)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": post_id, "status": status})


@app.get("/images/<post_id>/<fname>")
def images(post_id: str, fname: str):
    folder = os.path.join(common.IMAGES_DIR, post_id)
    return send_from_directory(folder, fname)


if __name__ == "__main__":
    print("Dashboard:  http://localhost:8000")
    app.run(host="127.0.0.1", port=8000, debug=False)
