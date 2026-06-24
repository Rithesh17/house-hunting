"""Fetch Craigslist reply-contact details (relay email + phone) for listings, via
the LOCAL chromerpc service using raw CDP + human-like mouse movement.

Craigslist hides contact behind the JS 'reply' button, so we drive a real browser:
navigate -> human-Bezier-click 'reply' -> enumerate the reply-option-header buttons
from the DOM -> human-click each -> read the revealed mailto / tel. Coordinates come
from the DOM bbox (the buttons shift with viewport width, so fixed pixels don't work),
but the CLICK is a human-like Bezier move + press/release. The reply panel loads
ASYNC so we poll for it. Results are stored on the listing row (reply_email + phone).

IMPORTANT: ONE pass per listing. Craigslist throttles repeated reply requests from
one IP — re-hitting a listing makes it drop the 'call' option or block reply. So we
skip listings already fetched (unless --force) and space requests out.

Prereq — run chromerpc locally first (headless is fine):
    cd chromerpc && ./bin/chromerpc -headless -addr :50051 &

    python3 scripts/fetch_cl_contacts.py --all-vetted        # all vetted CL, unfetched
    python3 scripts/fetch_cl_contacts.py 7942959383 7942...  # specific ids
    python3 scripts/fetch_cl_contacts.py --all-vetted --force --delay 15
"""
from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
import time

import db

GRPC = "localhost:50051"

# ---- chromerpc raw-CDP helpers -------------------------------------------------
def _call(method: str, payload: dict) -> dict:
    cmd = ["grpcurl", "-plaintext", "-max-time", "40", "-d", json.dumps(payload), GRPC, method]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"_err": (r.stderr or r.stdout)[:160]}

def _parse(v):
    for _ in range(3):
        if not isinstance(v, str):
            return v
        try:
            v = json.loads(v)
        except Exception:
            return v
    return v

def ev(expr: str):
    r = _call("cdp.runtime.RuntimeService/Evaluate", {"expression": expr, "return_by_value": True})
    return _parse((r.get("result") or {}).get("value"))

def navigate(url: str):
    _call("cdp.page.PageService/Navigate", {"url": url})

def _mouse(t, x, y, **kw):
    p = {"type": t, "x": x, "y": y}; p.update(kw)
    _call("cdp.input.InputService/DispatchMouseEvent", p)

def _smooth(t):  # smootherstep -> slow start, fast middle, slow end (ease-in-out)
    return t * t * t * (t * (t * 6 - 15) + 10)

def _bezier(S, E, n=None):
    """Human-ish cursor path: a bowed cubic Bezier, sampled with ease-in-out timing
    (slow at the ends), small positional jitter, and randomized control points."""
    dist = math.hypot(E[0]-S[0], E[1]-S[1])
    n = n or max(10, min(36, int(dist / 14)))
    bow = random.uniform(0.12, 0.32) * (1 if random.random() < 0.5 else -1)
    C1 = (S[0]+(E[0]-S[0])*0.33 + random.uniform(-30, 30), S[1]+(E[1]-S[1])*0.33 + dist*bow*0.5)
    C2 = (S[0]+(E[0]-S[0])*0.66 + random.uniform(-30, 30), S[1]+(E[1]-S[1])*0.66 + dist*bow*0.5)
    for i in range(n + 1):
        u = _smooth(i / n); mt = 1 - u
        x = mt**3*S[0]+3*mt*mt*u*C1[0]+3*mt*u*u*C2[0]+u**3*E[0] + random.uniform(-1.3, 1.3)
        y = mt**3*S[1]+3*mt*mt*u*C1[1]+3*mt*u*u*C2[1]+u**3*E[1] + random.uniform(-1.3, 1.3)
        _mouse("mouseMoved", round(x, 1), round(y, 1))
        time.sleep(random.uniform(0.008, 0.02))
    return E

def human_click(E, start=(640, 430)):
    # approach with a slight overshoot, then a small correction onto the target
    over = (E[0] + random.uniform(-7, 7), E[1] + random.uniform(-6, 6))
    _bezier(start, over)
    time.sleep(random.uniform(0.04, 0.11))
    _bezier(over, E, n=5)
    time.sleep(random.uniform(0.13, 0.30))   # aim/settle before pressing
    _mouse("mouseMoved", E[0], E[1])
    _mouse("mousePressed", E[0], E[1], button="left", buttons=1, click_count=1)
    time.sleep(random.uniform(0.05, 0.13))
    _mouse("mouseReleased", E[0], E[1], button="left", buttons=0, click_count=1)

def warmup():
    """Look like a human reading: a couple of idle cursor wanders + scroll down/up."""
    _bezier((random.randint(250, 450), random.randint(180, 280)),
            (random.randint(520, 820), random.randint(320, 520)))
    for dy in (random.randint(350, 600), random.randint(250, 450), -random.randint(300, 550)):
        _mouse("mouseWheel", random.randint(450, 650), random.randint(350, 450), deltaX=0, deltaY=dy)
        time.sleep(random.uniform(0.5, 1.2))
    _bezier((random.randint(500, 800), random.randint(300, 500)),
            (random.randint(200, 400), random.randint(150, 300)))
    time.sleep(random.uniform(0.4, 0.9))

