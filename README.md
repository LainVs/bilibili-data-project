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
# 核心依赖（采集/存储/调度/报表）
pip install -r requirements.txt

# 可选：统计面板的折线/柱状图需要 matplotlib（缺失时面板自动降级为文字+表格，不报错）
pip install matplotlib

# 可选：开发依赖（ruff + pytest + pre-commit）
pip install -r requirements-dev.txt

# 1. 配置 config/settings.ini（MySQL 密码 + B站登录 Cookie）
# 或设置环境变量 BILI_COOKIE；Cookie 也可单放 config/bili_cookie.txt

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
- `python main.py stats --overview` 总览 10 项指标；`stats --charts logs/charts` 渲染趋势图
- `python main.py iptrace --uid 123456` 查询指定用户的 IP 属地迁徙轨迹、流转时序链与瞬移风险判定
- `python main.py iptrace --bvid BV18q4y1V7pM` 批量排查指定视频观众中的多地跳跃与代理池瞬移账号
- `python main.py detect --bvid BV18q4y1V7pM --export water_army.csv` 水军/刷屏/控评检测（复读率+短时爆发+属地聚集+异地瞬移综合打分，GUI「统计洞察」面板同步可用）
- `python main.py report --bvid BV18q4y1V7pM --output report.html` 一键生成独立单文件离线 HTML 可视化研报（支持双滑块时间视窗动态缩放、多维交互式检索评论表格、情感/属地即时筛选）；加 `--no-sync` 可仅用本地数据离线生成、不联网补采
- `python main.py export --bvid BV18q4y1V7pM --format sqlite --output bundle.sqlite` 打包视频全量生态数据为独立便携 SQLite 数据库（无视 MySQL 环境，任何机器双击秒开）
- `python main.py export --bvid BV18q4y1V7pM --format xlsx --output bundle.xlsx` 导出带有 Claude 质感排版、冻结表头与斑马纹的多 Sheet 格式化 Excel
- `python main.py export --type comments --bvid BV18q4y1V7pM --output out.csv --limit 5000` 数据快速导出 (CSV / JSONL / XLSX / SQLite)
- `python main.py crawl --comments-only BVID` 只采评论
- `python main.py crawl --bvid-file list.txt` 批量采集
- `python main.py up <UID>` 按 UP 主批量采集其全部视频
- `python main.py post <链接或ID>` 采集帖子/动态/专栏评论
- `python main.py increment` 手动增量
- `python main.py version`

## 核心特性与模块

- **极速 CRC32 反查引擎 (`crc32_crack/`)**：采用双向折半（MITM）单字节逆向算法，彻底告别单向暴力穷举，1~8位数字秒级破（<10ms），9~10位毫秒级完成，自动支持哈希碰撞多候选识别。
- **ETL 管道与清洗导出 (`etl/`)**：提供文本表情包净化、URL/@清除、IP属地规范化、用户行为与视频互动聚合画像，支持 UTF-8 BOM CSV 及 JSONL 流式导出。
- **系统体检诊断 (`tools/doctor.py`)**：一键检测双库（D/E）延迟、Cookie 有效期、磁盘空间及算法引擎跑分。
- **统计 V2 与可视化 (`analysis/` + `visualization/`)**：总览 10 项指标 + 5 大排行榜导出 + 趋势洞察（高频词、情感分布、24 小时活跃度、每日趋势折线图）。
- **跨仓检索加速 (`storage/dao/search.py`)**：给归档分仓补 `idx_ctime` + 密度自适应执行计划 + 按最新时间倒序分仓并提前终止，把「搜一个词要扫 19 个仓」从分钟级压到秒级（详见下文「跨仓检索与索引策略」）。
- **GUI 交互优化**：支持 `b23.tv` 短链重定向自动解析、正则精确识别 BV 号、用户查询导出全部解锁、排行榜一键导出。
- **工程化保障**：`ruff` 静态检查 + `pre-commit` 钩子 + GitHub Actions CI（Python 3.10/3.12 跑 lint 与单测）；核心写入路径（`storage/dao`）、异步写线程与 API 客户端均有离线单元测试。

## D/E 双库归档机制

D 库是写缓冲（SSD），E 库是归档库（HDD）。GUI「连接状态」面板点「手动归档 D -> E」即把 D 全量搬到 E 的分仓库 `bilibili_<时间戳>`，验对通过后清空 D。流程：**流式导出 CSV → LOAD DATA 入分仓 → 验对（缺失即中止）→ 全局去重并集 → 建检索索引 `idx_ctime` → ANALYZE → 清 D**。

清 D 按"漂移量"（拷贝窗口期内新写入的行）自动三岔路：

| 漂移 | 策略 | 耗时量级 |
|---|---|---|
| 零漂移 | `TRUNCATE` 秒清 | 秒级 |
| ≤100 万行 | 捞出漂移 → `TRUNCATE` → 回插 | 数分钟 |
| >100 万行 | 逐批 `DELETE`（每 1000 行提交，可中断续清） | 小时级 |

要点：

