"""Fetch Craigslist reply-contact details (relay email + phone) for listings, via
the LOCAL chromerpc service — HUMAN interaction only, NO page JavaScript.

Per THUMB_RULES: we never execute page scripts (Runtime.Evaluate). We drive the
browser like a person — real Input mouse moves/clicks (human Bézier paths) and
wheel scrolling — and we LOCATE elements + READ content through the CDP DOM domain
(GetDocument -> QuerySelector(All) -> GetBoxModel for click coords; GetOuterHTML /
GetAttributes to read), parsing the HTML in Python. Craigslist hides contact behind
the JS 'reply' button, so: navigate -> human-click 'reply' -> enumerate the
reply-option-header buttons -> human-click each -> read the revealed mailto/tel.
The reply panel loads ASYNC so we poll. Results are stored on the listing row.

ALSO handles the in-body 'click to reveal contact' pattern: some posts hide a
phone/email in the body behind a clickable trigger; reveal_in_body() finds it (by
its visible text), human-clicks it, and reads what surfaces.

IMPORTANT: ONE pass per listing. Craigslist throttles repeated reply requests from
one IP. We skip already-fetched listings (unless --force) and space requests out.

Prereq — run chromerpc locally first (HEADFUL recommended):
    cd chromerpc && ./bin/chromerpc -headless=false -addr :50051 &

    python3 scripts/fetch_cl_contacts.py --all-vetted
    python3 scripts/fetch_cl_contacts.py 7942959383 7942...
    python3 scripts/fetch_cl_contacts.py --all-vetted --force --delay 15
"""
from __future__ import annotations

import argparse
import html as _html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time

# contact_details is ONLY worth storing when there's a special step beyond the bare
# number — a masked-relay extension/code ("x 46", "ext 7852", "text 46 to ..."). A
# plain number lives in `phone`; anything else captured is noise and must NOT be stored.
_EXT_RE = re.compile(r"\b(?:ext\.?|x)\s*\d{1,5}\b|\btext\s+\d{1,5}\s+to\b", re.I)

import db

GRPC = "localhost:50051"


def _grpcurl_bin() -> str:
    """Resolve the grpcurl executable. PATH first, then GRPCURL_BIN / GOPATH-bin
    fallbacks (Windows subprocess doesn't always inherit a shell-mangled PATH)."""
    g = shutil.which("grpcurl")
    if g:
        return g
    for c in (os.getenv("GRPCURL_BIN"),
              os.path.expandvars(r"%USERPROFILE%\go\bin\grpcurl.exe"),
              os.path.expanduser("~/go/bin/grpcurl.exe"),
              os.path.expanduser("~/go/bin/grpcurl")):
        if c and os.path.exists(c):
            return c
    return "grpcurl"


_GRPCURL = _grpcurl_bin()


# ---- chromerpc raw-CDP helpers -------------------------------------------------
def _call(method: str, payload: dict) -> dict:
    cmd = [_GRPCURL, "-plaintext", "-max-time", "40", "-d", json.dumps(payload), GRPC, method]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"_err": (r.stderr or r.stdout)[:160]}


def navigate(url: str):
    _call("cdp.page.PageService/Navigate", {"url": url})


# ---- human input (real mouse, no JS) -------------------------------------------
def _mouse(t, x, y, **kw):
    p = {"type": t, "x": x, "y": y}; p.update(kw)
    _call("cdp.input.InputService/DispatchMouseEvent", p)


def _smooth(t):  # smootherstep -> slow start, fast middle, slow end (ease-in-out)
    return t * t * t * (t * (t * 6 - 15) + 10)


def _bezier(S, E, n=None):
    """Human-ish cursor path: a bowed cubic Bézier, ease-in-out timing, small jitter."""
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
    over = (E[0] + random.uniform(-7, 7), E[1] + random.uniform(-6, 6))
    _bezier(start, over)
    time.sleep(random.uniform(0.04, 0.11))
    _bezier(over, E, n=5)
    time.sleep(random.uniform(0.13, 0.30))
    _mouse("mouseMoved", E[0], E[1])
    _mouse("mousePressed", E[0], E[1], button="left", buttons=1, click_count=1)
    time.sleep(random.uniform(0.05, 0.13))
    _mouse("mouseReleased", E[0], E[1], button="left", buttons=0, click_count=1)


def warmup():
    """Look like a human reading: idle cursor wanders + real wheel scroll down/up."""
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


