# Ktown4u 云端自动抓取(GitHub Actions)

让 GitHub 在云端 24 小时自动抓 Ktown4u 签售销量,数据存进仓库。
你的电脑断网也没关系——有网时打开仓库就能看/下载全部历史。

## 文件结构(照原样放进仓库)
```
你的仓库/
├─ .github/workflows/track.yml   # 定时任务(每5分钟)
├─ ktown4u_ci.py                 # 抓取脚本
├─ track_config.json             # 配置:抓哪些活动
└─ data/                         # 自动生成:track_<活动号>.csv
```

## 一次性设置(约 5 分钟)

1. **建一个 GitHub 仓库**,选 **Public(公开)**。
   > 为什么公开:公开仓库的 Actions **免费不限量**;私有仓库每月只有 2000 分钟,跑不久。
   > 签售销量不是敏感数据,公开无妨。

2. **上传这三样**(保持上面的目录结构):`.github/workflows/track.yml`、`ktown4u_ci.py`、`track_config.json`。
   - 网页操作:仓库页 → Add file → Upload files,把 `cloud/` 文件夹里的内容拖进去即可
     (注意 `.github/workflows/track.yml` 这层目录别丢)。

3. 打开 **Actions** 标签页 → 如果提示,点 **“I understand my workflows, enable them”** 启用。

4. **填你要抓的活动号**:编辑 `track_config.json` 的 `events`,比如:
   ```json
   { "shopNo": 164, "interval": 10, "burstSeconds": 200, "events": [44469917, 44468447] }
   ```
   - 活动号哪来:用本地网页版 `ktown4u_app.py`「加载当前活动 / 搜索」拿到。
   - 提交保存后,下一轮就会开始抓这些活动。

完成!之后它**每 5 分钟自动跑一轮**,每轮内部每 10 秒密抓一次,数据不断追加到
`data/track_<活动号>.csv`(含 `time, member, sales, delta`)。

## 怎么看数据
- 仓库 **Code → data 文件夹** → 点开 CSV 直接看,或下载。
- 或 `git clone` / `git pull` 到本地,拿 CSV 喂给 `fansign_cut_estimate.py` 算建议区间。

## 增减/停止
- **加或删活动**:改 `track_config.json` 的 `events` 即可。
- **暂停抓取**:Actions → 左侧 `ktown4u-track` → 右上 `⋯` → **Disable workflow**。
- **彻底停**:把 `events` 设成 `[]`,或停用 workflow,或删仓库。

## 已知限制(诚实说明)
- **不是分毫不差的 10 秒**:GitHub 定时任务最细 5 分钟,且高峰期可能延迟几分钟才触发。
  所以是「每 5 分钟一轮、每轮内 10 秒密抓」拼起来的**近似连续 10 秒**覆盖,偶有小空档,正常。
- 只能抓**进行中 / 刚结束**的活动;太老的活动平台会归档,抓不到(返回空)。
- 想要真·一秒不差的连续抓,得用一直开机的云服务器(VPS),那是另一套方案。

## 参数说明(track_config.json)
| 字段 | 含义 | 默认 |
|---|---|---|
| `shopNo` | K-pop 活动通用店号 | 164 |
| `interval` | 每轮内抓取间隔(秒) | 10 |
| `burstSeconds` | 每轮密抓多长(秒,别超 ~240 以免撞下一轮) | 200 |
| `events` | 要抓的活动号数组 | — |
