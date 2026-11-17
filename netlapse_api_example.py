#!/usr/bin/env python3
"""
netlapse_api.py — authenticated test client for the Netlapse API.

Logs in via POST /api/v1/auth/login (session-cookie auth), reuses the
'netlapse_session' cookie, then issues requests. Stdlib only — no pip deps.

Usage:
    # creds from env (recommended)
    export NETLAPSE_USER=admin
    export NETLAPSE_PASS=...

    # dump every route the RUNNING server has registered (the key diagnostic)
    python netlapse_api.py --routes

    # GET any path
    python netlapse_api.py /api/v1/jobs/capture-types
    python netlapse_api.py /api/v1/jobs

    # other methods / body
    python netlapse_api.py -X POST /api/v1/jobs/bgp-summary/run
    python netlapse_api.py -X PUT  /api/v1/jobs/arp-table --data '{"interval_seconds":600}'

    # creds / host as flags instead of env
    python netlapse_api.py -u admin -p secret --base http://localhost:8888 /api/v1/jobs
"""

import argparse
import getpass
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

COOKIE_NAME = "netlapse_session"


def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def request(opener, method, url, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        resp = opener.open(req, timeout=15)
        return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        print(f"connection error: {e.reason}", file=sys.stderr)
        sys.exit(2)


def login(opener, base, user, password):
    code, body = request(
        opener, "POST", f"{base}/api/v1/auth/login",
        body={"username": user, "password": password},
    )
    if code != 200:
        print(f"login failed (HTTP {code}): {body}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ logged in as {user}")


def pretty(body):
    try:
        return json.dumps(json.loads(body), indent=2)
    except Exception:
        return body


def main():
    ap = argparse.ArgumentParser(description="Authenticated Netlapse API client")
    ap.add_argument("path", nargs="?", help="API path, e.g. /api/v1/jobs/capture-types")
    ap.add_argument("--base", default=os.environ.get("NETLAPSE_BASE", "http://localhost:8888"))
    ap.add_argument("-u", "--user", default=os.environ.get("NETLAPSE_USER"))
    ap.add_argument("-p", "--password", default=os.environ.get("NETLAPSE_PASS"))
    ap.add_argument("-X", "--method", default="GET")
    ap.add_argument("--data", help="JSON request body")
    ap.add_argument("--token", help="API token (Bearer) — skips session login")
    ap.add_argument("--routes", action="store_true",
                    help="List all /jobs* routes the running server has registered")
    args = ap.parse_args()

    opener, _ = make_opener()

    # Token auth skips the login dance entirely.
    if not args.token:
        user = args.user or input("username: ")
        password = args.password or getpass.getpass("password: ")
        login(opener, args.base, user, password)

    # --routes: pull the live OpenAPI schema and show what's actually registered.
    if args.routes:
        code, body = request(opener, "GET", f"{args.base}/openapi.json", token=args.token)
        if code != 200:
            print(f"could not fetch openapi.json (HTTP {code}): {body[:200]}", file=sys.stderr)
            sys.exit(1)
        paths = json.loads(body).get("paths", {})
        job_routes = sorted(p for p in paths if "/jobs" in p)
        print(f"\nRegistered /jobs* routes on the running server ({args.base}):")
        for p in job_routes:
            methods = ",".join(m.upper() for m in paths[p])
            print(f"  {methods:20} {p}")
        target = "/api/v1/jobs/capture-types"
        print()
        print(f"{'PRESENT' if target in paths else 'MISSING'}: {target}")
        return

    if not args.path:
        ap.error("provide a PATH or use --routes")

    url = args.path if args.path.startswith("http") else f"{args.base}{args.path}"
    body = json.loads(args.data) if args.data else None
    code, resp = request(opener, args.method, url, body=body, token=args.token)
    print(f"HTTP {code}")
    print(pretty(resp))


if __name__ == "__main__":
    main()