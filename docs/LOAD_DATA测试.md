# LOAD DATA vs INSERT 归档测试记录 - 2026-09-02

## 测试环境
- D (3306): SSD, raw_comments ~788K行, 有 FULLTEXT 索引
- E (3307): HDD, raw_comments ~13M行, 5个二级索引 (idx_aid, idx_uid, idx_parent, idx_aid_type, idx_like_num, idx_ctime)

## 测试结果

### 空表（无数据无索引）
| 方法 | 批次 | 耗时 | 速度 |
|------|------|------|------|
| LOAD DATA | 1000 | 0.42秒 | 2396行/秒 |
| LOAD DATA | 5000 | 2.62秒 | 1909行/秒 |
| LOAD DATA | 10000 | 2.33秒 | **4300行/秒** |

### 有数据+有索引（E 13M行）
| 方法 | 批次 | 耗时 | 速度 |
|------|------|------|------|
| LOAD DATA | 10000 | 170秒 | 58行/秒 |
| INSERT executemany | 100 | 0.17秒 | 588行/秒 |

## 结论
- **空表**: LOAD DATA 比 INSERT 快 10-12 倍
- **有索引**: INSERT executemany 比 LOAD DATA 快 10 倍
- **原因**: LOAD DATA 逐行更新索引; INSERT executemany 批量更新索引
- **DROP INDEX → LOAD → REBUILD INDEX**: 在 HDD 13M 行上 REBUILD 要 15+ 分钟，不实际

## 最终方案
保持 `INSERT 100/批`，`33万行 ≈ 15分钟`，稳且无锁冲突。

## LOAD DATA 适用场景
- 新建空表导入
- 无二级索引的表
- SSD 上少量数据（<1M行）
