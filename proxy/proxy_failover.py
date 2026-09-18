#!/usr/bin/env python3
"""
Binance egress proxy failover for local sing-box.

- Health-checks https://eapi.binance.com via http://127.0.0.1:7890
- On failure: refresh subscription (BINANCE_PROXY_URL), probe each node,
  pin the first that returns HTTP 200 from Binance eAPI, restart sing-box.

Usage:
  python proxy_failover.py once     # reprobe/pin once
  python proxy_failover.py watch    # loop forever
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent
BIN = Path(os.getenv("SINGBOX_BIN", str(ROOT / "bin" / "sing-box")))
CONF = DIR / "sing-box.json"
PID_FILE = DIR / "sing-box.pid"
STATE_FILE = DIR / "failover_state.json"
LOG_FILE = DIR / "failover.log"

LISTEN = "127.0.0.1"
PORT = int(os.getenv("PROXY_LOCAL_PORT", "7890"))
PROXY = {"http": f"http://{LISTEN}:{PORT}", "https": f"http://{LISTEN}:{PORT}"}
HEALTH_URL = os.getenv(
    "BINANCE_HEALTH_URL",
    "https://eapi.binance.com/eapi/v1/index?underlying=BTCUSDT",
)
CHECK_INTERVAL = int(os.getenv("PROXY_FAILOVER_INTERVAL_SEC", "60"))
FAIL_THRESHOLD = int(os.getenv("PROXY_FAILOVER_FAILS", "2"))
PROBE_WAIT = float(os.getenv("PROXY_PROBE_WAIT_SEC", "2.0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("proxy_failover")


def load_sub() -> str:
    v = (os.environ.get("BINANCE_PROXY_URL") or "").strip()
    if v:
        return v
    # Box-local fallback (never required in git); secrets card store
    secrets = Path(os.getenv("BOX_SECRETS_PATH", "/home/box/sand-data/box-secrets.json"))
    if secrets.exists():
        try:
            card = json.loads(secrets.read_text()).get("card") or {}
            v = (card.get("BINANCE_PROXY_URL") or "").strip()
            if v:
                return v
        except Exception as e:
            log.warning("read box secrets failed: %s", type(e).__name__)
    # Optional dotenv from repo root
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("BINANCE_PROXY_URL=") and not line.strip().startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def b64url_decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def parse_uris(uris: list[str]) -> tuple[list[dict], list[str]]:
    outbounds: list[dict] = []
    tags: list[str] = []
    for i, uri in enumerate(uris):
        try:
            if uri.startswith("ss://"):
                rest = uri[5:]
                if "#" in rest:
                    rest = rest.split("#", 1)[0]
                if "@" not in rest:
                    decoded_ss = b64url_decode(rest).decode()
                    method_pass, hostport = decoded_ss.rsplit("@", 1)
                    method, password = method_pass.split(":", 1)
                    host, port = hostport.rsplit(":", 1)
                else:
                    userinfo, hostport = rest.rsplit("@", 1)
                    try:
                        userinfo_d = b64url_decode(userinfo).decode()
                        method, password = userinfo_d.split(":", 1)
                    except Exception:
                        method, password = urllib.parse.unquote(userinfo).split(":", 1)
                    host, port = hostport.rsplit(":", 1)
                    host = host.strip("[]")
                tag = f"proxy-{i}"
                outbounds.append(
                    {
                        "type": "shadowsocks",
                        "tag": tag,
                        "server": host,
                        "server_port": int(port),
                        "method": method,
                        "password": password,
                    }
                )
                tags.append(tag)
            elif uri.startswith("vless://"):
                parsed = urllib.parse.urlparse(uri)
                q = dict(urllib.parse.parse_qsl(parsed.query))
                tag = f"proxy-{i}"
                network = q.get("type", "tcp")
                tls_enabled = q.get("security", "") in ("tls", "reality")
                ob: dict = {
                    "type": "vless",
                    "tag": tag,
                    "server": parsed.hostname,
                    "server_port": int(parsed.port),
                    "uuid": parsed.username,
                }
                if q.get("flow"):
                    ob["flow"] = q["flow"]
                if network == "ws":
                    ob["transport"] = {
                        "type": "ws",
                        "path": q.get("path") or "/",
                        "headers": {
                            "Host": q.get("host") or q.get("sni") or parsed.hostname
                        },
                    }
                elif network == "grpc":
                    ob["transport"] = {
                        "type": "grpc",
                        "service_name": q.get("serviceName") or "",
                    }
                if tls_enabled:
                    tls = {
                        "enabled": True,
                        "server_name": q.get("sni") or q.get("host") or parsed.hostname,
                        "insecure": q.get("allowInsecure", "0")
                        in ("1", "true", "True"),
                        "utls": {
                            "enabled": True,
                            "fingerprint": q.get("fp") or "chrome",
                        },
                    }
                    if q.get("security") == "reality":
                        tls["reality"] = {
                            "enabled": True,
                            "public_key": q.get("pbk") or "",
                            "short_id": q.get("sid") or "",
                        }
                    ob["tls"] = tls
                outbounds.append(ob)
                tags.append(tag)
            else:
                log.info("skip scheme %s", uri.split("://", 1)[0])
        except Exception as e:
            log.warning("parse_fail %s %s", i, type(e).__name__)
    return outbounds, tags


def build_full_config(outbounds: list[dict], tags: list[str]) -> dict:
    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": LISTEN,
                "listen_port": PORT,
            }
        ],
        "outbounds": outbounds
        + [
            {
                "type": "urltest",
                "tag": "auto",
                "outbounds": tags,
                "url": "https://www.gstatic.com/generate_204",
                "interval": "3m",
            },
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"final": "auto"},
    }


def stop_singbox() -> None:
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        if pid.isdigit():
            subprocess.run(["kill", pid], stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            subprocess.run(["kill", "-9", pid], stderr=subprocess.DEVNULL)
        PID_FILE.unlink(missing_ok=True)
    subprocess.run(
        ["pkill", "-f", str(BIN)],
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.4)


def start_singbox(conf_path: Path) -> int:
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = DIR / "sing-box.log"
    with open(log_path, "ab") as lf:
        p = subprocess.Popen(
            [str(BIN), "run", "-c", str(conf_path)],
            stdout=lf,
            stderr=lf,
            cwd=str(DIR),
        )
    PID_FILE.write_text(str(p.pid))
    time.sleep(PROBE_WAIT)
    return p.pid


def health_ok(timeout: float = 20.0) -> bool:
    try:
        r = requests.get(HEALTH_URL, proxies=PROXY, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def save_state(**kwargs) -> None:
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}
    state.update(kwargs)
    state["updated_at"] = time.time()
    STATE_FILE.write_text(json.dumps(state, indent=2))
    STATE_FILE.chmod(0o600)


def reprobe_and_pin() -> bool:
    if not BIN.exists():
        log.error("sing-box binary missing: %s", BIN)
        return False
    sub = load_sub()
    if not sub:
        log.error("BINANCE_PROXY_URL not set")
        return False
    try:
        raw = requests.get(
            sub, headers={"User-Agent": "ClashMeta/1.0"}, timeout=30
        ).text.strip()
    except Exception as e:
        log.error("fetch subscription failed: %s", type(e).__name__)
        return False
    pad = "=" * ((4 - len(raw) % 4) % 4)
    try:
        decoded = base64.b64decode(raw + pad).decode("utf-8", "replace")
    except Exception:
        decoded = raw
    uris = [ln.strip() for ln in decoded.splitlines() if ln.strip()]
    outbounds, tags = parse_uris(uris)
    if not tags:
        log.error("no nodes parsed from subscription")
        return False
    full = build_full_config(outbounds, tags)
    (DIR / "sing-box-all.json").write_text(json.dumps(full, indent=2))
    (DIR / "sing-box-all.json").chmod(0o600)
    log.info("probing %d nodes", len(tags))

    for tag in tags:
        conf = copy.deepcopy(full)
        conf["outbounds"] = [o for o in conf["outbounds"] if o.get("type") != "urltest"]
        conf["route"] = {"final": tag}
        test_path = DIR / f"test-{tag}.json"
        test_path.write_text(json.dumps(conf))
        test_path.chmod(0o600)
        stop_singbox()
        start_singbox(test_path)
        ok = health_ok()
        log.info("probe %s -> %s", tag, "OK" if ok else "FAIL")
        if ok:
            pinned = copy.deepcopy(conf)
            CONF.write_text(json.dumps(pinned, indent=2))
            CONF.chmod(0o600)
            # restart on canonical config path
            stop_singbox()
            start_singbox(CONF)
            if health_ok():
                save_state(pinned_tag=tag, last_failover_ok=True)
                log.info("pinned %s", tag)
                return True
            log.warning("pin verify failed for %s", tag)
    stop_singbox()
    save_state(last_failover_ok=False)
    log.error("NO_WORKING_NODE")
    return False


def ensure_singbox() -> None:
    alive = False
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        if pid.isdigit():
            try:
                os.kill(int(pid), 0)
                alive = True
            except OSError:
                alive = False
    if not alive:
        if CONF.exists():
            log.info("sing-box down; starting pinned config")
            start_singbox(CONF)
        else:
            log.info("no pinned config; reprobing")
            reprobe_and_pin()


def watch_loop() -> None:
    fails = 0
    log.info(
        "watch start interval=%ss fail_threshold=%s",
        CHECK_INTERVAL,
        FAIL_THRESHOLD,
    )
    while True:
        try:
            ensure_singbox()
            if health_ok():
                if fails:
                    log.info("health recovered after %s fails", fails)
                fails = 0
                save_state(last_health_ok=True, consecutive_fails=0)
            else:
                fails += 1
                log.warning("health fail %s/%s", fails, FAIL_THRESHOLD)
                save_state(last_health_ok=False, consecutive_fails=fails)
                if fails >= FAIL_THRESHOLD:
                    log.warning("failover: reprobe nodes")
                    if reprobe_and_pin():
                        fails = 0
                    else:
                        # back off a bit when nothing works
                        time.sleep(CHECK_INTERVAL)
        except Exception as e:
            log.exception("watch loop error: %s", e)
        time.sleep(CHECK_INTERVAL)


def main(argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else "once").lower()
    if mode in ("once", "reprobe", "pin"):
        ok = reprobe_and_pin()
        return 0 if ok else 1
    if mode in ("watch", "daemon"):
        watch_loop()
        return 0
    if mode == "check":
        ensure_singbox()
        ok = health_ok()
        print("health", "ok" if ok else "fail")
        return 0 if ok else 1
    print("usage: proxy_failover.py [once|watch|check]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
