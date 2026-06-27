"""Fetch Craigslist reply-contact details (relay email + phone) for listings, via
the LOCAL chromerpc service using raw CDP + human-like mouse movement.

Craigslist hides contact behind the JS 'reply' button, so we drive a real browser:
navigate -> human-Bezier-click 'reply' -> enumerate the reply-option-header buttons
from the DOM -> human-click each -> read the revealed mailto / tel. Coordinates come
from the DOM bbox (the buttons shift with viewport width, so fixed pixels don't work),
but the CLICK is a human-like Bezier move + press/release. The reply panel loads
ASYNC so we poll for it. Results are stored on the listing row (reply_email + phone).

ALSO handles the OTHER pattern: some posts hide a phone/email in the BODY behind a
'click to reveal contact' / blocked section (unstructured, varies per post). After
the reply panel, reveal_in_body() scans the posting body for a clickable reveal
trigger, human-clicks it, waits a couple seconds, and reads the number that appears.
Best-effort and self-skipping (no trigger found -> no clicks), so it's safe to always
run; it only acts when the body actually gates the contact behind a click.

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
import re
import subprocess
import sys
import time

# contact_details is ONLY worth storing when there's a special step beyond the bare
# number — a masked-relay extension/code ("x 46", "ext 7852", "text 46 to ..."). A
# plain number lives in `phone`; anything else captured (marketing sentences, the
# post body) is noise and must NOT be stored.
_EXT_RE = re.compile(r"\b(?:ext\.?|x)\s*\d{1,5}\b|\btext\s+\d{1,5}\s+to\b", re.I)

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
CONTACT_NAME = ("(function(){var e=document.querySelector('.reply-contact-name');if(!e)return '';"
                "var t=e.textContent.replace(/\\s+/g,' ').trim();"
                "var m=t.match(/contact name\\s*:\\s*(.+)/i);return m?m[1].trim():'';})()")
# True when CL hasn't issued the reply token yet: the reply/contact data-href still
# holds the literal __SERVICE_ID__ placeholder (JS swaps in a real id once CL grants
# it). Seeing this after a click = the token was withheld, i.e. IP-throttled.
REPLY_UNINIT = ("(function(){var e=document.querySelector('button.reply-button[data-href],a.show-contact[data-href]');"
                "return !!(e&&/__SERVICE_ID__/.test(e.getAttribute('data-href')||''));})()")
MAILTO = "(function(){var a=document.querySelector('a[href^=\"mailto:\"]');return a?a.getAttribute('href'):'';})()"
PHONE = ("(function(){var t=document.querySelector('a[href^=\"tel:\"]');if(t)return t.getAttribute('href');"
         "var p=document.querySelector('.reply-content,.reply-info,[class*=reply]');var s=p?p.innerText:'';"
         "var m=s.match(/\\(\\d{3}\\)\\s*\\d{3}[-.\\s]?\\d{4}|\\d{3}[-.\\s]\\d{3}[-.\\s]\\d{4}/);return m?m[0]:'';})()")

# ---- in-body "click to reveal contact" (unstructured; some posts hide a phone in
# the BODY behind a JS click, separate from the reply panel) ---------------------
# Shared element filter: clickable nodes in the posting body whose visible text reads
# like a contact-reveal trigger ("show/click/tap ... phone/number/contact/email").
# Skips real external-navigation anchors so a click can't sail off the post.
_REVEAL_FILTER = (
    "var b=document.querySelector('#postingbody,section#postingbody')||document.body;"
    "var re=/(show|reveal|click|tap|see|view|get|press|unlock|here)[^.]{0,30}"
    "(contact|phone|number|email|call|text|reach|info|details)|"
    "(contact|phone|number|email)[^.]{0,20}(here|below|info|details|hidden|blocked)/i;"
    "var els=[...b.querySelectorAll('button,a,span,div,strong,u,em,[onclick],[role=button]')];"
    "var out=[];for(var i=0;i<els.length;i++){var e=els[i];"
    "var t=(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim();"
    "if(!t||t.length>80||!re.test(t))continue;"
    "if(e.tagName==='A'){var h=e.getAttribute('href')||'';if(/^https?:/i.test(h))continue;}"
    "var r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)continue;out.push(e);}"
)
REVEAL_LIST = ("(function(){%s return JSON.stringify(out.slice(0,4).map(function(e){"
               "return (e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim().slice(0,60);}));})()"
               % _REVEAL_FILTER)
def REVEAL_POS(n):
    return ("(function(){%s var e=out[%d];if(!e)return '';e.scrollIntoView({block:'center'});"
            "var r=e.getBoundingClientRect();"
            "return JSON.stringify({cx:Math.round(r.x+r.width/2),cy:Math.round(r.y+r.height/2)});})()"
            % (_REVEAL_FILTER, n))
BODY_CONTACTS = (
    "(function(){var b=document.body;"   # whole page: a reveal may surface outside #postingbody
    "var txt=b.innerText||'';"
    "var ph=(txt.match(/\\(\\d{3}\\)\\s*\\d{3}[-.\\s]?\\d{4}|\\b\\d{3}[-.\\s]\\d{3}[-.\\s]\\d{4}\\b/g)||[]);"
    "var tel=[...b.querySelectorAll('a[href^=\"tel:\"]')].map(function(a){return a.getAttribute('href').slice(4);});"
    "var em=(txt.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/g)||[]);"
    "var mail=[...b.querySelectorAll('a[href^=\"mailto:\"]')].map(function(a){return a.getAttribute('href').slice(7).split('?')[0];});"
    "return JSON.stringify({phones:[...new Set(ph.concat(tel))],emails:[...new Set(em.concat(mail))]});})()"
)
# Verbatim revealed contact instructions — capture SEMANTICALLY (the actual call/text
# lines with their extension/code), not just a bare phone. The masked relay needs the
# extra ("Call (415) 943-0693 x 46" / "Text 46 to (415) 943-0693") to be usable, and
# the exact wording varies, so we store it as-is for the dashboard.
CONTACT_BLOCK = (
    "(function(){var lines=(document.body.innerText||'').split(/\\n+/)"
    ".map(function(s){return s.trim();}).filter(Boolean);"
    # keep CALL/TEXT instruction lines or any line with a phone number; DROP the
    # email reply-panel chrome (webmail providers / 'default mail app' / copy button)
    "var hit=lines.filter(function(s){"
    "if(/webmail|gmail|yahoo|hotmail|outlook|live mail|aol|default mail app|^copy$|reply using/i.test(s))return false;"
    "return (/\\b(call|text)\\b/i.test(s)&&/\\d{3}/.test(s))||"
    "/\\(?\\d{3}\\)?[-.\\s]?\\d{3}[-.\\s]\\d{4}/.test(s);});"
    "return hit.slice(0,4).join('\\n').replace(/[ \\t]+/g,' ').trim().slice(0,300);})()"
)


def _grab_details(out: dict) -> None:
    """Capture the call/text instructions into out['details'] ONLY when they carry a
    real special step (a masked-relay extension/code). A plain number is left to the
    `phone` field — we don't store marketing sentences or the post body."""
    if out.get("details"):
        return
    cb = ev(CONTACT_BLOCK)
    if isinstance(cb, str) and _EXT_RE.search(cb):
        # keep just the line(s) bearing the number/code, trimmed of surrounding prose
        line = next((ln.strip() for ln in cb.splitlines() if _EXT_RE.search(ln)), cb.strip())
        out["details"] = line[:160]


