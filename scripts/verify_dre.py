"""Verify a California DRE (real-estate) license against the FREE public lookup.

The DRE public license page is a plain GET — no key, no auth:
    https://www2.dre.ca.gov/publicasp/pplinfo.asp?License_id=<8 digits>

We FETCH the licensed name / type / status / expiration / employing broker /
disciplinary history. We do NOT judge here — the vetting subagent semantically
compares the licensed name to the name the lister gave (a real # under a
different name = a stolen license = a scam flag). Absence of a DRE # is neutral
(small landlords / subletters don't have one); a fake/mismatched one is the flag.

    py scripts/verify_dre.py 01717299
    py scripts/verify_dre.py --name "Everest Mwamba"
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import requests
from bs4 import BeautifulSoup

BASE = "https://www2.dre.ca.gov/publicasp/pplinfo.asp"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_DISC_RE = re.compile(r"revoked|suspend|barred|surrender|disciplin|accusation|"
                      r"formal action|restricted", re.IGNORECASE)


def normalize_id(raw: str) -> str:
    """DRE ids are 8 digits, often written with leading zeros or spaces."""
    digits = re.sub(r"\D", "", raw or "")
    return digits.zfill(8) if digits else ""


_EXTRACT_RE = re.compile(
    r"(?:cal\s?dre|dre|d\.r\.e\.|license|lic\.?)\s*#?\s*:?\s*(\d{7,8})", re.IGNORECASE)


def extract_dre(text: str | None) -> list[str]:
    """All 8-digit DRE/license ids mentioned in a post body (agent + brokerage),
    de-duplicated, normalized to 8 digits. Empty list if none (the common case —
    small landlords / subletters have no license, which is fine)."""
    found = []
    for m in _EXTRACT_RE.finditer(text or ""):
        lid = normalize_id(m.group(1))
        if lid and lid not in found:
            found.append(lid)
    return found


def _is_value(ln: str) -> bool:
    """A real value line — not blank, not pure punctuation, not another label."""
    s = ln.strip()
    return bool(s) and not re.fullmatch(r"[\W_]+", s) and not s.endswith(":")


def _value_after(lines: list[str], label: str) -> str | None:
    """First real value line after the line whose text starts with `label`."""
    for i, ln in enumerate(lines):
        if ln.lower().startswith(label.lower()):
            for nxt in lines[i + 1:]:
                if _is_value(nxt):
                    return nxt.strip()
    return None


def lookup(license_id: str, *, timeout: int = 20) -> dict:
    """Return a dict of the license record, or {found: False}."""
    lid = normalize_id(license_id)
    if not lid:
        return {"found": False, "error": "no license id"}
    out = {"found": False, "license_id": lid}
    try:
        r = requests.get(BASE, params={"License_id": lid},
                         headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        out["error"] = f"fetch failed: {e}"
        return out
    lines = [ln.strip() for ln in BeautifulSoup(r.text, "html.parser")
             .get_text("\n").split("\n") if ln.strip()]
    name = _value_after(lines, "Name:")
    if not name or not any(ln.startswith("License Type") for ln in lines):
        return out  # no record for this id
    # Real disciplinary actions carry a date AND an action verb (or an all-caps
    # REVOKED/SUSPENDED token) — this excludes the page's boilerplate disclaimer.
    disc, seen = [], set()
    for ln in lines:
        hit = (re.search(r"\d\d?/\d\d?/\d\d", ln) and _DISC_RE.search(ln)) or \
              re.search(r"\b(REVOKED|SUSPENDED|BARRED|SURRENDERED)\b", ln)
        if hit and ln not in seen:
            seen.add(ln); disc.append(ln)
    out.update({
        "found": True,
        "name": name,
        "license_type": _value_after(lines, "License Type:"),
        "status": _value_after(lines, "License Status"),
        "expiration": _value_after(lines, "Expiration Date:"),
        "employing_broker": _value_after(lines, "Broker Associate for:")
                            or _value_after(lines, "Affiliated Licensed Corporation"),
        "has_disciplinary_history": bool(disc),
        "disciplinary": disc[:6],
    })
    return out


def search_by_name(name: str, *, timeout: int = 20) -> dict:
    """Best-effort name search (last name first works best). Returns raw text
    rows so the caller can eyeball matches; the DRE name form posts last/first."""
    try:
        r = requests.get(BASE, params={"License_id": "", "Name": name},
                         headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        txt = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        return {"query": name, "raw": txt[:1500]}
    except requests.exceptions.RequestException as e:
        return {"query": name, "error": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("license_id", nargs="?")
    ap.add_argument("--name")
    args = ap.parse_args()
    if args.name:
        print(json.dumps(search_by_name(args.name), indent=2))
    elif args.license_id:
        print(json.dumps(lookup(args.license_id), indent=2))
    else:
        ap.error("give a license id or --name")


if __name__ == "__main__":
    main()
