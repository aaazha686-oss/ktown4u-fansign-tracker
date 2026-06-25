# musicndrama 部署(亚洲服务器,绕开机房IP封锁)

musicndrama 封数据中心IP,GitHub(美国)抓不到。所以 musicndrama 这一个平台要在
**一台亚洲IP的机器**上跑,数据推到**同一个仓库**,dashboard 会自动一起显示。
Ktown4u 那套仍在 GitHub 云端 24 小时跑,**不受影响**。

## 推荐:Oracle Cloud 首尔免费层(永久免费 + 韩国IP,最可能过)

### 第一步:开一台免费 VM
1. 注册 Oracle Cloud(cloud.oracle.com),Home Region 选 **Seoul(首尔)**。
2. 建 **Always Free** 的 VM 实例(Ampere/A1 或 Micro 都行),系统选 **Ubuntu**。
3. 拿到它的公网IP + SSH 登录。

### 第二步:在 VM 上装好并克隆仓库
```bash
sudo apt update && sudo apt install -y git python3 python3-pip
pip3 install certifi
git clone https://<你的token>@github.com/aaazha686-oss/ktown4u-fansign-tracker.git
cd ktown4u-fansign-tracker
git config user.name "mnd-worker"; git config user.email "mnd@local"
```
> `<你的token>` 用你的 GitHub token(和云端那套同一个,有 repo 权限)。

### 第三步:先测这台机器的IP能不能抓 musicndrama
```bash
python3 -c "import musicndrama_ci as m; print(m.mnd_fetch(10693))"
```
- 看到 `'stock': 317`(或别的数字)→ ✅ **这台IP能抓,继续第四步**。
- 看到 `'stock': None` 或报错 → ❌ 这台IP也被封,需挂**住宅代理**:
  注册一个住宅代理(如 IPRoyal / Webshare 住宅套餐),拿到 `http://user:pass@host:port`,
  之后所有命令前加 `export MND_PROXY="http://user:pass@host:port"`,再测一次。

### 第四步:配置要抓的活动
```bash
cp mnd_events.example.json mnd_events.json
nano mnd_events.json   # 改成你要追踪的签售:每个成员一个商品 idx
```
> idx 怎么找:打开 musicndrama 商品页,URL 里 `?idx=数字` 就是。每个成员/版本是单独商品。

### 第五步:常驻运行(关掉SSH也继续)
```bash
# 简单方式:tmux 里跑
sudo apt install -y tmux
tmux new -s mnd
export MND_INTERVAL=120          # 每120秒抓一次(库存变化慢,不用太快)
# 若用代理:export MND_PROXY="http://user:pass@host:port"
python3 musicndrama_worker.py
# 按 Ctrl+B 再按 D 退出 tmux(脚本继续跑)。重进:tmux attach -t mnd
```

完成!之后打开 dashboard,musicndrama 平台块就会出现(和 Ktown4u 并列)。

## 备选:任意 $5/月 亚洲 VPS(Vultr/DigitalOcean 首尔/新加坡)
步骤一样,只是 VM 来源不同。**先做第三步测IP**——亚洲机房IP也可能被封,被封就挂住宅代理。

## 说明
- musicndrama 给的是**剩余库存**,脚本折算成「已售 = 首次看到的库存 − 当前」,所以
  刚开始 sales=0,之后随下单递增(和 Ktown4u 同语义,delta=增量)。
- 这台机器只负责 musicndrama;Ktown4u 仍在 GitHub 云端跑,两者数据进同一仓库、同一 dashboard。
- `mnd_events.json` 只存在这台机器上(不进仓库),所以 GitHub 云端不会去抓 musicndrama。
