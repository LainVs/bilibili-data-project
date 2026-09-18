# B站弹幕/评论采集与存储项目

采集 B站视频的 **弹幕、评论** 及与之关联的 **用户 UID**，存入 MySQL。
学习/练习性质，覆盖「采集 → 清洗 → 存储 → 反查 → 增量」全流程。

## 功能

- 弹幕采集（历史分段 protobuf 解析，含发送者 `mid_hash`）
- 评论采集（wbi 签名 + 分页游标 + 一级/二级评论，直接带真实 UID）
- `mid_hash` → 真实 UID 的 **CRC32 反查**（彩虹表 + 缓存）
- MySQL 分层存储（raw 表 + 维度表 + 进度表），幂等 upsert
- 定时增量调度（APScheduler）
- 断点续传 + 完整性校验预留

## 快速开始

```bash
pip install -r requirements.txt

# 1. 配置 config/settings.ini（MySQL 密码 + B站登录 Cookie）
# 或设置环境变量 BILI_COOKIE

# 2. 初始化数据库表
python main.py init

# 3. 全量采集某视频（弹幕+评论）
python main.py crawl BV18q4y1V7pM

# 4. 对已入库弹幕执行 UID 反查
python main.py resolve BV18q4y1V7pM

# 5. 定时增量后台运行
python main.py serve
```

其他:
- `python main.py crawl --comments-only BVID` 只采评论
- `python main.py crawl --bvid-file list.txt` 批量采集
- `python main.py increment` 手动增量
- `python main.py version`

## 统计 V2（总览 + 排行榜 + 趋势洞察）

- GUI「统」面板新增「趋势洞察」页：关键词抽样 → 高频词 Top20 / 情感分布 /
  每日趋势 / 24小时分布 / 长度·点赞·IP分布，图表存 `logs/stats_charts/`，
  可导出 CSV。
- 服务层 `analysis/stats_service.py`（GUI/CLI 共用）+ 纯函数
  `analysis/text.py`（jieba 可选，缺失回退 2-gram）+ 图表
  `visualization/charts.py`（matplotlib 可选，缺失降级文字）。
- CLI：`python main.py stats --overview` 只打总览；
  `python main.py stats --keyword 烂片 --days 30 --export out.csv --charts logs/stats_charts`
- 单测（无DB）：`python -m pytest tests/test_stats_service.py -q`

## 目录结构

```
crawler/          # 采集层(api封装/清洗管道/任务编排)
storage/          # MySQL 连接/DDL/DAO
crc32_crack/      # mid_hash -> UID 反查(CRC32)
config/           # 配置
etl/analysis/visualization/   # 后续扩展
docs/             # 方案与讨论文档
tests/
logs/
```

## 注意事项

- 弹幕 UID 需经 CRC32 反查，**10 位以上 UID 可能解析失败**，属已知限制。
- 详细方案见 `docs/01-项目方案与讨论记录.md`。
- 请以合规、低频率方式使用，勿绕过平台风控。