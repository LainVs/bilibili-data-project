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
- `python main.py doctor` 系统全链路环境与健康体检（DB、Cookie、磁盘、跑分）
- `python main.py crack 1ad5be0d --all` 弹幕 mid_hash 毫秒级逆向破解
- `python main.py iptrace --uid 123456` 查询指定用户的 IP 属地迁徙轨迹、流转时序链与瞬移风险判定
- `python main.py iptrace --bvid BV18q4y1V7pM` 批量排查指定视频观众中的多地跳跃与代理池瞬移账号
- `python main.py detect --bvid BV18q4y1V7pM --export water_army.csv` 水军/刷屏/控评检测（复读率+短时爆发+属地聚集+异地瞬移综合打分，GUI「统计洞察」面板同步可用）
- `python main.py report --bvid BV18q4y1V7pM --output report.html` 一键生成独立单文件离线 HTML 可视化研报（支持选择性按需导出）
- `python main.py export --bvid BV18q4y1V7pM --format sqlite --output bundle.sqlite` 打包视频全量生态数据为独立便携 SQLite 数据库（无视 MySQL 环境，任何机器双击秒开）
- `python main.py export --bvid BV18q4y1V7pM --format xlsx --output bundle.xlsx` 导出带有 Claude 质感排版、冻结表头与斑马纹的多 Sheet 格式化 Excel
- `python main.py export --type comments --bvid BV18q4y1V7pM --output out.csv` 数据快速导出 (CSV / JSONL / XLSX / SQLite)
- `python main.py crawl --comments-only BVID` 只采评论
- `python main.py crawl --bvid-file list.txt` 批量采集
- `python main.py increment` 手动增量
- `python main.py version`

## 核心特性与模块

- **极速 CRC32 反查引擎 (`crc32_crack/`)**：采用双向折半（MITM）单字节逆向算法，彻底告别单向暴力穷举，1~8位数字秒级破（<10ms），9~10位毫秒级完成，自动支持哈希碰撞多候选识别。
- **ETL 管道与清洗导出 (`etl/`)**：提供文本表情包净化、URL/@清除、IP属地规范化、用户行为与视频互动聚合画像，支持 UTF-8 BOM CSV 及 JSONL 流式导出。
- **系统体检诊断 (`tools/doctor.py`)**：一键检测双库（D/E）延迟、Cookie 有效期、磁盘空间及算法引擎跑分。
- **统计 V2 与可视化 (`analysis/` + `visualization/`)**：总览 10 项指标 + 5 大排行榜导出 + 趋势洞察（高频词、情感分布、24小时活跃度、每日趋势折线图）。
- **GUI 交互优化**：支持 `b23.tv` 短链重定向自动解析、正则精确识别 BV 号、用户查询导出全部解锁、排行榜一键导出。

## 目录结构

```
crawler/          # 采集层(API封装/清洗管道/任务编排/限速风控)
storage/          # MySQL 双库管理(D写缓冲/E归档小仓库/DAO/线程复用)
crc32_crack/      # mid_hash -> UID 极速逆向破解(MITM算法)
etl/              # ETL清洗规范化、维度聚合与多格式导出(CSV/JSONL)
reporting/        # 网页研报生成引擎(零依赖单文件HTML离线研报/SVG图表)
analysis/         # 文本分析与统计服务层(高频词/情感/时段)
visualization/    # 数据图表生成(matplotlib折线/柱状图)
gui/              # 桌面客户端(Obsidian单面板风格)
tools/            # 数据库迁移、体检诊断与限速探针
config/           # 配置文件
docs/             # 设计与修复记录文档
tests/            # 单元测试集
logs/             # 运行与归档日志
```

## 注意事项

- 弹幕 UID 需经 CRC32 反查，**10 位以上 UID 可能解析失败**，属已知限制。
- 详细方案见 `docs/01-项目方案与讨论记录.md`。
- 请以合规、低频率方式使用，勿绕过平台风控。