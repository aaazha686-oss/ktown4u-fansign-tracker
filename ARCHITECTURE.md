# 架构 · K-pop 签售追踪框架

多平台 → 活动 → 追踪 dashboard → 结束总结。目前已接 **Ktown4u**,其它平台留接口。

## 组成
```
门户 index.html (GitHub Pages)
  读 data/index.json 渲染:平台卡片 → 在售签售卡片 → 点开活动子页(明细表 + 总结)
后端 GitHub Actions (track.yml + ktown4u_ci.py)
  发现 → 追踪(每10s) → 索引 → 结束总结
```

## 数据契约(加新平台只要照样产出这些)
| 文件 | 内容 |
|---|---|
| `data/index.json` | 门户清单:`platforms[].events[]`(eventNo/title/status/total/...) |
| `data/track_<eno>.csv` | 单活动时间序列:`time,member,sales,delta` |
| `data/summary_<eno>.json` | 单活动结束总结:各成员 total + 建议下单区间 + 大单拆解 |

## 加一个新平台(例:Yetimall)的步骤
1. 写 `<platform>_ci.py`:实现 `discover()`(列出在售签售)和 `fetch_agg(eventNo)`(返回 `{成员:销量}`),产出 `track_<eno>.csv`。
2. 在 `build_index()` 的 `platforms` 里把该平台 `supported:true`,events 填进去。
3. 门户 index.html 无需改(它纯靠 index.json 渲染)。

## 关键算法:建议下单本数
- **主(稳健)= 总盘法**:中签者人均 ≈ φ·N/S → 建议区间 `[φ_low·N/S, φ_high·N/S]`,瞄准中位偏上保高入选率。补单已含在总盘 N 里,无需区分。
- **辅(实验)= 大单拆解**:把 ≥阈值(默认10本)的单次增量当“某账号主单”,排序取前 S 估 cut/中位。
  - <阈值 的增量 = 后期补单(认真账号怕没中再加) 或 散粉买卡,**无法按账号归并**,故只作参考、不作主依据。
- 名额 S、φ、阈值都在 `track_config.json` 的 `analysis` 里配,门户页也能临时改。

## 已知平台可行性
| 平台 | 销量数据 | 状态 |
|---|---|---|
| Ktown4u | API 直接给每成员 sales | ✅ 已接 |
| Soundwave | Cafe24,use_stock=false,不公开 | ❌ 无源 |
| MakeStar | is_display_stock=false | ❌ 无源 |
| Yetimall (yettie.kr) | Next.js,待逆向 | ⏳ 待接 |
