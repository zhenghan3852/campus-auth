#!/usr/bin/env python3
"""campus-auth 校园网掉线自动重认证

常驻循环：探测 -> 掉线则发认证请求 -> 指数退避重试
配置来源：环境变量优先，其次配置文件。日志输出到 stdout 和文件。
同时在 PORT（默认 3000）提供一个只读状态接口，供面板检查健康状态。
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

DEFAULT_CHECK = "http://connect.rom.miui.com/generate_204"
HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATHS = [
    os.environ.get("CAMPUS_CONFIG", ""),
    "/sdcard/Homedroid/campus-auth.conf",
    "/media/campus-auth.conf",
    "/mnt/homedroid/campus-auth.conf",
    os.path.join(HERE, "campus-auth.conf"),
]

LOG_PATHS = [
    "/sdcard/Homedroid/campus-auth.log",
    "/media/campus-auth.log",
    os.path.join(HERE, "campus-auth.log"),
]

ENV_KEYS = [
    "AUTH_URL", "AUTH_METHOD", "USER_FIELD", "PASS_FIELD", "CAMPUS_USER",
    "CAMPUS_PASS", "EXTRA_PARAMS", "PREFLIGHT_URL", "CHECK_URL",
    "CHECK_INTERVAL", "RETRY_INTERVAL", "MAX_BACKOFF", "USER_AGENT", "PORT",
]

STATE = {
    "status": "starting",
    "checks": 0,
    "auths": 0,
    "last_check": 0,
    "last_auth": 0,
    "config_from": "",
}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    print(line, flush=True)
    for path in LOG_PATHS:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return
        except OSError:
            continue


def load_config():
    cfg = {}
    source = "none"
    for path in CONFIG_PATHS:
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    cfg[key.strip()] = value.strip().strip('"').strip("'")
            source = path
            break

    for key in ENV_KEYS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
            if source == "none":
                source = "environment"
            elif not source.startswith("environment"):
                source += " + environment"

    STATE["config_from"] = source
    log("config loaded from " + source if source != "none" else
        "no config found, looked at: " + ", ".join(p for p in CONFIG_PATHS if p))
    return cfg


def probe(session, url, timeout=6):
    try:
        r = session.get(url, timeout=timeout, allow_redirects=False)
        return "online" if r.status_code == 204 else "portal"
    except requests.RequestException as e:
        return "down:" + type(e).__name__


def authenticate(session, cfg):
    url = cfg.get("AUTH_URL", "")
    if not url:
        log("AUTH_URL missing, skip")
        return
    params = {
        cfg.get("USER_FIELD", "username"): cfg.get("CAMPUS_USER", ""),
        cfg.get("PASS_FIELD", "password"): cfg.get("CAMPUS_PASS", ""),
    }
    extra = cfg.get("EXTRA_PARAMS", "")
    if extra:
        try:
            params.update(json.loads(extra))
        except json.JSONDecodeError:
            log("EXTRA_PARAMS is not valid JSON, ignored")

    headers = {"User-Agent": cfg.get("USER_AGENT", "Mozilla/5.0 (Linux; Android 12)")}
    method = cfg.get("AUTH_METHOD", "POST").upper()
    try:
        if cfg.get("PREFLIGHT_URL"):
            session.get(cfg["PREFLIGHT_URL"], timeout=8, headers=headers)
        if method == "GET":
            r = session.get(url, params=params, timeout=10, headers=headers)
        else:
            r = session.post(url, data=params, timeout=10, headers=headers)
        log("auth %s -> HTTP %s %s" % (method, r.status_code, r.text[:200].replace("\n", " ")))
    except requests.RequestException as e:
        log("auth failed: " + type(e).__name__)


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        STATE["uptime"] = int(time.time() - STARTED_AT)
        body = json.dumps(STATE, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_status_server(port):
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), StatusHandler)
    except OSError as e:
        log("status server failed on :%s (%s)" % (port, e))
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("status server listening on :%s" % port)


STARTED_AT = time.time()


def main():
    cfg = load_config()
    check_url = cfg.get("CHECK_URL", DEFAULT_CHECK)
    interval = int(cfg.get("CHECK_INTERVAL", "30"))
    retry = int(cfg.get("RETRY_INTERVAL", "15"))
    max_backoff = int(cfg.get("MAX_BACKOFF", "300"))
    port = int(cfg.get("PORT", "3000"))

    start_status_server(port)
    log("started pid=%s check=%s" % (os.getpid(), check_url))

    session = requests.Session()
    session.headers.update({"User-Agent": cfg.get("USER_AGENT", "Mozilla/5.0 (Linux; Android 12)")})

    backoff = retry
    while True:
        state = probe(session, check_url)
        STATE["checks"] += 1
        STATE["last_check"] = int(time.time())
        STATE["status"] = state

        if state == "online":
            backoff = retry
            time.sleep(interval)
            continue

        if state.startswith("down"):
            log("network unreachable (%s), retry in %ss" % (state, backoff))
        else:
            log("portal detected, authenticating")
            STATE["auths"] += 1
            STATE["last_auth"] = int(time.time())
            authenticate(session, cfg)
            time.sleep(5)
            if probe(session, check_url) == "online":
                log("back online")
                STATE["status"] = "online"
                backoff = retry
                continue
            log("auth did not take effect")
            STATE["status"] = "auth-failed"

        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
