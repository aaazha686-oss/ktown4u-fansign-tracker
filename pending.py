#!/usr/bin/env python3
"""
断链缓冲:抓到了但还没成功推上 GitHub 的行,先存着,恢复后补上
===================================================================
1) 记账  add(path, row):每个抓取器写一行 CSV 时同时记一笔;推送成功后 clear() 销账。
2) 存档  flush_remote():任务要死了(被取消/看门狗重启/到点推送失败)还有没销账的行,
         绕开 git,直接用 GitHub Contents API 写成 data/pending/<run>-<ms>.csv(文件名唯一,不会冲突)。
3) 补上  merge_pending_files():下一个任务提交时把 data/pending/*.csv 并回原 CSV,
         按时间排序、按 (时间,成员) 去重、重算增量,然后删掉存档。
4) 自检  reconcile(snap):git 修复(rebase --abort 等)可能抹掉工作区里没提交的行,
         提交前把账上的行补回文件。

只能补"任务活着、抓到了、但推不上去"的空档。没有任务在跑的时段没有数据可补
(数据源只给当前数、不给历史)。
"""
import base64
import csv
import glob
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime

DATADIR = "data"
PENDDIR = os.path.join(DATADIR, "pending")
HEADER = ["time", "member", "sales", "delta"]
_TARGET_OK = re.compile(r"^data/track_[A-Za-z0-9_\-]+\.csv$")

_LOCK = threading.Lock()
_ROWS = {}   # path -> {(time, member): [time, member, sales, delta]}


# ---------- 记账 ----------
def add(path, row):
    path = path.replace(os.sep, "/")
    r = ["" if x is None else str(x) for x in row]
    with _LOCK:
        _ROWS.setdefault(path, {})[(r[0], r[1])] = r


def snapshot():
    with _LOCK:
        return {p: dict(d) for p, d in _ROWS.items()}


def clear(snap):
    """只销掉快照里的(推送期间新抓的保留)。"""
    with _LOCK:
        for p, d in snap.items():
            cur = _ROWS.get(p)
            if not cur:
                continue
            for k in d:
                cur.pop(k, None)
            if not cur:
                _ROWS.pop(p, None)


def count():
    with _LOCK:
        return sum(len(d) for d in _ROWS.values())


# ---------- CSV 合并/整理 ----------
def _parse_time(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _read_rows(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.reader(f)):
            if i == 0 and r[:1] == ["time"]:
                continue
            if len(r) >= 3 and _parse_time(r[0]) is not None:   # 跳过表头/冲突标记等坏行
                rows.append((r + [""])[:4])
    return rows


def normalize(path, extra=()):
    """把 extra 并入 path:按 (时间,成员) 去重(已有的优先)、按时间排序、重算每个成员的增量。
    返回新增行数。文件已有序且无新增时不重写。"""
    rows = _read_rows(path)
    seen = {(r[0], r[1]) for r in rows}
    added = 0
    for r in extra:
        r = ["" if x is None else str(x) for x in r][:4]
        if len(r) < 3 or _parse_time(r[0]) is None or (r[0], r[1]) in seen:
            continue
        rows.append((r + [""])[:4])
        seen.add((r[0], r[1]))
        added += 1
    times = [_parse_time(r[0]) for r in rows]
    is_sorted = all(a <= b for a, b in zip(times, times[1:]))
    order = sorted(range(len(rows)), key=lambda i: times[i])   # 稳定排序,同一时刻保持原顺序
    rows = [list(rows[i]) for i in order]
    # 按时间重算每个成员的增量(两个任务各自算的增量合并后会对不上,补回的行也要重算)
    deltas_ok = True
    last = {}
    for r in rows:
        try:
            v = int(float(r[2]))
        except ValueError:
            continue
        want = "" if r[1] not in last else str(v - last[r[1]])
        if r[3] != want:
            deltas_ok = False
            r[3] = want
        last[r[1]] = v
    if added == 0 and is_sorted and len(seen) == len(rows) and deltas_ok:
        return 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    os.replace(tmp, path)
    return added


def reconcile(snap):
    """确保账上的行都在文件里(git 修复可能抹掉了没提交的行)。返回补回的行数。"""
    n = 0
    for p, d in snap.items():
        if os.path.exists(p) or d:
            n += normalize(p, list(d.values()))
    return n


def tidy(paths):
    """union 合并后行序可能乱/有重复:整理一下(已有序则不动)。"""
    for p in paths:
        if os.path.exists(p):
            normalize(p)


def merge_pending_files():
    """把 data/pending/*.csv 并回原表并删掉存档。返回 (合并的存档数, 补回的行数)。"""
    files = sorted(glob.glob(os.path.join(PENDDIR, "*.csv")))
    done, rows_added = 0, 0
    for fp in files:
        try:
            by_path = {}
            with open(fp, encoding="utf-8", newline="") as f:
                for r in csv.reader(f):
                    if len(r) >= 4 and _TARGET_OK.match(r[0]):
                        by_path.setdefault(r[0], []).append(r[1:5])
            for p, rows in by_path.items():
                rows_added += normalize(p, rows)
            os.remove(fp)
            done += 1
        except Exception as e:
            print(f"合并存档 {fp} 失败(下轮再试): {e}")
    return done, rows_added


# ---------- 存档到远端(绕开 git)----------
def flush_remote(reason, timeout=8):
    """把还没销账的行用 Contents API 写成 data/pending/<run>-<ms>.csv。成功后销账。"""
    snap = snapshot()
    n = sum(len(d) for d in snap.values())
    if n == 0:
        return True
    repo = os.environ.get("REPO", "")
    tokens = [t for t in (os.environ.get("GITHUB_TOKEN", ""), os.environ.get("PAT", ""), _checkout_token()) if t]
    if not (tokens and repo):
        print(f"无可用 token,无法存档 {n} 行")
        return False
    lines = []
    for p, d in snap.items():
        for r in d.values():
            lines.append([p] + r)
    import io
    buf = io.StringIO()
    csv.writer(buf).writerows(lines)
    name = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{int(time.time() * 1000)}.csv"
    body = {"message": f"pending: 断链存档 {n} 行 ({reason})",
            "content": base64.b64encode(buf.getvalue().encode("utf-8")).decode(),
            "branch": "main"}
    for tok in tokens:   # 依次试:工作流自带 token(有写文件权限)-> PAT -> checkout 留下的凭据
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/contents/data/pending/{name}",
                data=json.dumps(body).encode(), method="PUT",
                headers={"Authorization": f"token {tok}", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status in (200, 201):
                    clear(snap)
                    print(f"💾 已存档 {n} 行 -> data/pending/{name} ({reason}),下个任务会补上")
                    return True
        except Exception as e:
            print(f"存档尝试失败({reason}): {e}")
    return False


def _checkout_token():
    """actions/checkout 会把工作流 token 存在 git 配置的 extraheader 里;没配 GITHUB_TOKEN 环境变量时用它兜底。"""
    try:
        import subprocess
        out = subprocess.run(["git", "config", "--get-regexp", r"http\..*\.extraheader"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"basic\s+(\S+)", out, re.I)
        if m:
            user_tok = base64.b64decode(m.group(1)).decode()
            return user_tok.split(":", 1)[1]
    except Exception:
        pass
    return ""
