#!/usr/bin/env python3
"""
fanme(fanmeofficial.com, Shopline)抓取器 —— 框架第3个平台
==========================================================
fanme 有公开接口(无需认证):
  GET /api/merchants/{merchant}/products/{product_id}/check_stock?variation_id={key}
  -> {"left_items_quantity": 9994, "quantity": 9994, ...}
left_items_quantity = 该选项剩余名额(报名后递减)。已报名 = 上限cap − 剩余。
fanme 每个成员/类型是单独商品(各自10000名额)→ 真正的分成员 cut。

产出 data/track_<事件id>.csv(time, member, sales, delta)+ data/fanme_index.json。
配置:track_config.json 的 "fanme" 段,或 fanme_events.json(VPS用)。
代理:FANME_PROXY 或复用 MND_PROXY(若云端IP被 fanme 封)。
"""
import csv
import json
import os
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
DATADIR = "data"
STATE = os.path.join(DATADIR, "fanme_state.json")
CAP_DEFAULT = 10000
HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en,zh-CN;q=0.9",
    "Referer": "https://www.fanmeofficial.com/",
}
os.makedirs(DATADIR, exist_ok=True)
_CTX = ssl.create_default_context()
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass

# 默认直连(fanme 是 Shopline 全球CDN,机房IP一般不封);若云端被封,设 FANME_PROXY
_PROXY = (os.environ.get("FANME_PROXY") or "").strip()
if _PROXY:
    _OPENER = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}),
        urllib.request.HTTPSHandler(context=_CTX))
else:
    _OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))


def check_stock(merchant, pid, key):
    url = (f"https://www.fanmeofficial.com/api/merchants/{merchant}"
           f"/products/{pid}/check_stock?variation_id={key}")
    req = urllib.request.Request(url, headers=HDRS)
    with _OPENER.open(req, timeout=20) as r:
        d = json.loads(r.read())
    return d.get("left_items_quantity")


def load_json(p, d):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


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


def poll_event(ev, state):
    eid = ev["id"]
    merchant = ev["merchant"]
    path = os.path.join(DATADIR, f"track_{eid}.csv")
    prev = last_sales(path)
    ts = datetime.now(BJ).isoformat(timespec="seconds")
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    rows, total, members = [], 0, 0
    products = ev.get("products", [])

    def _fetch(p):
        try:
            return p, check_stock(merchant, p["productId"], p["key"])
        except Exception as e:
            print(f"  fanme {p.get('label')} 抓取失败: {e}")
            return p, None

    # 11个商品并发抓取(延迟从~4s降到<1s)
    with ThreadPoolExecutor(max_workers=min(12, len(products) or 1)) as ex:
        results = list(ex.map(_fetch, products))

    for p, left in results:
        if left is None:
            continue
        # 上限cap:取 max(默认10000, 历史见过的最大剩余);已报名 = cap − 剩余
        skey = p["key"]
        cap = max(CAP_DEFAULT, state.get(skey, 0), left)
        state[skey] = cap
        sold = max(0, cap - left)
        member = p.get("label") or skey
        last = prev.get(member)
        rows.append({"time": ts, "member": member, "sales": sold,
                     "delta": "" if last is None else sold - last})
        total += sold
        members += 1
    if rows:
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "member", "sales", "delta"])
            for r in rows:
                w.writerow([r["time"], r["member"], r["sales"], r["delta"]])
    return {"eventNo": eid, "title": ev.get("name", eid), "platform": "fanme",
            "status": "live", "tracked": True, "total": total, "members": members,
            "hasSummary": False}


def ended_entry(ev):
    """已结束活动:不再发请求,直接用 CSV 里最后的销量构建索引条目(保留展示)。"""
    eid = ev["id"]
    prev = last_sales(os.path.join(DATADIR, f"track_{eid}.csv"))
    return {"eventNo": eid, "title": ev.get("name", eid), "platform": "fanme",
            "status": "ended", "tracked": False, "total": sum(prev.values()),
            "members": len(prev), "hasSummary": False,
            "end": ev.get("end", "")}


def load_events():
    e = load_json("fanme_events.json", None)
    if e is not None:
        return e if isinstance(e, list) else e.get("events", [])
    return load_json("track_config.json", {}).get("fanme", {}).get("events", [])


def main():
    events = load_events()
    print(f"[fanme] 启动 events={len(events)} 代理={'已启用' if _PROXY else '直连'}")
    if not events:
        return
    state = load_json(STATE, {})
    # 已结束的活动跳过抓取、只保留展示;在售的正常抓
    summary = [ended_entry(ev) if ev.get("ended") else poll_event(ev, state) for ev in events]
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    idx = {"updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "platform": {"id": "fanme", "name": "fanme (fanmeofficial)",
                        "supported": True, "events": summary}}
    json.dump(idx, open(os.path.join(DATADIR, "fanme_index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("fanme 抓取完成:", [(s["title"], "total=" + str(s["total"]), s["members"]) for s in summary])


if __name__ == "__main__":
    main()
