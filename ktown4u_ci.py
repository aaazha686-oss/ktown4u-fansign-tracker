#!/usr/bin/env python3
"""
Ktown4u 云端连续抓取(GitHub Actions,真·每10秒)
================================================
一个任务长时间运行,全程每 interval 秒抓一次各成员销量,每 commitEvery 秒把数据
提交回仓库。靠 workflow 的「定时取消重启」接力,做到近似连续的 10 秒采样
(只在每次重启时有约 30 秒空档,GitHub 的设计限制)。

数据:data/track_<活动号>.csv,字段 time, member, sales, delta(增量跨重启续接)。
配置:track_config.json
  { "shopNo":164, "interval":10, "runSeconds":1500, "commitEvery":60, "events":[44469917] }
"""
import csv
import json
import os
import re
import signal
import ssl
import subprocess
import time
import urllib.request
from datetime import datetime, timezone


def load_cfg():
    return json.load(open("track_config.json", encoding="utf-8"))


CFG = load_cfg()
SHOP = int(CFG.get("shopNo", 164))
INTERVAL = int(os.environ.get("INTERVAL", CFG.get("interval", 10)))
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", CFG.get("runSeconds", 1500)))
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY", CFG.get("commitEvery", 60)))
NOCOMMIT = os.environ.get("NOCOMMIT") == "1"
DATADIR = "data"
GQL = "https://apis.ktown4u.com/vador/graphql?operationName=eventProductsV2"
Q = ("query eventProductsV2($request: EventProductsRequest!){ "
     "eventProductsV2(request:$request){ groups{ groupName products{ name sales } } } }")

os.makedirs(DATADIR, exist_ok=True)
_CTX = ssl.create_default_context()
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass

_STOP = False


def _sig(*_a):
    global _STOP
    _STOP = True


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)


def member_of(name):
    name = name or ""
    m = re.search(r"\(([^)]+)\)", name)
    if "Video Call" in name:
        return "VideoCall·" + (m.group(1) if m else "?")
    if "Off-Line" in name or "Off-line" in name:
        return "OffLine"
    return name[:24]


def fetch_agg(eno, retries=25):
    body = json.dumps({"operationName": "eventProductsV2", "query": Q,
                       "variables": {"request": {"eventNo": int(eno), "shopNo": SHOP}}}).encode()
    req = urllib.request.Request(GQL, data=body, headers={
        "Content-Type": "application/json", "Origin": "https://www.ktown4u.com",
        "Referer": "https://www.ktown4u.com/", "User-Agent": "Mozilla/5.0"})
    d = {}
    for _ in range(retries):
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            d = json.loads(r.read())
        if any(e.get("extensions", {}).get("code") == "CACHE_LOADING" for e in (d.get("errors") or [])):
            time.sleep(1)
            continue
        break
    if d.get("errors"):
        raise RuntimeError(d["errors"][0].get("message", "error"))
    agg = {}
    g = (d.get("data") or {}).get("eventProductsV2") or {}
    for grp in g.get("groups", []):
        for p in grp.get("products", []):
            k = member_of(p.get("name"))
            agg[k] = agg.get(k, 0) + (p.get("sales") or 0)
    return agg


def last_sales(path):
    prev = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    prev[row["member"]] = int(float(row["sales"]))
                except (TypeError, ValueError, KeyError):
                    pass
    return prev


def append_rows(path, ts, agg, prev):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "member", "sales", "delta"])
        for m, v in agg.items():
            last = prev.get(m)
            w.writerow([ts, m, v, "" if last is None else v - last])
            prev[m] = v


def commit_push():
    if NOCOMMIT:
        return
    subprocess.run(["git", "add", "data"], check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        msg = "track " + datetime.now(timezone.utc).isoformat(timespec="seconds")
        subprocess.run(["git", "commit", "-q", "-m", msg], check=False)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"], check=False)
        subprocess.run(["git", "push", "-q"], check=False)


def main():
    events = [str(e) for e in load_cfg().get("events", [])]
    if not events:
        print("track_config.json 的 events 为空。")
        return
    prev = {e: last_sales(os.path.join(DATADIR, f"track_{e}.csv")) for e in events}
    end = time.time() + RUN_SECONDS
    last_commit = time.time()
    polls = 0
    while time.time() < end and not _STOP:
        ts = datetime.now(timezone.utc).isoformat()
        for e in events:
            try:
                append_rows(os.path.join(DATADIR, f"track_{e}.csv"), ts, fetch_agg(e), prev[e])
            except Exception as ex:
                print(f"[{ts}] event {e} error: {ex}")
        polls += 1
        if time.time() - last_commit >= COMMIT_EVERY:
            commit_push()
            last_commit = time.time()
            # 提交时顺带 pull,能拿到你在网页上改过的 track_config.json
            new_events = [str(e) for e in load_cfg().get("events", [])]
            if new_events != events:
                events = new_events
                for e in events:
                    prev.setdefault(e, last_sales(os.path.join(DATADIR, f"track_{e}.csv")))
                print(f"配置更新,现在追踪:{events}")
        if not _STOP:
            time.sleep(INTERVAL)
    commit_push()
    print(f"本段结束:{polls} 轮 (stop={_STOP})")


if __name__ == "__main__":
    main()