- **逐批路径可随时取消**，已删部分实时提交，E 库数据始终完整，下次归档自动续清剩余；进度条与日志实时输出已删行数/速率/预计剩余，不再出现"看起来卡死"。
- **回插路径在 `TRUNCATE` 前可取消**（零副作用）；`TRUNCATE` 后必须走完回插，CSV 落盘可人工恢复，且回插后 `COUNT(*)` 验数。
- **归档期间请暂停采集**：拷贝窗口期内的写入会形成漂移，漂移小于 100 万行走回插快路径，超过则被迫逐批删除。理想状态是整个"拷贝 + 清 D"窗口零写入，此时 `TRUNCATE` 秒清。
- **`DELETE` 不释放磁盘**：`bilibili_data/*.ibd` 大小不变（空洞页会被新采集复用），目录体积主要来自 binlog。D 库 binlog 保留期已调为 7 天；维护可用 `PURGE BINARY LOGS BEFORE <日期>` 回收。

## 跨仓检索与索引策略

跨仓搜评论的形态是 `WHERE content LIKE '%词%' ORDER BY ctime DESC LIMIT n`。**分仓建表用的 DDL 里没有 `idx_ctime`**，于是 19 个库每个都要全表扫 + filesort，串行累加实测 **192 秒**。三处改动把这条路打通：

**1. 补索引（`tools/add_ctime_index.py` / `storage/archive_sync.py`）**

`idx_ctime (ctime)` 约 20 字节/行，1144 万行的仓只占 0.23 GB。归档流程改为**装载完成后**再建（InnoDB 排序批量构建远快于边 `LOAD DATA` 边维护索引），所以分仓 DDL 里刻意剔除了这一行，主库 `_COMMENT_DDL` 则保留。

**2. 密度自适应执行计划（`_plan_hint`）**

单仓 1144 万行、`LIMIT 303` 实测：

| 关键词 | 命中/采样 2 万行 | 索引反扫 | 全表扫 |
|---|---|---|---|
| `的` | 7330（36.6%） | **0.0s** | 14.6s |
| `哈哈` | 127（0.64%） | **0.1s** | 14.3s |
| 零命中词 | 0 | 49.9s | **12.9s** |

纯加索引会让稀疏词灾难性变慢 3.9 倍。更麻烦的是**优化器对 `LIKE '%x%'` 的选择率一律按 11% 估，零命中词也会自发选索引计划**，所以不能指望它，必须显式给提示词。

做法是先用 `idx_ctime` 反扫采样 2 万行数命中数（实测 **0.06s**）——这段采样区间正是索引计划最先走的那一段，衡量的就是"凑够 n 条要扫多少行"。稠密 → `FORCE INDEX(idx_ctime)`（反扫 + 早停，秒级）；稀疏 → `IGNORE INDEX(idx_ctime)`（维持全表扫，不退化）。

**3. 分仓按最新评论时间倒序 + 提前终止（`_order_shards_fresh_first` / `_can_stop_early`）**

`MAX(ctime)` 借 `idx_ctime` 反扫是 O(1)，据此把分仓按数据新鲜度倒序查询（新数据先出首屏），并在「已凑满 limit 条、且剩余仓的最新评论都早于当前第 limit 名」时直接停止。剩余仓里不可能再有任何一条挤进全局前 limit，所以这是**精确**优化而非近似。任一仓时间未知就保守不停，宁可多扫不可漏结果。

> 注意：并发查分仓对本机 HDD **无效**（实测只有 1.03x 加速，单查询反而变慢，磁头互相寻道），所以没有走多线程路线——瓶颈是物理 I/O，不是 CPU。

## 目录结构

```
main.py           # CLI 入口（参数解析 + 命令分派）
commands/         # CLI 子命令实现（basic/crawl/stats/tools/export/analyze）
crawler/          # 采集层(API封装/清洗管道/任务编排/限速风控)
storage/          # MySQL 双库管理(D写缓冲/E归档小仓库/线程复用)
  └ dao/          #   数据访问层包（_core 基础设施 / search 检索 / rankings 榜单）
crc32_crack/      # mid_hash -> UID 极速逆向破解(MITM算法)
etl/              # ETL清洗规范化、维度聚合与多格式导出(CSV/JSONL)
reporting/        # 网页研报生成引擎(零依赖单文件HTML离线研报/SVG图表)
analysis/         # 文本分析与统计服务层(高频词/情感/时段)
visualization/    # 数据图表生成(matplotlib折线/柱状图)
gui/              # 桌面客户端(Obsidian单面板风格)
tools/            # 数据库迁移、体检诊断、限速探针与 Cookie 检测
config/           # 配置文件
docs/             # 设计与修复记录文档
tests/            # 单元测试集
logs/             # 运行与归档日志
```

## 注意事项

- 弹幕 UID 需经 CRC32 反查，**10 位以上 UID 可能解析失败**，属已知限制。
- 详细方案见 `docs/01-项目方案与讨论记录.md`。
- 双库（D 写缓冲 / E 归档）的角色划分、连接回退语义与跨仓去重机制见 `docs/03-双库架构.md`。
- 请以合规、低频率方式使用，勿绕过平台风控。