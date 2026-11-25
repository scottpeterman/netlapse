"""
netlapse forwarding — offline path tracing and the endpoint forwarding matrix.

    netlapse forwarding matrix [--site S ...] [--as-of T] [--out-dir DIR]
    netlapse forwarding trace SRC DEST [--site S ...] [--as-of T] [--json]
    netlapse forwarding check [--site S ...]

Reads the same config.yaml as the daemon (NETLAPSE_CONFIG or
~/.netlapse/config.yaml) for the storage backend and DCIM database. Nothing
is collected and no device is contacted.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path


def _open(ns):
    from netlapse.app import load_config
    from netlapse.dcim.db_schema import NetlapseDB
    from netlapse.forwarding.config import load_forwarding_config
    from netlapse.storage import create_backend
    from netlapse.topology.history import CaptureReader, parse_as_of

    config = load_config()
    storage = create_backend(config)
    db_path = Path(config.get("dcim_db", "~/.netlapse/netlapse.db")).expanduser()
    if not db_path.exists():
        raise SystemExit(f"no DCIM database at {db_path}")
    devices = NetlapseDB(db_path).list_devices(status="active")
    cfg = load_forwarding_config(ns.config, config)
    reader = CaptureReader(storage, parse_as_of(ns.as_of))
    return devices, reader, cfg


def _progress(quiet):
    start = time.monotonic()

    def emit(msg):
        if not quiet:
            print(f"  {msg}  ({time.monotonic() - start:.0f}s)", file=sys.stderr, flush=True)
    return emit


def _model(ns, extra=None):
    from netlapse.forwarding.model import ForwardingModel
    devices, reader, cfg = _open(ns)
    return ForwardingModel(devices, reader, cfg, sites=ns.site, progress=_progress(ns.quiet),
                           extra_destinations=extra)


def _print_quality(meta):
    sk = meta.get("skew") or {}
    if sk.get("stale"):
        print(f"stale route captures ({len(sk['stale'])}): "
              + ", ".join(f"{x['device']} {x['minutes_behind']}m" for x in sk["stale"][:8]))
    for dev, prob in sorted((meta.get("problems") or {}).items()):
        print(f"unmodeled routes  {dev}: {prob}")
    if meta.get("no_dest_tors"):
        print(f"endpoints without a destination ({len(meta['no_dest_tors'])}): " + ", ".join(meta["no_dest_tors"]))
    amb = meta.get("ambiguous_ips") or {}
    if amb:
        print(f"addresses owned by more than one device ({len(amb)}): "
              + ", ".join(f"{ip}={'/'.join(o)}" for ip, o in list(amb.items())[:4]))


def cmd_matrix(ns) -> int:
    from netlapse.forwarding.report import render_html
    from netlapse.forwarding.walk import build_matrix

    model = _model(ns)
    result = build_matrix(model, progress=_progress(ns.quiet))
    result.meta["built_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    data = result.to_dict(model)

    out = Path(ns.out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "matrix.json").write_text(json.dumps(data, default=str))
    (out / "matrix.html").write_text(render_html(data))
    with open(out / "pairs.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["src", "dst", "status", "terminals", "min_hops", "max_hops", "paths", "fanout",
                    "symmetric", "only_forward", "only_reverse", "default_dep_devices", "exits"])
        for (a, b), p in sorted(result.pairs.items()):
            w.writerow([a, b, p["status"], " ".join(p["terms"]), p["minh"], p["maxh"], p["np"], p["fanout"],
                        p["sym"], " ".join(p["only_fwd"]), " ".join(p["only_rev"]),
                        " ".join(p["dep_devs"]), " | ".join(p["exits"])])

    m = result.meta
    print(f"{m['tors']} endpoints, {m['pairs']} directed pairs, {m['devices']} devices modeled"
          + (f", as of {m['as_of']}" if m["as_of"] else ""))
    print("status:     " + "  ".join(f"{k}={v}" for k, v in sorted(m["status"].items())))
    print("terminals:  " + "  ".join(f"{k}={v}" for k, v in sorted(m["terminals"].items())))
    print(f"asymmetric: {m['asymmetric']} of {m['pairs']}   distinct path signatures: {m['signatures']}")
    single = [s["tor"] for s in result.endpoint_summary if s["fanout_max"] == 1]
    if single:
        print(f"single first hop to every destination ({len(single)}): " + ", ".join(single))
    _print_quality(m)
    print(f"wrote {out / 'matrix.html'}, {out / 'matrix.json'}, {out / 'pairs.csv'}")
    return 0


def cmd_trace(ns) -> int:
    from netlapse.forwarding.normalize import parse_prefix
    from netlapse.forwarding.walk import trace

    dest = ns.dest
    extra = [dest] if parse_prefix(dest if "/" in dest else f"{dest}/32") else None
    model = _model(ns, extra=extra)
    try:
        t = trace(model, ns.src, dest)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if ns.json:
        print(json.dumps(t, indent=2, default=str))
        return 0
    print(f"{t['src']} -> {t['dest']}")
    print(f"{t['status']} · terminals {', '.join(t['terms'])} · hops {t['minh']}-{t['maxh']} · "
          f"{t['np']} paths · first-hop fanout {t['fanout']}")
    for i, layer in enumerate(t["layers"]):
        for a in layer:
            head = f"  hop {i:<2} {a['device']:<28}"
            if a["t"] == "fwd":
                hops = ", ".join(f"{x['nh'] or ''} {x['if']} -> {x['n'] if x['k'] == 'dev' else 'EXIT ' + x['n']}"
                                 for x in a["x"])
                print(f"{head} {a['p']:<18} {a['c']:<9} {hops}")
            else:
                print(f"{head} {a.get('p', ''):<18} {a.get('c', ''):<9} {a['t'].upper()} {a.get('if', '')}")
    for e in t["exits"]:
        print(f"  exit: {e}")
    return 0


def cmd_check(ns) -> int:
    """Data readiness: does each device have what the model needs?"""
    model = _model(ns)
    print(f"{'device':30} {'role':12} {'routes':>8} {'usable':>8} {'owned':>6} {'lldp':>5}  notes")
    for node in sorted(model.by_node, key=lambda n: (model.site_of(n), n)):
        st = model.by_node[node]
        info = st.route_info
        notes = []
        if st.endpoint:
            d = model.destinations.get(node)
            notes.append(f"dest {d.subnet} ({d.interface})" if d else "NO DESTINATION")
        if info.get("problem"):
            notes.append(info["problem"])
        if not st.owned:
            notes.append("no owned addresses (add the ip-interfaces capture)")
        print(f"{node[:30]:30} {(st.device.get('role_slug') or '')[:12]:12} {st.routes_total:>8} "
              f"{info.get('usable', 0):>8} {len(st.owned):>6} {len(st.lldp):>5}  {'; '.join(notes)}")
    if model.excluded:
        print(f"excluded ({len(model.excluded)}): " + ", ".join(model.excluded))
    print(f"config: {model.cfg.source_path}")
    return 0


def cli_forwarding(args: list) -> int:
    p = argparse.ArgumentParser(prog="netlapse forwarding",
                                description="Offline path tracing and the endpoint forwarding matrix")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--site", action="append", help="site slug (repeatable); default all sites")
        sp.add_argument("--as-of", default=None, help="point in time (ISO-8601); default latest")
        sp.add_argument("--config", default=None, help="forwarding.yaml (default: definitions dir, then built-in)")
        sp.add_argument("-q", "--quiet", action="store_true", help="no progress lines")

    m = sub.add_parser("matrix", help="build the endpoint-to-endpoint matrix and HTML report")
    common(m)
    m.add_argument("--out-dir", default="forwarding_out")
    m.set_defaults(fn=cmd_matrix)

    t = sub.add_parser("trace", help="offline fibtrace from a device to an address, prefix or endpoint")
    common(t)
    t.add_argument("src")
    t.add_argument("dest")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_trace)

    c = sub.add_parser("check", help="per-device data readiness for the model")
    common(c)
    c.set_defaults(fn=cmd_check)

    ns = p.parse_args(args)
    return ns.fn(ns)
