#!/usr/bin/env python3
"""
musicndrama 工作机(在亚洲服务器/本地跑)
=========================================
musicndrama 封机房IP,GitHub(美国)抓不到。这个脚本在一台**亚洲IP**的机器上跑
(首选 Oracle 首尔免费层),每隔 MND_INTERVAL 秒抓一次 musicndrama,提交+推送到
同一个 GitHub 仓库 —— dashboard 会自动把 musicndrama 和 Ktown4u 一起显示。

要在仓库目录里跑(git clone 后)。配置:
  - mnd_events.json :要抓的活动(见 MUSICNDRAMA_SETUP.md)
  - 环境变量 MND_INTERVAL=120 (秒,默认120)
  - 环境变量 MND_PROXY=http://user:pass@host:port (可选;若服务器IP也被封,挂住宅代理)
  - git 已配置好推送权限(token)
"""
import os
import subprocess
import time
from datetime import datetime, timezone

import musicndrama_ci

INTERVAL = int(os.environ.get("MND_INTERVAL", "120"))


def push():
    subprocess.run(["git", "add", "data"], check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        msg = "mnd " + datetime.now(timezone.utc).isoformat(timespec="seconds")
        subprocess.run(["git", "commit", "-q", "-m", msg], check=False)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"], check=False)
        subprocess.run(["git", "push", "-q"], check=False)


def main():
    print(f"musicndrama worker 启动,每 {INTERVAL}s 一次"
          + (f",经代理 {os.environ.get('MND_PROXY','')[:20]}…" if os.environ.get("MND_PROXY") else ""))
    while True:
        try:
            musicndrama_ci.main()
            push()
        except Exception as e:
            print("循环出错:", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
