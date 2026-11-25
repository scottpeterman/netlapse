#!/usr/bin/env python3
"""
netlapse_reparse.py — reparse a capture type one device at a time through
POST /api/v1/parse-audit/reparse, with progress and ETA.

A single all-devices reparse call can run for minutes on large route tables
and outlive a client timeout. Calling it per device shows progress, and one
slow device can't time out the whole run.

Dry run by default (writes nothing). Add --persist to write the new parses.

Usage:
    export NETLAPSE_USER=admin
    export NETLAPSE_PASS=...
    python netlapse_reparse.py --capture-type routes
    python netlapse_reparse.py --capture-type routes --site iad1 --platform arista_eos
    python netlapse_reparse.py --capture-type routes --persist
"""

import argparse
import getpass
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request(opener, method, url, body=None, token=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except TimeoutError:
        return 0, "client timeout"
    except urllib.error.URLError as e:
        print(f"connection error: {e.reason}", file=sys.stderr)
        sys.exit(2)


def fmt_secs(s):
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def main():
    ap = argparse.ArgumentParser(description="Per-device Netlapse reparse with progress")
    ap.add_argument("--base", default=os.environ.get("NETLAPSE_BASE", "http://localhost:8888"))
    ap.add_argument("-u", "--user", default=os.environ.get("NETLAPSE_USER"))
    ap.add_argument("-p", "--password", default=os.environ.get("NETLAPSE_PASS"))
    ap.add_argument("--token", help="API token (Bearer) — skips session login")
    ap.add_argument("--capture-type", default="routes")
    ap.add_argument("--site", help="site slug filter")
    ap.add_argument("--platform", help="platform slug filter, e.g. arista_eos, juniper_junos")
    ap.add_argument("--persist", action="store_true", help="write the new parses (default: dry run)")
    ap.add_argument("--timeout", type=int, default=600, help="per-device timeout, seconds")
    ap.add_argument("--out", help="write all per-device results to this JSON file")
    args = ap.parse_args()

    opener = make_opener()
    if not args.token:
        user = args.user or input("username: ")
        password = args.password or getpass.getpass("password: ")
        code, body = request(opener, "POST", f"{args.base}/api/v1/auth/login",
                             body={"username": user, "password": password}, timeout=15)
        if code != 200:
            print(f"login failed (HTTP {code}): {body}", file=sys.stderr)
            sys.exit(1)

    q = {}
    if args.site:
        q["site"] = args.site
    if args.platform:
        q["platform"] = args.platform
    url = f"{args.base}/api/v1/devices" + (f"?{urllib.parse.urlencode(q)}" if q else "")
    code, body = request(opener, "GET", url, token=args.token, timeout=60)
    if code != 200:
        print(f"device list failed (HTTP {code}): {body[:200]}", file=sys.stderr)
        sys.exit(1)
    devices = [d for d in json.loads(body) if d.get("collection_enabled", True)]
    devices.sort(key=lambda d: (d.get("site_slug") or "", d["name"]))

    total = len(devices)
    mode = "PERSIST" if args.persist else "dry run"
    print(f"reparse {args.capture_type} ({mode}) — {total} devices")
    print(f"{'device':28} {'delta':10} {'old tmpl':34} {'old#':>6} {'new tmpl':34} {'new#':>6}")

    summary = {}
    results = []
    start = time.monotonic()
    for idx, d in enumerate(devices, 1):
        body = {"capture_type": args.capture_type, "device_ids": [d["id"]], "persist": args.persist}
        code, resp = request(opener, "POST", f"{args.base}/api/v1/parse-audit/reparse",
                             body=body, token=args.token, timeout=args.timeout)
        elapsed = time.monotonic() - start
        eta = elapsed / idx * (total - idx)
        progress = f"[{idx}/{total} eta {fmt_secs(eta)}]"

        if code != 200:
            summary["error"] = summary.get("error", 0) + 1
            print(f"{d['name'][:28]:28} {'ERROR':10} HTTP {code} {resp[:80]}  {progress}")
            continue

        data = json.loads(resp)
        for k, v in (data.get("summary") or {}).items():
            if v:
                summary[k] = summary.get(k, 0) + v
        rows = data.get("results") or []
        if not rows:
            print(f"{d['name'][:28]:28} {'skipped':10} (no stored {args.capture_type} capture)  {progress}")
            continue
        for r in rows:
            results.append(r)
            old, new = r.get("old") or {}, r.get("new") or {}
            print(f"{d['name'][:28]:28} {r.get('delta', ''):10} "
                  f"{(old.get('template') or '-')[:34]:34} {old.get('records', 0):>6} "
                  f"{(new.get('template') or '-')[:34]:34} {new.get('records', 0):>6}  {progress}")

    print()
    print(f"done in {fmt_secs(time.monotonic() - start)} — " + "  ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
