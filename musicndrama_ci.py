#!/usr/bin/env python3
"""
musicndrama(뮤직앤드라마, imweb)抓取器 —— 框架第2个平台
=========================================================
imweb 商品页里有 `initProdStock(true, 剩余库存, ...)`,开了库存的商品(签售/photo
event/限量)剩余库存随下单递减。本脚本抓每个商品的剩余库存,折算成:
  sales(已售) = 基准库存(首次看到) − 当前剩余   (随下单递增,和 K4 同语义)
  delta       = 这次相对上次的增量
产出 data/track_<事件id>.csv(time, member, sales, delta),并写 data/mnd_index.json
(musicndrama 平台块,供门户合并显示)。基准库存存 data/mnd_state.json 持久化。

配置 track_config.json 的 musicndrama 段:
  "musicndrama": {
    "events": [
      { "id": "mnd_xxxx", "name": "活动名", "products": [13151, 13152, ...] }
    ]
  }
每个 product idx = 一个成员/版本(imweb 里成员是分开的商品)。member 名从标题解析。
"""
import csv
import json
import os
import re
import ssl
import time
import urllib.request
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
DATADIR = "data"
STATE = os.path.join(DATADIR, "mnd_state.json")
BASE = "https://www.musicndrama.com/shop_view/?idx="
os.makedirs(DATADIR, exist_ok=True)
_CTX = ssl.create_default_context()
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass


_HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.musicndrama.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin",
}


def _get(url):
    req = urllib.request.Request(url, headers=_HDRS)
    with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
        return r.read().decode("utf-8", "ignore")


def mnd_label(title):
    """从标题提取成员/版本短标签:取最后一个括号内容,否则取尾部。"""
    title = (title or "").replace(" : 뮤직앤드라마", "").strip()
    m = re.findall(r'[\(\[【]([^\(\)\[\]【】]{1,30})[\)\]】]', title)
    if m:
        return m[-1].strip()
    return title[-28:]


def mnd_fetch(idx):
    """抓一个商品:返回 {idx,title,label,stock(剩余),use_stock,onsale}。"""
    html = _get(BASE + str(idx))
    m = re.search(r'initProdStock\(\s*(true|false)\s*,\s*(\d+)', html)
    use_stock = bool(m) and m.group(1) == "true"
    stock = int(m.group(2)) if m else None
    t = re.search(r'<title>([^<]*)</title>', html)
    title = (t.group(1) if t else "").replace(" : 뮤직앤드라마", "").strip()
    soldout = "구매하기" not in html  # 粗略:无"购买"按钮视为不可买
    return {"idx": str(idx), "title": title, "label": mnd_label(title),
            "stock": stock, "use_stock": use_stock, "onsale": not soldout}


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
    """抓一个活动的所有商品,写一行批次到 track_<id>.csv。返回该活动汇总(供 index)。"""
    eid = ev["id"]
    path = os.path.join(DATADIR, f"track_{eid}.csv")
    prev = last_sales(path)
    ts = datetime.now(BJ).isoformat(timespec="seconds")
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    rows, total, members = [], 0, 0
    for idx in ev.get("products", []):
        try:
            p = mnd_fetch(idx)
        except Exception as e:
            print(f"  mnd {idx} 抓取失败: {e}")
            continue
        if p["stock"] is None:
            print(f"  mnd {idx}: 无 initProdStock(可能被拦) title='{p['title'][:40]}'")
            continue
        # 基准库存(首次见到的剩余),持久化;sold = 基准 − 当前
        bkey = str(idx)
        base = state.get(bkey)
        if base is None or p["stock"] > base:   # 没记过 或 出现补货(取更大值当基准)
            base = p["stock"]
            state[bkey] = base
        sold = max(0, base - p["stock"])
        member = ev.get("labels", {}).get(bkey) or p["label"] or ("#" + bkey)
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
    return {"eventNo": eid, "title": ev.get("name", eid), "platform": "musicndrama",
            "status": "live", "tracked": True, "total": total, "members": members,
            "hasSummary": False}


def build_mnd_index(events_summary):
    idx = {"updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "platform": {"id": "musicndrama", "name": "musicndrama (뮤직앤드라마)",
                        "supported": True, "events": events_summary}}
    json.dump(idx, open(os.path.join(DATADIR, "mnd_index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def main():
    cfg = load_json("track_config.json", {})
    mnd = cfg.get("musicndrama", {})
    events = mnd.get("events", [])
    if not events:
        print("musicndrama.events 为空")
        return
    state = load_json(STATE, {})
    summary = []
    for ev in events:
        summary.append(poll_event(ev, state))
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    build_mnd_index(summary)
    print("musicndrama 抓取完成:", [(s["title"], "total=" + str(s["total"])) for s in summary])


if __name__ == "__main__":
    main()
