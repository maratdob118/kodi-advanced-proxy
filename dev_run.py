#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local dev runner: runs the ProxySupervisor WITHOUT Kodi.

Simulates main.py's service loop using plain Python so we can develop and test
the addon logic (config gen, sing-box launch, watchdog, proxy responses) on the
dev machine before deploying to a real Kodi box.

Usage:
    python3 dev_run.py [--seconds N] [--no-run]

    --seconds N   run the supervisor loop for N seconds then stop (default: 15)
    --no-run      only generate + validate config, don't start sing-box
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_DIR = os.path.join(HERE, "service.advancedproxy")
sys.path.insert(0, os.path.join(ADDON_DIR, "src"))

import supervisor  # noqa: E402

SUB_URL = "https://bigping.duckdns.org/sub/Xj7kM9pQ2wR5vN8sT4fL1hY6gA3dE0cB/urls"

SETTINGS = {
    "subscription_url": SUB_URL,
    "skip_protocols": "trojan,xhttp",
    "local_port": 1080,
    "lan_mixed_enabled": False,
    "lan_mixed_port": 1080,
    "urltest_interval": "3m",
    "urltest_tolerance": 50,
    "interrupt_connections": True,
    "test_url": "https://www.gstatic.com/generate_204",
    "log_level": "info",
    "binary_platform_override": "auto",
}


def log(msg, level="info"):
    print("[%s] %s" % (level.upper(), msg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=15)
    ap.add_argument("--no-run", action="store_true")
    args = ap.parse_args()

    work_dir = os.path.join(HERE, "_work")
    os.makedirs(work_dir, exist_ok=True)

    sup = supervisor.ProxySupervisor(
        settings=dict(SETTINGS),
        addon_dir=ADDON_DIR,
        work_dir=work_dir,
        logger=log,
    )

    if args.no_run:
        ok = sup.build_and_write_config()
        log("config build: %s" % ("OK" if ok else sup.last_error),
            "info" if ok else "error")
        if ok:
            with open(sup.config_path) as f:
                cfg = json.load(f)
            log("inbounds: %s" % [(i["type"], i["listen_port"]) for i in cfg["inbounds"]])
            ut = [o for o in cfg["outbounds"] if o["type"] == "urltest"][0]
            log("urltest: %d outbounds interval=%s tolerance=%s" % (
                len(ut["outbounds"]), ut["interval"], ut["tolerance"]))
        return 0 if ok else 1

    if not sup.start():
        log("start failed: %s" % sup.last_error, "error")
        return 1

    log("running for %ds; proxy on 127.0.0.1:%s (mixed SOCKS5/HTTP)" % (
        args.seconds, SETTINGS["local_port"]))
    try:
        for _ in range(args.seconds):
            sup.tick()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log("status: %s" % sup.status())
        sup.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