def _poll(get, ok, timeout=14, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = get()
        if ok(v):
            return v
        time.sleep(every)
    return get()

# ---- DOM expressions ----------------------------------------------------------
REPLY = ("(function(){var b=document.querySelector('button.reply-button')||"
         "[...document.querySelectorAll('button,a')].find(function(e){return e.textContent.trim().toLowerCase()==='reply';});"
         "if(!b)return '';var r=b.getBoundingClientRect();"
         "return JSON.stringify({cx:Math.round(r.x+r.width/2),cy:Math.round(r.y+r.height/2)});})()")
OPTION_NAMES = ("JSON.stringify([...document.querySelectorAll('button.reply-option-header')]"
                ".map(function(b){return b.textContent.replace(/\\s+/g,' ').trim().toLowerCase();}))")
def OPT_POS(name):
    return ("(function(){var t=[...document.querySelectorAll('button.reply-option-header')]"
            ".find(function(e){return e.textContent.toLowerCase().indexOf('%s')>=0;});"
            "if(!t)return '';var r=t.getBoundingClientRect();"
            "return JSON.stringify({cx:Math.round(r.x+r.width/2),cy:Math.round(r.y+r.height/2)});})()") % name
MAILTO = "(function(){var a=document.querySelector('a[href^=\"mailto:\"]');return a?a.getAttribute('href'):'';})()"
PHONE = ("(function(){var t=document.querySelector('a[href^=\"tel:\"]');if(t)return t.getAttribute('href');"
         "var p=document.querySelector('.reply-content,.reply-info,[class*=reply]');var s=p?p.innerText:'';"
         "var m=s.match(/\\(\\d{3}\\)\\s*\\d{3}[-.\\s]?\\d{4}|\\d{3}[-.\\s]\\d{3}[-.\\s]\\d{4}/);return m?m[0]:'';})()")


def _email_addr(mailto: str | None) -> str | None:
    if not mailto:
        return None
    m = mailto[len("mailto:"):] if mailto.startswith("mailto:") else mailto
    return m.split("?", 1)[0] or None

def _phone_num(v: str | None) -> str | None:
    if not v:
        return None
    return v[len("tel:"):] if v.startswith("tel:") else v


def fetch_contact(url: str) -> dict:
    """Drive chromerpc to reveal + read the reply options for one listing.
    Returns {email, phone, options, ok, note}."""
    _call("cdp.emulation.EmulationService/ClearDeviceMetricsOverride", {})
    navigate(url)
    time.sleep(random.uniform(2.5, 4.0))
    warmup()                       # idle wander + scroll, like a human reading first
    rb = ev(REPLY)
    if not isinstance(rb, dict):
        return {"ok": False, "note": "no reply button (taken down / blocked)"}
    time.sleep(random.uniform(0.4, 1.1))
    human_click((rb["cx"], rb["cy"]))
    # wait for the async reply panel, then let all option headers (email/call/text) load
    got = _poll(lambda: ev(OPTION_NAMES), lambda v: isinstance(v, list) and len(v) > 0, timeout=14)
    if not (isinstance(got, list) and got):
        return {"ok": False, "note": "no reply options (captcha/throttled?)"}
    time.sleep(3)
    names = ev(OPTION_NAMES) or got
    out = {"ok": True, "options": names, "email": None, "phone": None}
    for name in names:
        pos = ev(OPT_POS(name))
        if not isinstance(pos, dict):
            continue
        human_click((pos["cx"], pos["cy"]), start=(pos["cx"] + 120, pos["cy"] - 30))
        if "email" in name:
            mt = _poll(lambda: ev(MAILTO), lambda v: isinstance(v, str) and v.startswith("mailto"), timeout=12)
            out["email"] = _email_addr(mt)
        else:  # call / text
            ph = _poll(lambda: ev(PHONE), lambda v: isinstance(v, str) and v != "", timeout=12)
            if ph:
                out["phone"] = _phone_num(ph)
    return out


def _targets(conn, args) -> list:
    if args.ids:
        rows = [db.get(conn, i) for i in args.ids]
        return [r for r in rows if r]
    q = "SELECT * FROM listings WHERE source='craigslist' AND status='vetted'"
    if not args.force:
        q += " AND (reply_email IS NULL AND contact_fetched_at IS NULL)"
    return conn.execute(q + " ORDER BY first_seen_at DESC").fetchall()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="specific listing ids")
    ap.add_argument("--all-vetted", action="store_true", help="all vetted CL listings")
    ap.add_argument("--force", action="store_true", help="re-fetch even if already fetched")
    ap.add_argument("--delay", type=int, default=12, help="seconds between listings (throttle guard)")
    args = ap.parse_args()
    if not args.ids and not args.all_vetted:
        ap.error("give listing ids or --all-vetted")

    # chromerpc reachable?
    if "_err" in _call("cdp.runtime.RuntimeService/Evaluate", {"expression": "1", "return_by_value": True}):
        raise SystemExit("chromerpc not reachable on " + GRPC +
                         " — start it: cd chromerpc && ./bin/chromerpc -headless -addr :50051 &")

    conn = db.connect()
    targets = _targets(conn, args)
    print(f"{len(targets)} listing(s) to fetch contact for.\n")
    done = 0
    for i, row in enumerate(targets):
        pid, url = row["id"], row["url"]
        print(f"[{i+1}/{len(targets)}] {pid}  {url}")
        try:
            res = fetch_contact(url)
        except Exception as e:
            res = {"ok": False, "note": f"error: {e}"}
        if res.get("ok"):
            conn.execute(
                "UPDATE listings SET reply_email=?, phone=COALESCE(?,phone), contact_fetched_at=? WHERE id=?",
                (res.get("email"), res.get("phone"), db.now(), pid))
            conn.commit()
            done += 1
            print(f"    options={res.get('options')} email={res.get('email')} phone={res.get('phone')}")
        else:
            print(f"    SKIP: {res.get('note')}")
        if i < len(targets) - 1:
            time.sleep(args.delay)
    conn.close()
    print(f"\nDone. Fetched contact for {done}/{len(targets)}. "
          f"Re-run sync_supabase.py to publish to the dashboard.")


if __name__ == "__main__":
    main()