# ---- CDP DOM reads (no JS) -----------------------------------------------------
# grpcurl emits proto responses in camelCase (nodeId/nodeIds/outerHtml); accept both.
def _g(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return None


def _doc_root():
    r = _call("cdp.dom.DOMService/GetDocument", {"depth": 0})
    return _g(r.get("root") or {}, "nodeId", "node_id")


def _qs(selector: str, root=None):
    root = root or _doc_root()
    if not root:
        return None
    r = _call("cdp.dom.DOMService/QuerySelector", {"node_id": root, "selector": selector})
    return _g(r, "nodeId", "node_id")


def _qsa(selector: str, root=None):
    root = root or _doc_root()
    if not root:
        return []
    r = _call("cdp.dom.DOMService/QuerySelectorAll", {"node_id": root, "selector": selector})
    return _g(r, "nodeIds", "node_ids") or []


def _center(node_id):
    if not node_id:
        return None
    r = _call("cdp.dom.DOMService/GetBoxModel", {"node_id": node_id})
    q = ((r.get("model") or {}).get("content")) or []
    if len(q) < 8:
        return None
    return {"cx": round((q[0]+q[2]+q[4]+q[6])/4), "cy": round((q[1]+q[3]+q[5]+q[7])/4)}


def _outer(node_id):
    if not node_id:
        return ""
    r = _call("cdp.dom.DOMService/GetOuterHTML", {"node_id": node_id})
    return _g(r, "outerHtml", "outer_html") or ""


def _attrs(node_id):
    r = _call("cdp.dom.DOMService/GetAttributes", {"node_id": node_id})
    a = r.get("attributes") or []
    return {a[i]: a[i+1] for i in range(0, len(a) - 1, 2)}


def _html_lines(h: str) -> list:
    """Rendered-ish text lines from raw HTML (block tags -> newlines), no JS."""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h\d|/tr|/section)\s*/?>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return [re.sub(r"[ \t]+", " ", _html.unescape(ln)).strip() for ln in h.split("\n")]


def _node_text(node_id) -> str:
    return re.sub(r"\s+", " ", " ".join(_html_lines(_outer(node_id)))).strip()


def _page_lines() -> list:
    return [ln for ln in _html_lines(_outer(_doc_root())) if ln]


def _page_text() -> str:
    return " ".join(_page_lines())


# ---- contact extraction (CDP DOM + Python parsing) -----------------------------
_PHONE_PAT = r"\(\d{3}\)\s*\d{3}[-.\s]?\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"
_EMAIL_PAT = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
_REVEAL_RE = re.compile(
    r"(show|reveal|click|tap|see|view|get|press|unlock|here)[^.]{0,30}"
    r"(contact|phone|number|email|call|text|reach|info|details)|"
    r"(contact|phone|number|email)[^.]{0,20}(here|below|info|details|hidden|blocked)", re.I)


def _reply_button_center():
    """Center of the 'reply' button (real coords from the box model)."""
    nid = _qs("button.reply-button")
    if nid:
        c = _center(nid)
        if c:
            return c
    for nid in _qsa("button, a"):
        if _node_text(nid).strip().lower() == "reply":
            c = _center(nid)
            if c:
                return c
    return None


def _reply_options():
    """[(name, center)] for each reply-option-header (email / call / text)."""
    out = []
    for nid in _qsa("button.reply-option-header"):
        name = _node_text(nid).lower()
        c = _center(nid)
        if name and c:
            out.append((name, c))
    return out


def _contact_name():
    nid = _qs(".reply-contact-name")
    if not nid:
        return None
    m = re.search(r"contact name\s*:\s*(.+)", _node_text(nid), re.I)
    return (m.group(1).strip() if m else None) or None


def _mailto():
    nid = _qs('a[href^="mailto:"]')
    return _attrs(nid).get("href", "") if nid else ""


def _tel_or_phone():
    nid = _qs('a[href^="tel:"]')
    if nid:
        h = _attrs(nid).get("href", "")
        if h:
            return h
    m = re.search(_PHONE_PAT, _page_text())
    return m.group(0) if m else ""


def _reply_uninit() -> bool:
    """True when CL withheld the reply token (data-href still holds __SERVICE_ID__)."""
    for sel in ("button.reply-button[data-href]", "a.show-contact[data-href]"):
        nid = _qs(sel)
        if nid and "__SERVICE_ID__" in (_attrs(nid).get("data-href", "") or ""):
            return True
    return False


def _body_contacts() -> dict:
    txt = _page_text()
    phones = set(re.findall(_PHONE_PAT, txt))
    emails = set(re.findall(_EMAIL_PAT, txt))
    for nid in _qsa('a[href^="tel:"]'):
        phones.add(_attrs(nid).get("href", "")[4:])
    for nid in _qsa('a[href^="mailto:"]'):
        emails.add(_attrs(nid).get("href", "")[7:].split("?")[0])
    return {"phones": [p for p in phones if p], "emails": [e for e in emails if e]}


def _contact_block() -> str:
    """Verbatim revealed call/text instruction lines (with extension/code), dropping
    the email reply-panel chrome."""
    hit = []
    for s in _page_lines():
        if re.search(r"webmail|gmail|yahoo|hotmail|outlook|live mail|aol|default mail app|^copy$|reply using", s, re.I):
            continue
        if (re.search(r"\b(call|text)\b", s, re.I) and re.search(r"\d{3}", s)) or re.search(_PHONE_PAT, s):
            hit.append(s)
    return "\n".join(hit[:4]).strip()[:300]


