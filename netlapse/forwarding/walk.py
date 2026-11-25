"""
Walks over the forwarding model.

For one destination, every device's action (longest match -> next hops) is
fixed, so the network is a graph per destination. A source's path is a walk
of that graph: ECMP branches fan out, branches can converge on a shared device
(normal, not a loop), and a device that reaches itself again is a loop.

Terminals:
  delivers   connected/local route covers the destination
  blackhole  Null0 / Reject / Discard (an aggregate with nothing behind it)
  noroute    no matching route at all
  overlay    route points into a tunnel/VXLAN interface the model can't follow
  unknown    a route with no usable next hop
  exit       next hop leaves the modeled devices (transit, unmanaged, out of scope)
  loop       forwarding returns to a device already on the path

Pair status:
  ok         every branch delivers at the destination endpoint
  elsewhere  every branch delivers, but on another device (shared L2 segment)
  partial    some branches deliver
  fail       none do

Symmetric: the two directions cross the same set of transit devices. ECMP is
compared as sets, so two directions sharing every device but using different
links are symmetric here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from netlapse.forwarding.model import ForwardingModel
from netlapse.forwarding.normalize import ip2int, parse_prefix


def _walk(actions: Dict[str, dict], src: str, action_fn: Callable[[str], dict]) -> dict:
    memo: Dict[str, dict] = {}
    on_stack = set()

    def visit(n: str) -> dict:
        if n in memo:
            return memo[n]
        if n in on_stack:
            return {"terms": {"loop"}, "minh": 0, "maxh": 0, "np": 1, "reach": {n}, "dep": set(), "exits": set()}
        on_stack.add(n)
        a = actions.get(n)
        if a is None:
            a = actions[n] = action_fn(n)
        if a["t"] != "fwd":
            res = {"terms": {a["t"]}, "minh": 0, "maxh": 0, "np": 1, "reach": {n}, "dep": set(), "exits": set()}
        else:
            terms, mins, maxs, np_, reach, dep, exits = set(), [], [], 0, {n}, set(), set()
            if a["c"] in ("default", "aggregate"):
                dep.add(n)
            targets = list(dict.fromkeys((t["k"], t["n"]) for t in a["x"]))
            for k, name in targets:
                if k == "exit":
                    terms.add("exit")
                    exits.add(f"{n} -> {name}")
                    mins.append(1)
                    maxs.append(1)
                    np_ += 1
                    continue
                c = visit(name)
                terms |= c["terms"]
                mins.append(c["minh"] + 1)
                maxs.append(c["maxh"] + 1)
                np_ += c["np"]
                reach |= c["reach"]
                dep |= c["dep"]
                exits |= c["exits"]
            res = {"terms": terms or {"unknown"}, "minh": min(mins) if mins else 0,
                   "maxh": max(maxs) if maxs else 0, "np": np_ or 1, "reach": reach, "dep": dep, "exits": exits}
        on_stack.discard(n)
        if "loop" not in res["terms"]:
            memo[n] = res
        return res

    return visit(src)


def _status(terms: set, delivered_at: List[str], dst: Optional[str]) -> str:
    if terms == {"delivers"}:
        return "ok" if (dst is None or dst in delivered_at) else "elsewhere"
    return "partial" if "delivers" in terms else "fail"


def _summary(model: ForwardingModel, actions: Dict[str, dict], src: str, dst: Optional[str], s: dict) -> dict:
    delivered_at = sorted(n for n in s["reach"] if actions.get(n, {}).get("t") == "delivers")
    first = actions[src]
    fanout = len({(x["k"], x["n"]) for x in first.get("x", [])}) if first["t"] == "fwd" else 0
    return {
        "status": _status(s["terms"], delivered_at, dst), "terms": sorted(s["terms"]),
        "minh": s["minh"], "maxh": s["maxh"], "np": s["np"], "reach": sorted(s["reach"]),
        "dep": len(s["dep"]), "dep_devs": sorted(s["dep"]), "exits": sorted(s["exits"]),
        "fanout": fanout, "delivered_at": delivered_at,
    }


@dataclass
class MatrixResult:
    endpoints: List[str]
    pairs: Dict[Tuple[str, str], dict]
    actions: Dict[str, Dict[str, dict]]
    endpoint_summary: List[dict]
    meta: dict

    def to_dict(self, model: ForwardingModel) -> dict:
        return {
            "meta": self.meta,
            "tors": [{"name": n, "site": model.site_of(n), "subnet": model.destinations[n].subnet,
                      "dest": model.destinations[n].host, "interface": model.destinations[n].interface,
                      "unique": model.destinations[n].unique} for n in self.endpoints],
            "tor_summary": self.endpoint_summary,
            "pairs": {f"{a}|{b}": p for (a, b), p in self.pairs.items()},
            "actions": self.actions,
            "devices": {n: {"id": s.device.get("id"), "site": model.site_of(n),
                            "platform": s.device.get("platform_slug"), "role": s.device.get("role_slug"),
                            "is_tor": s.endpoint} for n, s in model.by_node.items()},
        }


def build_matrix(model: ForwardingModel, progress: Optional[Callable[[str], None]] = None) -> MatrixResult:
    progress = progress or (lambda m: None)
    eps = [n for n in model.endpoints if n in model.destinations]
    pairs: Dict[Tuple[str, str], dict] = {}
    per_dest: Dict[str, Dict[str, dict]] = {}
    for i, dst in enumerate(eps, 1):
        d = model.destinations[dst]
        dint, dplen = ip2int(d.host), d.plen
        actions: Dict[str, dict] = {}

        def action_fn(n, dint=dint, dplen=dplen):
            return model.action(n, dint, dplen)

        for src in eps:
            if src == dst:
                continue
            pairs[(src, dst)] = _summary(model, actions, src, dst, _walk(actions, src, action_fn))
        per_dest[dst] = actions
        if i % 10 == 0 or i == len(eps):
            progress(f"destinations {i}/{len(eps)}")

    for (a, b), p in pairs.items():
        r = pairs.get((b, a))
        fa, fb = set(p["reach"]) - {a, b}, set(r["reach"]) - {a, b}
        p["sym"] = fa == fb
        p["only_fwd"] = sorted(fa - fb)
        p["only_rev"] = sorted(fb - fa)

    summary = []
    for t in eps:
        outp = [pairs[(t, d)] for d in eps if d != t]
        inp = [pairs[(s, t)] for s in eps if s != t]
        fan = Counter(p["fanout"] for p in outp)
        summary.append({
            "tor": t, "site": model.site_of(t), "subnet": model.destinations[t].subnet,
            "out_ok": sum(p["status"] == "ok" for p in outp), "in_ok": sum(p["status"] == "ok" for p in inp),
            "asym": sum(p["sym"] is False for p in outp),
            "fanout_min": min(fan) if fan else 0, "fanout_max": max(fan) if fan else 0,
            "single_homed_pairs": fan.get(1, 0), "maxh": max((p["maxh"] for p in outp), default=0),
        })

    status = Counter(p["status"] for p in pairs.values())
    terms = Counter(t for p in pairs.values() for t in p["terms"])
    sigs = {tuple(p["reach"][1:]) for p in pairs.values()}
    meta = {
        "sites": model.sites, "as_of": model.reader.as_of.isoformat() if model.reader.as_of else None,
        "tors": len(eps), "pairs": len(pairs), "devices": len(model.by_node),
        "status": dict(status), "terminals": dict(terms),
        "asymmetric": sum(p["sym"] is False for p in pairs.values()), "signatures": len(sigs),
        "no_dest_tors": sorted(n for n in model.endpoints if n not in model.destinations),
        "excluded": model.excluded, "problems": model.problems,
        "ambiguous_ips": model.ambiguous_ips(), "skew": model.capture_skew(),
        "config": model.cfg.source_path,
    }
    return MatrixResult(endpoints=eps, pairs=pairs, actions=per_dest, endpoint_summary=summary, meta=meta)


def trace(model: ForwardingModel, src: str, dest: str) -> dict:
    """
    Offline fibtrace: src device -> destination IP (or prefix, or endpoint name).
    Returns the summary plus the per-hop layers the walk took.
    """
    node = model.node_for_name(src)
    if node is None:
        raise KeyError(f"unknown or excluded device: {src}")
    dst_node = None
    if model.node_for_name(dest) in model.destinations:
        dst_node = model.node_for_name(dest)
        d = model.destinations[dst_node]
        dint, dplen, label = ip2int(d.host), d.plen, f"{d.host} ({d.subnet} on {dst_node})"
    else:
        pp = parse_prefix(dest if "/" in dest else f"{dest}/32")
        if pp is None:
            raise ValueError(f"not an IPv4 address, prefix or endpoint: {dest}")
        dint, dplen = pp
        if dplen < 32:
            dint += 1
        label = dest
        for n, dd in model.destinations.items():
            if (dint & ((0xFFFFFFFF << (32 - dd.plen)) & 0xFFFFFFFF)) == dd.net:
                dst_node, dplen = n, dd.plen
                break

    # Transit tables only hold routes that can match the endpoint subnets and
    # the model's extra_destinations; build the model with the traced address
    # in extra_destinations (the CLI does) to trace anything else.
    actions: Dict[str, dict] = {}
    s = _walk(actions, node, lambda n: model.action(n, dint, dplen))
    out = _summary(model, actions, node, dst_node, s)

    layers, seen, frontier = [[node]], {node}, [node]
    while frontier and len(layers) < 64:
        nxt = []
        for n in frontier:
            a = actions.get(n, {})
            for x in a.get("x", []) if a.get("t") == "fwd" else []:
                if x["k"] == "dev" and x["n"] not in seen:
                    seen.add(x["n"])
                    nxt.append(x["n"])
        if nxt:
            layers.append(nxt)
        frontier = nxt
    out.update({"src": node, "dest": label, "dest_endpoint": dst_node,
                "layers": [[{"device": n, "id": model.by_node[n].device.get("id"),
                             "site": model.site_of(n), **actions[n]} for n in layer] for layer in layers]})
    return out
