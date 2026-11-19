#!/usr/bin/env python3
"""
签售追踪框架 · Ktown4u 抓取器(GitHub Actions)
=============================================
职责:
  1) 发现:抓 Ktown4u /event,筛出在售签售(线上/线下),自动纳入追踪
  2) 追踪:每 interval 秒抓各成员销量 -> data/track_<eno>.csv(含增量 delta)
  3) 索引:每轮更新 data/index.json(门户网页据此渲染:平台 -> 活动清单)
  4) 总结:活动一旦结束 -> data/summary_<eno>.json(总量/各成员/建议下单区间)

框架约定(以后加别的平台,只需照样产出 track_<eno>.csv + 在 index.json 里加平台块):
  data/index.json     门户清单
  data/track_<eno>.csv 单活动时间序列
  data/summary_<eno>.json 单活动结束总结

配置 track_config.json:
{
  "interval":10,"runSeconds":1500,"commitEvery":60,"discoverEvery":300,
  "ktown4u":{"shopNo":164,"autoTrackOpenFansign":true,"extraEvents":[]},
  "analysis":{"slots":30,"phiLow":0.55,"phiHigh":0.75,"bigThreshold":10}
}
"""
import csv
import io
import json
import os
import re
import signal
import ssl
import statistics
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import pending   # 断链缓冲:推不上去的行先存着,恢复后补上

BJ = timezone(timedelta(hours=8))  # 北京时间


def load_cfg():
    try:
        return json.load(open("track_config.json", encoding="utf-8"))
    except Exception:
        return {}


CFG = load_cfg()
INTERVAL = int(os.environ.get("INTERVAL", CFG.get("interval", 10)))
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", CFG.get("runSeconds", 1500)))
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY", CFG.get("commitEvery", 60)))
DISCOVER_EVERY = int(os.environ.get("DISCOVER_EVERY", CFG.get("discoverEvery", 300)))
FANME_EVERY = int(os.environ.get("FANME_EVERY", CFG.get("fanmeEvery", 10)))  # fanme 抓取间隔(秒)
GOODS_EVERY = int(os.environ.get("GOODS_EVERY", CFG.get("goodsEvery", 20)))   # k4应募/showcase 抓取间隔(秒)
NOCOMMIT = os.environ.get("NOCOMMIT") == "1"
DATADIR = "data"
GQL = "https://apis.ktown4u.com/vador/graphql"
KCFG = CFG.get("ktown4u", {})
SHOP = int(KCFG.get("shopNo", 164))
ACFG = CFG.get("analysis", {})

os.makedirs(DATADIR, exist_ok=True)
_CTX = ssl.create_default_context()
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass

_STOP = False
_IO_LOCK = threading.Lock()  # 串行化 fanme 线程写文件 vs commit_push 的 git 重写工作区
_LAST_PROGRESS = time.time()  # 主循环上次推进时间(看门狗用)
WATCHDOG_SEC = 300            # 主循环超过这么久没进展 -> 判定卡死,强制接力+退出


_FLUSH_T = None   # 收到取消信号后的存档线程


def _flush_on_stop():
    """被取消时 GitHub 只给 ~10 秒:立刻把没推上去的行存档到远端(不走 git,也就不会和新任务撞冲突)。"""
    for _ in range(2):
        if pending.flush_remote("cancelled"):
            return


def _sig(*_a):
    global _STOP, _FLUSH_T
    _STOP = True
    if _FLUSH_T is None:
        _FLUSH_T = threading.Thread(target=_flush_on_stop, daemon=True)
        _FLUSH_T.start()


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)

FANSIGN_KW = ("sign event", "fansign", "fan sign", "video call", "사인", "팬사인", "영상통화", "영통")
EXCLUDE_KW = ("cafe", "카페")


# ---------- 通用 HTTP ----------
def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
        return r.read().decode("utf-8", "ignore")