def _reveal_triggers():
    """[(label, center)] for in-body 'click to reveal contact' triggers (by text)."""
    body = _qs("#postingbody") or _qs("section#postingbody") or _doc_root()
    out = []
    for nid in _qsa("button, a, [onclick], [role=button]", root=body):
        t = _node_text(nid)
        if not t or len(t) > 80 or not _REVEAL_RE.search(t):
            continue
        if (_attrs(nid).get("href", "") or "").lower().startswith(("http://", "https://")):
            continue
        c = _center(nid)
        if c:
            out.append((t[:60], c))
        if len(out) >= 4:
            break
    return out


# ---- orchestration -------------------------------------------------------------
def _email_addr(mailto):
    if not mailto:
        return None
    m = mailto[len("mailto:"):] if mailto.startswith("mailto:") else mailto
    return m.split("?", 1)[0] or None


def _phone_num(v):
    if not v:
        return None
    return v[len("tel:"):] if v.startswith("tel:") else v


def _grab_details(out: dict) -> None:
    if out.get("details"):
        return
    cb = _contact_block()
    if cb and _EXT_RE.search(cb):
        line = next((ln.strip() for ln in cb.splitlines() if _EXT_RE.search(ln)), cb.strip())
        out["details"] = line[:160]


def _new_contact(bc, before_ph, before_em) -> bool:
    return isinstance(bc, dict) and (
        any(p not in before_ph for p in (bc.get("phones") or []))
        or any(e not in before_em for e in (bc.get("emails") or [])))


def _read_reply_panel(out: dict) -> bool:
    """If the reply panel is (or becomes) open, enumerate its option headers, human-
    click each, and read the revealed mailto/tel. Returns True if options were found."""
    opts = _poll(_reply_options, lambda v: bool(v), timeout=14)
    if not opts:
        return False
    time.sleep(3)
    opts = _reply_options() or opts
    out["options"] = [n for n, _ in opts]
    if not out.get("name"):
        out["name"] = _contact_name()
    for name in list(out["options"]):
        cur = {n: c for n, c in _reply_options()}              # fresh coords per click
        c = cur.get(name)
        if not c:
            continue
        human_click((c["cx"], c["cy"]), start=(c["cx"] + 120, c["cy"] - 30))
        if "email" in name:
            mt = _poll(_mailto, lambda v: isinstance(v, str) and v.startswith("mailto"), timeout=12)
            if mt:
                out["email"] = _email_addr(mt)
        else:  # call / text
            ph = _poll(_tel_or_phone, lambda v: isinstance(v, str) and v != "", timeout=12)
            if ph:
                out["phone"] = _phone_num(ph)
            _grab_details(out)
    return True


def reveal_in_body(out: dict) -> list:
    """Click any in-body 'reveal contact' trigger (human-like) and capture what it
    surfaces (inline phone/email OR the reply panel). Best-effort."""
    triggers = _reveal_triggers()
    if not triggers:
        return []
    before = _body_contacts()
    before_ph = set(before.get("phones") or [])
    before_em = set(before.get("emails") or [])
    clicked = []
    for label, c in triggers:
        time.sleep(random.uniform(0.4, 1.0))
        human_click((c["cx"], c["cy"]), start=(c["cx"] + 110, c["cy"] - 40))
        clicked.append(label)
        _poll(_body_contacts, lambda bc: _new_contact(bc, before_ph, before_em), timeout=12, every=1.0)
        if not _new_contact(_body_contacts(), before_ph, before_em) \
                and not (out.get("email") or out.get("phone")):
            _read_reply_panel(out)
    after = _body_contacts()
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


def fetch_contact(url: str) -> dict:
    """Drive chromerpc (human input + CDP DOM reads, no JS) to reveal + read the
    contact for one listing. Returns {email, phone, options, name, ok, note, ...}."""
    _call("cdp.emulation.EmulationService/ClearDeviceMetricsOverride", {})
    navigate(url)
    time.sleep(random.uniform(2.5, 4.0))
    warmup()                       # human idle wander + scroll first
    out = {"ok": False, "options": None, "name": None, "email": None, "phone": None,
           "details": None}

    # 1) Standard reply panel (the common case).
    rb = _reply_button_center()
    if rb:
        time.sleep(random.uniform(0.4, 1.1))
        human_click((rb["cx"], rb["cy"]))
        if _read_reply_panel(out):
            out["ok"] = True

    # 2) In-body click-to-reveal contact (best-effort).
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
        if _reply_uninit():
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

    # chromerpc reachable? (CDP DOM call, no JS)
    if "_err" in _call("cdp.dom.DOMService/GetDocument", {"depth": 0}):
        raise SystemExit("chromerpc not reachable on " + GRPC +
                         " — start it: cd chromerpc && ./bin/chromerpc -headless=false -addr :50051 &")

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
