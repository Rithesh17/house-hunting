"""Ad-hoc chromerpc driver: navigate / evaluate JS / screenshot the local headful
Chrome via grpcurl. For exploring bot-protected sites (Zillow, Apartments.com).

    py tools/cr.py nav "https://..."            # navigate (returns final url+title)
    py tools/cr.py eval "document.title"         # eval JS, print JSON value
    py tools/cr.py shot data/_shot.png           # full-page PNG screenshot
    py tools/cr.py shotfull data/_shot.png       # capture_beyond_viewport (tall) PNG

Prereq: chromerpc on :50051 (headful). grpcurl on PATH or at GOPATH/bin.
"""
from __future__ import annotations
import base64, json, os, shutil, subprocess, sys

GRPC = "localhost:50051"

def _grpcurl() -> str:
    g = shutil.which("grpcurl")
    if g:
        return g
    for c in (os.path.expandvars(r"%USERPROFILE%\go\bin\grpcurl.exe"),
              os.path.expanduser("~/go/bin/grpcurl.exe")):
        if os.path.exists(c):
            return c
    return "grpcurl"

GC = _grpcurl()

def call(method: str, payload: dict, max_time: int = 60) -> dict:
    r = subprocess.run([GC, "-plaintext", "-max-time", str(max_time), "-d",
                        json.dumps(payload), GRPC, method],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"_err": (r.stderr or r.stdout)[:300]}

def _parse(v):
    for _ in range(3):
        if not isinstance(v, str):
            return v
        try:
            v = json.loads(v)
        except Exception:
            return v
    return v

def ev(expr: str, await_promise: bool = False):
    r = call("cdp.runtime.RuntimeService/Evaluate",
             {"expression": expr, "return_by_value": True, "await_promise": await_promise})
    if "_err" in r:
        return r
    return _parse((r.get("result") or {}).get("value"))

def nav(url: str):
    call("cdp.page.PageService/Navigate", {"url": url})

def shot(path: str, beyond: bool = False):
    r = call("cdp.page.PageService/CaptureScreenshot",
             {"format": "SCREENSHOT_FORMAT_PNG", "capture_beyond_viewport": beyond})
    data = r.get("data")
    if not data:
        print("screenshot failed:", r); return False
    open(path, "wb").write(base64.b64decode(data))
    print("saved", path, len(base64.b64decode(data)), "bytes")
    return True

def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd = sys.argv[1]
    if cmd == "nav":
        nav(sys.argv[2])
        import time; time.sleep(float(sys.argv[3]) if len(sys.argv) > 3 else 4)
        info = ev("JSON.stringify({url:location.href,title:document.title,ready:document.readyState})")
        print(info)
    elif cmd == "eval":
        out = ev(sys.argv[2], await_promise=("--await" in sys.argv))
        sys.stdout.buffer.write((out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)).encode("utf-8"))
        print()
    elif cmd == "shot":
        shot(sys.argv[2])
    elif cmd == "shotfull":
        shot(sys.argv[2], beyond=True)
    else:
        print("unknown cmd", cmd)

if __name__ == "__main__":
    main()
