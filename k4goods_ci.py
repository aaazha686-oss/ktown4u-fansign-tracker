#!/usr/bin/env python3
"""
Ktown4u 应募(FAN SHOWCASE / 按 goods_no 追踪)抓取器
=====================================================
有些 k4 活动(如 FAN SHOWCASE 应募)页面上隐藏了销量(sellQuantityHide=true),
但商品数据里的 productDetails.quantity 是**负库存**:预售应募从 0 往下减,
已应募数 = -quantity。虽然前端隐藏,__NEXT_DATA__ 里照样有,可实时读。

跟 eventProductsV2(按 eventNo)不同,这是**按 goods_no**追踪(单商品/单池)。
产出 data/track_g<goods_no>.csv(time, member, sales, delta) + data/goods_index.json。
配置:track_config.json 的 "ktown4u"."goods" 列表:
  [{"goodsNo":172409,"shopNo":174,"name":"...","label":"EXO-KASE","ended":false}]
已结束的加 "ended":true 即停抓、保留。
"""
import csv
import json
import os
import re
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
DATADIR = "data"
HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": "https://kr.ktown4u.com/",
}
os.makedirs(DATADIR, exist_ok=True)
_CTX = ssl.create_default_context()
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass
_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def fetch_goods(goods_no):
    """返回 (applications, saleStatus, name)。applications = max(0, -quantity)。"""
    url = f"https://kr.ktown4u.com/iteminfo?goods_no={goods_no}"
    req = urllib.request.Request(url, headers=HDRS)
    with _OPENER.open(req, timeout=25) as r:
        html = r.read().decode("utf-8", "ignore")
    m = _NEXT.search(html)
    if not m:
        raise RuntimeError("无 __NEXT_DATA__")
    pd = json.loads(m.group(1))["props"]["pageProps"]["productDetails"]
    q = pd.get("quantity")
    apps = max(0, -int(q)) if isinstance(q, (int, float)) else 0
    return apps, bool(pd.get("saleStatus")), pd.get("productName") or str(goods_no)


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


def eid_of(g):
    return "g" + str(g["goodsNo"])


def poll_one(g):
    eid = eid_of(g)
    path = os.path.join(DATADIR, f"track_{eid}.csv")
    label = g.get("label") or str(g["goodsNo"])
    prev = last_sales(path)
    try:
        apps, live, name = fetch_goods(g["goodsNo"])
    except Exception as e:
        print(f"  goods {g['goodsNo']} 抓取失败: {e}")
        # 失败就用历史值构建条目,别让它从列表消失
        total = prev.get(label, 0)
        return {"eventNo": eid, "title": g.get("name", label), "platform": "ktown4u",
                "status": "live", "tracked": True, "total": total, "members": 1,
                "hasSummary": False, "kind": "showcase"}
    ts = datetime.now(BJ).isoformat(timespec="seconds")
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    last = prev.get(label)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "member", "sales", "delta"])
        w.writerow([ts, label, apps, "" if last is None else apps - last])
    return {"eventNo": eid, "title": g.get("name", name), "platform": "ktown4u",
            "status": "live", "tracked": True, "total": apps, "members": 1,
            "hasSummary": False, "kind": "showcase"}


def ended_entry(g):
    eid = eid_of(g)
    prev = last_sales(os.path.join(DATADIR, f"track_{eid}.csv"))
    label = g.get("label") or str(g["goodsNo"])
    return {"eventNo": eid, "title": g.get("name", label), "platform": "ktown4u",
            "status": "ended", "tracked": False, "total": prev.get(label, 0),
            "members": 1, "hasSummary": False, "kind": "showcase", "end": g.get("end", "")}


def load_goods():
    g = load_json("track_config.json", {}).get("ktown4u", {}).get("goods", [])
    return g if isinstance(g, list) else []


def main():
    goods = load_goods()
    if not goods:
        return
    print(f"[k4goods] 启动 goods={len(goods)}")
    live = [g for g in goods if not g.get("ended")]
    with ThreadPoolExecutor(max_workers=min(8, len(live) or 1)) as ex:
        polled = list(ex.map(poll_one, live))
    summary = polled + [ended_entry(g) for g in goods if g.get("ended")]
    idx = {"updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "events": summary}
    json.dump(idx, open(os.path.join(DATADIR, "goods_index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("k4goods 完成:", [(s["title"][:30], "apps=" + str(s["total"])) for s in summary])


if __name__ == "__main__":
    main()