def _new_contact(bc, before_ph, before_em) -> bool:
    return isinstance(bc, dict) and (
        any(p not in before_ph for p in (bc.get("phones") or []))
        or any(e not in before_em for e in (bc.get("emails") or [])))


def _read_reply_panel(out: dict) -> bool:
    """If the reply panel is (or becomes) open, enumerate its option headers
    (email/call/text), human-click each, and read the revealed mailto/tel into `out`.
    Returns True if any options were found. Used after the reply button AND after an
    in-body reveal (a 'show contact info' trigger sometimes opens this same panel)."""
    got = _poll(lambda: ev(OPTION_NAMES), lambda v: isinstance(v, list) and len(v) > 0, timeout=14)
    if not (isinstance(got, list) and got):
        return False
    time.sleep(3)
    names = ev(OPTION_NAMES) or got
    out["options"] = names
    if not out.get("name"):
        out["name"] = ev(CONTACT_NAME) or None
    for name in names:
        pos = ev(OPT_POS(name))
        if not isinstance(pos, dict):
            continue
        human_click((pos["cx"], pos["cy"]), start=(pos["cx"] + 120, pos["cy"] - 30))
        if "email" in name:
            mt = _poll(lambda: ev(MAILTO), lambda v: isinstance(v, str) and v.startswith("mailto"), timeout=12)
            if mt:
                out["email"] = _email_addr(mt)
        else:  # call / text
            ph = _poll(lambda: ev(PHONE), lambda v: isinstance(v, str) and v != "", timeout=12)
            if ph:
                out["phone"] = _phone_num(ph)
            _grab_details(out)   # verbatim "Call ... x46 / Text 46 to ..." wording
    return True


