#!/usr/bin/env python3
"""
Ktown4u 云端抓取(GitHub Actions 用,自包含)
============================================
每次被 Actions 调起,就做一段「密集突发抓取」:在 burstSeconds 时间内每
interval 秒抓一次各成员销量,把 (时间, 成员, 累计销量, 增量) 追加到
data/track_<活动号>.csv。增量跨多次运行连续(读上次 CSV 的最后值续上)。

配置见 track_config.json:
  { "shopNo":164, "interval":10, "burstSeconds":200, "events":[44469917] }

为什么这样设计:GitHub 定时任务最细每 5 分钟一轮,所以每轮内部用 10 秒
密抓 ~200 秒,轮与轮拼接 ≈ 准 10 秒的连续覆盖。
"""
import csv
import json
import os
import re
import ssl
import time
import urllib.request
from datetime import datetime, timezone

CFG = json.load(open("track_config.json", encoding="utf-8"))
SHOP = int(CFG.get("shopNo", 164))
INTERVAL = int(os.environ.get("INTERVAL", CFG.get("interval", 10)))
BURST = int(os.environ.get("BURST_SECONDS", CFG.get("burstSeconds", 200)))
EVENTS = [str(e) for e in CFG.get("events", [])]
GQL = "https://apis.ktown4u.com/vador/graphql?operationName=eventProductsV2"
DATADIR = "data"
Q = ("query eventProductsV2($request: EventProductsRequest!){ "
     "eventProductsV2(request:$request){ groups{ groupName products{ name sales } } } }")

os.makedirs(DATADIR, exist_ok=True)
_CTX = ssl.create_default_context()
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass  # GitHub runner 自带 CA,默认即可


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


def main():
    if not EVENTS:
        print("track_config.json 的 events 为空,没什么可抓。")
        return
    prev = {e: last_sales(os.path.join(DATADIR, f"track_{e}.csv")) for e in EVENTS}
    end = time.time() + BURST
    polls = 0
    while time.time() < end:
        ts = datetime.now(timezone.utc).isoformat()
        for e in EVENTS:
            try:
                agg = fetch_agg(e)
                append_rows(os.path.join(DATADIR, f"track_{e}.csv"), ts, agg, prev[e])
            except Exception as ex:
                print(f"[{ts}] event {e} error: {ex}")
        polls += 1
        if time.time() < end:
            time.sleep(INTERVAL)
    print(f"burst 完成:{polls} 轮,events={EVENTS}")


if __name__ == "__main__":
    main()