def _gql(op, query, variables, retries=25):
    body = json.dumps({"operationName": op, "query": query, "variables": variables}).encode()
    req = urllib.request.Request(f"{GQL}?operationName={op}", data=body, headers={
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
    return d


# ---------- 发现在售签售 ----------
def is_fansign(text):
    s = (text or "").lower()
    if any(x in s for x in EXCLUDE_KW):
        return False
    return any(x in s for x in FANSIGN_KW)


def event_title(eno):
    """取活动标题(用于把 extraEvents 等非签售活动也显示到门户)。"""
    q = "query event($eventRequest: EventRequest!){ event(eventRequest:$eventRequest){ eventNo title storeName } }"
    try:
        d = _gql("event", q, {"eventRequest": {"eventNo": int(eno), "shopNo": SHOP}}, retries=4)
        e = (d.get("data") or {}).get("event") or {}
        return e.get("title") or e.get("storeName") or ("#" + str(eno))
    except Exception:
        return "#" + str(eno)


def discover():
    """返回 Ktown4u 当前活动里的签售(含起止时间、是否在售)。"""
    try:
        html = _get("https://www.ktown4u.com/event")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        evs = json.loads(m.group(1))["props"]["pageProps"]["events"]
    except Exception as e:
        print("discover 失败:", e)
        return []
    now = datetime.now()
    out = []
    for e in evs:
        if not is_fansign(str(e.get("title", "")) + " " + str(e.get("description", ""))):
            continue
        try:
            sd = datetime.strptime(e["startDate"][:19], "%Y-%m-%d %H:%M:%S")
            ed = datetime.strptime(e["endDate"][:19], "%Y-%m-%d %H:%M:%S")
            live = sd <= now <= ed
        except Exception:
            ed = None
            live = False
        out.append({"eventNo": str(e["eventNo"]), "title": e.get("title", ""),
                    "description": e.get("description", ""), "start": e.get("startDate", ""),
                    "end": e.get("endDate", ""), "live": live})
    return out


# ---------- 抓销量 ----------
EP_Q = ("query eventProductsV2($request: EventProductsRequest!){ "
        "eventProductsV2(request:$request){ groups{ groupName products{ name sales } } } }")


def member_of(name):
    name = name or ""
    m = re.search(r"\(([^)]+)\)", name)
    if "Video Call" in name:
        return "VideoCall·" + (m.group(1) if m else "?")
    if "Off-Line" in name or "Off-line" in name:
        return "OffLine"
    return name[:24]


def fetch_agg(eno):
    d = _gql("eventProductsV2", EP_Q, {"request": {"eventNo": int(eno), "shopNo": SHOP}})
    if d.get("errors"):
        raise RuntimeError(d["errors"][0].get("message", "error"))
    agg = {}
    g = (d.get("data") or {}).get("eventProductsV2") or {}
    for grp in g.get("groups", []):
        for p in grp.get("products", []):
            k = member_of(p.get("name"))
            agg[k] = agg.get(k, 0) + (p.get("sales") or 0)
    return agg


def csv_path(eno):
    return os.path.join(DATADIR, f"track_{eno}.csv")


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
            row = [ts, m, v, "" if last is None else v - last]
            w.writerow(row)
            pending.add(path, row)   # 记账:推送成功前都算"未落地"
            prev[m] = v


# ---------- 结束总结(含建议下单区间)----------
def compute_summary(eno, meta):
    """从 CSV 算总结:各成员总量 + 建议区间(总盘法 φ·N/S)+ 大单拆解(实验)。"""
    path = csv_path(eno)
    if not os.path.exists(path):
        return None
    by_member_total = {}
    big_orders = {}  # member -> [delta>=threshold]
    slots = int(ACFG.get("slots", 30))
    plo = float(ACFG.get("phiLow", 0.55))
    phi = float(ACFG.get("phiHigh", 0.75))
    thr = int(ACFG.get("bigThreshold", 10))
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            m = row["member"]
            try:
                by_member_total[m] = int(float(row["sales"]))  # 最后一次即最终
            except (TypeError, ValueError):
                pass
            d = row.get("delta", "")
            if d not in ("", None):
                try:
                    dv = int(float(d))
                    if dv >= thr:
                        big_orders.setdefault(m, []).append(dv)
                except ValueError:
                    pass
    members = []
    for m, N in sorted(by_member_total.items(), key=lambda x: -x[1]):
        rec = {"N": N, "slots": slots, "phiLow": plo, "phiHigh": phi,
               "rangeLow": round(plo * N / slots), "rangeHigh": round(phi * N / slots)}
        # 大单拆解:把 >=阈值 的增量当“主单”,排序取前 slots
        bo = sorted(big_orders.get(m, []), reverse=True)
        decomp = {"threshold": thr, "bigOrderCount": len(bo)}
        if bo:
            decomp["cutTopN"] = bo[min(slots, len(bo)) - 1]
            decomp["medianTopN"] = statistics.median(bo[:slots])
        members.append({"member": m, "total": N, "recommend": rec, "deltaDecomp": decomp})
    return {
        "eventNo": str(eno), "platform": "ktown4u",
        "title": meta.get("title", ""), "end": meta.get("end", ""),
        "computedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totalAllMembers": sum(by_member_total.values()),
        "params": {"slots": slots, "phiLow": plo, "phiHigh": phi, "bigThreshold": thr,
                   "note": "建议区间=φ·N/S(主, 稳健); deltaDecomp=大单(>=阈值)拆解(实验, 偏估); "
                           "小于阈值的增量含‘后期补单’与‘散粉买卡’两类,无法区分账号,故只作参考"},
        "members": members,
    }


# ---------- 索引(门户用)----------
def load_index():
    try:
        return json.load(open(os.path.join(DATADIR, "index.json"), encoding="utf-8"))
    except Exception:
        return {"platforms": []}


def build_index(discovered, tracked):
    """合并:发现的签售 + 正在追踪的 + 已有总结。保留历史(结束的也留着)。"""
    idx = load_index()
    # 取出旧的 ktown4u 活动,做合并(按 eventNo)
    old_events = {}
    for p in idx.get("platforms", []):
        if p.get("id") == "ktown4u":
            for ev in p.get("events", []):
                old_events[ev["eventNo"]] = ev
    now = datetime.now()
    # 只保留:仍在追踪的 / 有总结的 / 已结束的真签售;其余(如取消追踪的非签售)清掉
    # 保留:自动发现过的(autofound,签售结束也永久留着)/ 在追踪的 / 有总结的。
    # 只清掉:手动加过又删掉的非签售(没 autofound 标记)。
    def _keep(eno, ev):
        return (ev.get("autofound") or eno in tracked or ev.get("hasSummary")
                or os.path.exists(os.path.join(DATADIR, f"summary_{eno}.json")))
    by_no = {eno: ev for eno, ev in old_events.items() if _keep(eno, ev)}
    for d in discovered:
        eno = d["eventNo"]
        ev = by_no.get(eno, {})
        ev.update({"eventNo": eno, "title": d["title"], "description": d["description"],
                   "start": d["start"], "end": d["end"], "autofound": True})
        # 状态
        try:
            ed = datetime.strptime(d["end"][:19], "%Y-%m-%d %H:%M:%S")
            ev["status"] = "live" if d["live"] else ("ended" if now > ed else "upcoming")
        except Exception:
            ev["status"] = "live" if d["live"] else "ended"
        ev["tracked"] = eno in tracked
        by_no[eno] = ev
    # 额外追踪的(extraEvents 等非签售)也加进索引,否则门户看不到
    for eno in tracked:
        if eno not in by_no:
            by_no[eno] = {"eventNo": eno, "title": event_title(eno), "status": "live", "tracked": True}
    # 标记总结是否存在、最新总量
    for eno, ev in by_no.items():
        ev["hasSummary"] = os.path.exists(os.path.join(DATADIR, f"summary_{eno}.json"))
        prev = last_sales(csv_path(eno))
        if prev:
            ev["total"] = sum(prev.values())
            ev["members"] = len(prev)
        # 兜底状态:end 过了就算 ended
        try:
            ed = datetime.strptime((ev.get("end") or "")[:19], "%Y-%m-%d %H:%M:%S")
            if now > ed:
                ev["status"] = "ended"
        except Exception:
            pass
    events = sorted(by_no.values(), key=lambda e: (e.get("status") != "live", e.get("end", "")), reverse=False)
    platforms = [
        {"id": "ktown4u", "name": "Ktown4u", "supported": True, "events": events},
        {"id": "soundwave", "name": "Soundwave", "supported": False, "reason": "平台(Cafe24)不公开销量/应募数", "events": []},
        {"id": "yetimall", "name": "Yetimall (yettie.kr)", "supported": False, "reason": "待接入(Next.js 站,需逆向接口)", "events": []},
        {"id": "makestar", "name": "MakeStar", "supported": False, "reason": "is_display_stock=false,不公开销量", "events": []},
    ]
    idx = {"updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "platforms": platforms}
    json.dump(idx, open(os.path.join(DATADIR, "index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return idx


# ---------- git ----------
def _successor_started(repo, pat, since_epoch):
    """查是否有在 since 之后创建、仍在 queued/in_progress 的 run(即接力真拉起了)。"""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/actions/workflows/track.yml/runs?per_page=5",
            headers={"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            runs = json.loads(r.read()).get("workflow_runs", [])
        for run in runs:
            if run.get("status") in ("queued", "in_progress"):
                ct = datetime.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc).timestamp()
                if ct >= since_epoch - 10:
                    return True
    except Exception as e:
        print("查接力状态失败:", e)
    return False


def dispatch_next():
    """任务正常结束前,用 PAT 触发下一个运行,并**验证**它真的起来了。
    只返回 204 不代表 run 真被创建(GitHub 偶发),所以派发后轮询确认,没起来就重试,
    堵住'干净结束但接力没拉起 -> 几十分钟空档'这个坑。"""
    pat = os.environ.get("PAT", "")
    repo = os.environ.get("REPO", "")
    if not (pat and repo):
        print("无 PAT,跳过自我接力(改靠 schedule 兜底)")
        return
    for rnd in range(4):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/actions/workflows/track.yml/dispatches",
                data=json.dumps({"ref": "main"}).encode(), method="POST",
                headers={"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"})
            urllib.request.urlopen(req, timeout=20)
            print(f"✅ 已派发接力(第{rnd+1}轮),验证新任务是否起来…")
        except Exception as e:
            print(f"派发失败(第{rnd+1}轮):{e}")
            time.sleep(5)
            continue
        # 验证最多 ~60s:被新任务取消(_STOP)也算成功
        for _ in range(12):
            if _STOP:
                print("✅ 接力已拉起(本任务被新任务取代)")
                return
            time.sleep(5)
            if _successor_started(repo, pat, t0):
                print("✅ 接力确认:新任务已在 队列/运行 中")
                return
        print(f"⚠️ 第{rnd+1}轮未见新任务,重试派发…")
    print("⚠️ 接力4轮都没拉起,靠 schedule 兜底")
    print("⚠️ 自我接力5次都失败,靠 schedule 兜底")


_LAST_PUSH_OK = time.time()   # 上次成功推送时间(看门狗用)
PUSH_STALE_SEC = 600          # 超过这么久没推送成功 -> 视为"活着但推不上去",同样自愈


def _watchdog():
    """看门狗,两种卡死都管:
    1) 主循环 >WATCHDOG_SEC 没推进(卡在网络调用/git);
    2) 主循环在转,但 >PUSH_STALE_SEC 没成功推送(如 git 卡在冲突/游离 HEAD)
       —— 这才是多次~半小时断更的真凶:进程活着,数据却一直推不上去。
    触发后接力新任务(全新 checkout)并硬退出。"""
    while not _STOP:
        time.sleep(20)
        stalled = time.time() - _LAST_PROGRESS
        unpushed = time.time() - _LAST_PUSH_OK
        why = None
        if stalled > WATCHDOG_SEC:
            why = f"主循环 {int(stalled)}s 无进展"
        elif unpushed > PUSH_STALE_SEC and not NOCOMMIT:
            why = f"{int(unpushed)}s 没成功推送"
        if why:
            print(f"⚠️ 看门狗:{why},判定卡死 -> 存档未推送的行 -> 接力+强制退出")
            try:
                pending.flush_remote("watchdog")   # 先把抓到但没推上去的存起来,新任务会补上
            except Exception as e:
                print("看门狗存档失败:", e)
            try:
                dispatch_next()
            except Exception as e:
                print("看门狗接力失败:", e)
            os._exit(1)   # 硬退出,绕过卡死的线程


def _git(args, timeout=60):
    """跑 git 命令,带超时;超时/出错都不抛(避免卡死整个抓取进程)。"""
    try:
        return subprocess.run(["git", *args], check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"git {args[0]} 超时{timeout}s,跳过本次")
        return None


def _ok(r):
    return r is not None and r.returncode == 0


def _repair_git_state():
    """修复上次失败留下的坏状态:残留的 rebase、游离 HEAD。否则之后每次提交都会失败。"""
    if os.path.isdir(".git/rebase-merge") or os.path.isdir(".git/rebase-apply"):
        print("🔧 修复:中止残留的 rebase")
        _git(["rebase", "--abort"], timeout=30)
    if subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 0:
        print("🔧 修复:游离 HEAD -> 把当前提交挂回 main")
        _git(["checkout", "-q", "-B", "main"], timeout=30)


def _setup_union_merge():
    """CSV 都是只追加的时间序列:两边同时追加时 union 合并 = 两边的行都保留(之后再排序去重)。
    写在 .git/info/attributes(只影响本次 checkout,不用改仓库文件)。"""
    try:
        os.makedirs(".git/info", exist_ok=True)
        p, line = ".git/info/attributes", "data/*.csv merge=union\n"
        cur = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        if line not in cur:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        print("设置 union 合并失败:", e)


def _commit_if_changed(msg):
    chk = _git(["diff", "--cached", "--quiet"], timeout=30)
    if chk is not None and chk.returncode != 0:
        _git(["commit", "-q", "-m", msg + " " + datetime.now(timezone.utc).isoformat(timespec="seconds")],
             timeout=30)
        return True
    return False


def _merge_remote_csvs_into_worktree():
    """兜底路径专用:对齐远端前,把远端 CSV 里本地没有的行并进本地文件,避免"用本地覆盖"时丢掉别人的行。"""
    out = subprocess.run(["git", "diff", "--name-only", "origin/main", "--", "data"],
                         capture_output=True, text=True).stdout.split()
    for p in out:
        if p.endswith(".csv") and os.path.exists(p) and "/pending/" not in p:
            txt = subprocess.run(["git", "show", f"origin/main:{p}"], capture_output=True,
                                 text=True, encoding="utf-8").stdout.lstrip("﻿")
            pending.normalize(p, list(csv.reader(io.StringIO(txt)))[1:])


def commit_push():
    global _LAST_PUSH_OK
    if NOCOMMIT:
        return
    with _IO_LOCK:  # 防止抓取线程在 git rebase 重写工作区时写文件
        snap = pending.snapshot()        # 账上"还没推上去"的行
        _repair_git_state()
        pending.reconcile(snap)          # 修复(rebase --abort)可能抹掉了没提交的行 -> 补回
        _git(["add", "data"], timeout=30)
        _commit_if_changed("track")
        # 变基到远端:CSV 走 union(两边行都留),json 索引走 -X theirs(本任务最新生成的为准)。
        if not _ok(_git(["pull", "--rebase", "--autostash", "-X", "theirs", "-q", "origin", "main"],
                        timeout=90)):
            print("⚠️ pull --rebase 失败,改为:中止 -> 合并远端行 -> 对齐远端 -> 提交本地数据")
            _git(["rebase", "--abort"], timeout=30)
            _repair_git_state()
            pending.reconcile(snap)
            if _ok(_git(["fetch", "-q", "origin", "main"], timeout=90)):
                _merge_remote_csvs_into_worktree()
                _git(["reset", "-q", "--soft", "origin/main"], timeout=30)
                _git(["reset", "-q", "origin/main", "--", "."], timeout=30)   # 暂存区先对齐远端(不碰代码文件)
                _git(["add", "data"], timeout=30)                             # 只提交数据
                _commit_if_changed("track(resync)")
        # 补上:远端存档(上个任务断链时存的)+ 账上的行;再整理 union 合并后的顺序
        nfiles, nrows = pending.merge_pending_files()
        nrows += pending.reconcile(snap)
        pending.tidy(snap.keys())
        _git(["add", "data"], timeout=30)
        if _commit_if_changed(f"backfill 补回{nrows}行(存档{nfiles}个)" if (nfiles or nrows) else "track(tidy)"):
            if nfiles or nrows:
                print(f"🩹 已补回断链期间的 {nrows} 行(来自 {nfiles} 个存档)")
        if _ok(_git(["push", "-q", "origin", "HEAD:main"], timeout=60)):
            _LAST_PUSH_OK = time.time()
            pending.clear(snap)          # 推上去了才销账
        else:
            print(f"⚠️ push 失败,下轮重试(账上 {pending.count()} 行未落地)")


_DISCOVERED = []
_TRACKED = set()


def refresh_discovery_and_index():
    """发现在售签售、决定追踪谁、刷新 index、给新结束的活动出总结。"""
    global _DISCOVERED, _TRACKED
    discovered = discover()
    if discovered:
        _DISCOVERED = discovered
    auto = KCFG.get("autoTrackOpenFansign", True)
    extra = [str(e) for e in KCFG.get("extraEvents", [])]
    tracked = set(extra)
    if auto:
        tracked |= {d["eventNo"] for d in (_DISCOVERED) if d["live"]}
    _TRACKED = tracked
    idx = build_index(_DISCOVERED, tracked)
    # 给已结束、还没总结的活动生成总结
    for p in idx["platforms"]:
        if p.get("id") != "ktown4u":
            continue
        for ev in p.get("events", []):
            if ev.get("status") == "ended" and not ev.get("hasSummary") and os.path.exists(csv_path(ev["eventNo"])):
                s = compute_summary(ev["eventNo"], ev)
                if s:
                    json.dump(s, open(os.path.join(DATADIR, f"summary_{ev['eventNo']}.json"), "w", encoding="utf-8"),
                              ensure_ascii=False, indent=1)
                    print("已生成总结:", ev["eventNo"])
    return sorted(tracked)


def _fanme_thread():
    """fanme 独立线程:每 FANME_EVERY 秒抓一次,不受 k4 的 CACHE_LOADING 阻塞。
    k4 主循环一轮可能要 ~75s(3活动×最多25次缓存重试),所以 fanme 必须独立跑才能真10秒。"""
    import fanme_ci
    while not _STOP:
        t0 = time.time()
        try:
            with _IO_LOCK:    # 与 commit_push 互斥,避免 git rebase 时写文件
                fanme_ci.main()   # 11商品并发抓取(~1s),写 CSV + fanme_index.json
        except Exception as e:
            print("fanme 抓取出错:", e)
        # 睡到下一个 FANME_EVERY 边界,期间可被 _STOP 打断
        while not _STOP and time.time() - t0 < FANME_EVERY:
            time.sleep(0.5)


def _goods_thread():
    """k4 应募/showcase 独立线程:每 GOODS_EVERY 秒抓一次(商品页209KB较重,默认20s),
    不受 k4 主循环 CACHE_LOADING(~75s/轮)阻塞。"""
    import k4goods_ci
    while not _STOP:
        t0 = time.time()
        try:
            with _IO_LOCK:    # 与 commit_push 互斥
                k4goods_ci.main()   # 写 track_g<N>.csv + goods_index.json
        except Exception as e:
            print("k4goods 抓取出错:", e)
        while not _STOP and time.time() - t0 < GOODS_EVERY:
            time.sleep(0.5)


def main():
    global _LAST_PROGRESS
    _LAST_PROGRESS = time.time()
    # 看门狗最先起,覆盖启动发现阶段。(反复断更的真因是 git 数据冲突卡住推送,见 commit_push / _watchdog)
    threading.Thread(target=_watchdog, daemon=True).start()
    _setup_union_merge()
    tracked = refresh_discovery_and_index()
    _LAST_PROGRESS = time.time()   # 启动发现可能慢,完成后立即打点
    print("本段追踪:", tracked)
    prev = {e: last_sales(csv_path(e)) for e in tracked}
    end = time.time() + RUN_SECONDS
    last_commit = last_discover = time.time()
    polls = 0
    # fanme 用独立线程跑(真10秒),k4 在主循环跑(受API缓存影响~75秒)
    ft = threading.Thread(target=_fanme_thread, daemon=True)
    ft.start()
    threading.Thread(target=_goods_thread, daemon=True).start()  # k4 应募/showcase 独立快抓
    while time.time() < end and not _STOP:
        _LAST_PROGRESS = time.time()   # 每轮开始打点,告诉看门狗主循环还活着
        ts = datetime.now(BJ).isoformat(timespec="seconds")  # 北京时间记录
        for e in tracked:
            try:
                append_rows(csv_path(e), ts, fetch_agg(e), prev[e])
            except Exception as ex:
                print(f"[{ts}] event {e}: {ex}")
            _LAST_PROGRESS = time.time()   # 每个活动抓完也打点(k4 可能慢)
        polls += 1
        if time.time() - last_commit >= COMMIT_EVERY:
            try:
                import musicndrama_ci
                musicndrama_ci.main()   # 顺带抓 musicndrama(写自己的 CSV + mnd_index.json)
            except Exception as e:
                print("musicndrama 抓取出错:", e)
            build_index(_DISCOVERED, _TRACKED)  # 用最新 CSV 刷新索引里的总量(不发请求)
            commit_push()
            last_commit = time.time()
            _LAST_PROGRESS = time.time()   # 提交后打点(commit/push 可能慢)
        if time.time() - last_discover >= DISCOVER_EVERY:
            new_tracked = refresh_discovery_and_index()
            for e in new_tracked:
                prev.setdefault(e, last_sales(csv_path(e)))
            if new_tracked != tracked:
                print("追踪列表更新:", new_tracked)
                tracked = new_tracked
            last_discover = time.time()
        if not _STOP:
            time.sleep(INTERVAL)
    if _STOP:
        # 被取消:不再抢着 git 推送(那正是新旧任务撞冲突的来源),
        # 未推上去的行已由 _flush_on_stop 存档到 data/pending/,新任务会补上。
        if _FLUSH_T is not None:
            _FLUSH_T.join(timeout=8)
        print(f"本段被取消:{polls} 轮,未落地 {pending.count()} 行(已尝试存档)")
        return
    commit_push()
    if pending.count():     # 到点了还有没推上去的 -> 存档,别跟着这台机器一起丢
        pending.flush_remote("end-unpushed")
    dispatch_next()         # 正常到点结束 -> 自我接力
    print(f"本段结束:{polls} 轮")


if __name__ == "__main__":
    main()
