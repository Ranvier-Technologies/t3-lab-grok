#!/usr/bin/env python3
"""Reachability + ceiling doctor for t3-lab targets.

Transport-agnostic: resolves the origin host and opens a TCP connect.
Tailscale, LAN, or a tunnel all look the same once DNS works and the port
answers. Exit 0 only when every reported target is either reachable or
explicitly classified as loopback-on-this-host (still reported).

Usage:
  python3 scripts/doctor-targets.py
  python3 scripts/doctor-targets.py --config ~/.config/t3-lab/v2/targets.json
  python3 scripts/doctor-targets.py --require-remote remote-dev
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_CONFIG = (Path(os.environ["T3_LAB_V2_CONFIG_DIR"]).expanduser() if os.environ.get("T3_LAB_V2_CONFIG_DIR")
                  else Path.home() / ".config" / "t3-lab" / "v2") / "targets.json"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def parse_origin(origin: str) -> tuple[str, int, str]:
    parsed = urlparse(origin)
    if not parsed.hostname:
        raise ValueError(f"unparseable origin: {origin!r}")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname, port, parsed.scheme or ""


def is_loopback_host(host: str) -> bool:
    return host.lower() in LOOPBACK


def tcp_probe(host: str, port: int, timeout: float) -> tuple[str, str | None]:
    """Return (status, detail). status in reachable|unreachable|dns_fail."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return "dns_fail", str(exc)
    last_err = None
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout)
                sock.connect(sockaddr)
            return "reachable", None
        except OSError as exc:
            last_err = str(exc)
            continue
    return "unreachable", last_err


def load_targets(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    targets = data.get("targets")
    if not isinstance(targets, list):
        raise ValueError(f"{path}: missing targets[]")
    return targets


def classify(target: dict, timeout: float) -> dict:
    tid = target.get("id", "?")
    origin = target.get("origin") or ""
    kind = target.get("kind") or ""
    ceiling = target.get("runtime_mode") or target.get("runtimeMode") or "unknown"
    row = {
        "id": tid,
        "kind": kind,
        "origin": origin,
        "ceiling": ceiling,
        "host": None,
        "port": None,
        "loopback_on_this_host": False,
        "reach": "skip",
        "detail": None,
        "ok_for_remote_spawn": False,
    }
    if not origin:
        row["reach"] = "no_origin"
        row["detail"] = "missing origin"
        return row
    try:
        host, port, _scheme = parse_origin(origin)
    except ValueError as exc:
        row["reach"] = "bad_origin"
        row["detail"] = str(exc)
        return row
    row["host"], row["port"] = host, port
    if is_loopback_host(host):
        row["loopback_on_this_host"] = True
        status, detail = tcp_probe(host, port, timeout)
        row["reach"] = status
        row["detail"] = detail
        # Loopback may answer on this host; still not a remote laptop.
        row["ok_for_remote_spawn"] = False
        if status == "reachable":
            row["detail"] = (row["detail"] or "") + (
                "; loopback is THIS host — not another machine"
            ).lstrip("; ")
        return row
    status, detail = tcp_probe(host, port, timeout)
    row["reach"] = status
    row["detail"] = detail
    row["ok_for_remote_spawn"] = status == "reachable"
    if status == "dns_fail":
        row["detail"] = (
            f"{detail}; hostname unresolved — join Tailscale/VPN/tunnel "
            "or fix DNS, then re-run doctor (no spawn code change needed)"
        )
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"targets.json path (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("--timeout", type=float, default=2.5)
    parser.add_argument(
        "--require-remote",
        action="append",
        default=[],
        metavar="ID",
        help="Fail if this target id is not TCP-reachable (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable rows")
    args = parser.parse_args()

    if not args.config.is_file():
        print(f"FAIL config missing: {args.config}", file=sys.stderr)
        return 2

    try:
        targets = load_targets(args.config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL config: {exc}", file=sys.stderr)
        return 2

    rows = [classify(t, args.timeout) for t in targets]
    if args.json:
        print(json.dumps({"config": str(args.config), "targets": rows}, indent=2))
    else:
        print(f"config: {args.config}")
        print(f"{'id':12} {'kind':7} {'ceiling':14} {'reach':12} host:port")
        for row in rows:
            hp = f"{row['host']}:{row['port']}" if row["host"] else "-"
            flag = ""
            if row["loopback_on_this_host"]:
                flag = " [loopback=this-host]"
            print(
                f"{row['id'][:12]:12} {str(row['kind'])[:7]:7} "
                f"{str(row['ceiling'])[:14]:14} {row['reach'][:12]:12} {hp}{flag}"
            )
            if row["detail"] and row["reach"] != "reachable":
                print(f"  detail: {row['detail']}")
            elif row["loopback_on_this_host"] and row["detail"]:
                print(f"  note: {row['detail']}")

    by_id = {row["id"]: row for row in rows}
    exit_code = 0
    for req in args.require_remote:
        row = by_id.get(req)
        if row is None:
            print(f"FAIL --require-remote {req}: not in config", file=sys.stderr)
            exit_code = 1
        elif not row["ok_for_remote_spawn"]:
            print(
                f"FAIL --require-remote {req}: reach={row['reach']} "
                f"(ok_for_remote_spawn=false)",
                file=sys.stderr,
            )
            exit_code = 1

    if exit_code == 0 and not args.json:
        remotes_ok = [r["id"] for r in rows if r["ok_for_remote_spawn"]]
        if remotes_ok:
            print(f"OK remote-reachable: {', '.join(remotes_ok)}")
        else:
            print(
                "WARN no remote target reachable from this host "
                "(Tailscale/tunnel not required by name — only DNS+TCP)"
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