def reveal_in_body(out: dict) -> list:
    """If the post BODY gates contact behind a 'click to reveal' element, click it
    (human-like) and capture what it surfaces — which can be EITHER an inline
    phone/email in the page text OR the standard reply panel opening. Mutates `out`
    (fills phone/email if still empty). Best-effort: no candidates -> no clicks.
    Returns the list of trigger labels it clicked."""
    names = ev(REVEAL_LIST)
    if not (isinstance(names, list) and names):
        return []
    before = ev(BODY_CONTACTS) or {}
    before_ph = set(before.get("phones") or [])
    before_em = set(before.get("emails") or [])
    clicked = []
    for i in range(len(names)):
        pos = ev(REVEAL_POS(i))
        if not isinstance(pos, dict):
            continue
        time.sleep(random.uniform(0.4, 1.0))
        human_click((pos["cx"], pos["cy"]), start=(pos["cx"] + 110, pos["cy"] - 40))
        clicked.append(names[i])
        # The reveal is ASYNC (AJAX) — POLL up to ~12s for a NEW phone/email to appear
        # inline (the common case). A fixed sleep was too short and missed it.
        _poll(lambda: ev(BODY_CONTACTS),
              lambda bc: _new_contact(bc, before_ph, before_em), timeout=12, every=1.0)
        # If nothing inlined, the reveal may instead have opened the reply panel.
        if not _new_contact(ev(BODY_CONTACTS), before_ph, before_em) \
                and not (out.get("email") or out.get("phone")):
            _read_reply_panel(out)
    # Pick up any phone/email that newly appeared anywhere on the page + verbatim text.
    after = ev(BODY_CONTACTS) or {}
    if not out.get("phone"):
        new_ph = [p for p in (after.get("phones") or []) if p not in before_ph]
        if new_ph:
            out["phone"] = _phone_num(new_ph[0])
    if not out.get("email"):
        new_em = [e for e in (after.get("emails") or []) if e not in before_em]
        if new_em:
            out["email"] = new_em[0]
    _grab_details(out)
    return clicked


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
    """Drive chromerpc to reveal + read the contact for one listing — the standard
    reply panel AND any in-body 'click to reveal contact' widget.
    Returns {email, phone, options, name, ok, note, body_reveal}."""
    _call("cdp.emulation.EmulationService/ClearDeviceMetricsOverride", {})
    navigate(url)
    time.sleep(random.uniform(2.5, 4.0))
    warmup()                       # idle wander + scroll, like a human reading first
    out = {"ok": False, "options": None, "name": None, "email": None, "phone": None,
           "details": None}

    # 1) Standard reply panel (the common case).
    rb = ev(REPLY)
    if isinstance(rb, dict):
        time.sleep(random.uniform(0.4, 1.1))
        human_click((rb["cx"], rb["cy"]))
        if _read_reply_panel(out):
            out["ok"] = True

    # 2) In-body click-to-reveal contact (best-effort; no-ops if the body has none).
    #    Handles a 'show contact info' widget that either inlines the number or opens
    #    the reply panel. Skipped only if we already have both email AND phone.
    if not (out.get("email") and out.get("phone")):
        try:
            clicked = reveal_in_body(out)
            if clicked:
                out["body_reveal"] = clicked
                if out.get("email") or out.get("phone"):
                    out["ok"] = True
        except Exception as e:
            out.setdefault("note", f"body-reveal error: {e}")

    if not out["ok"]:
        if ev(REPLY_UNINIT) is True:
            out["note"] = ("reply token not issued (__SERVICE_ID__ unresolved) — IP-throttled; "
                           "retry later, spaced out (one pass per listing)")
        else:
            out["note"] = "no reply panel + no in-body contact (taken down / blocked)"
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
                "UPDATE listings SET reply_email=?, phone=COALESCE(?,phone), "
                "contact_name=?, contact_details=COALESCE(?,contact_details), "
                "contact_fetched_at=? WHERE id=?",
                (res.get("email"), res.get("phone"), res.get("name"),
                 res.get("details"), db.now(), pid))
            conn.commit()
            done += 1
            br = f" body-reveal={res['body_reveal']}" if res.get("body_reveal") else ""
            det = f"\n    details={res['details']!r}" if res.get("details") else ""
            print(f"    options={res.get('options')} name={res.get('name')} "
                  f"email={res.get('email')} phone={res.get('phone')}{br}{det}")
        else:
            print(f"    SKIP: {res.get('note')}")
        if i < len(targets) - 1:
            time.sleep(args.delay)
    conn.close()
    print(f"\nDone. Fetched contact for {done}/{len(targets)}. "
          f"Re-run sync_supabase.py to publish to the dashboard.")


if __name__ == "__main__":
    main()
