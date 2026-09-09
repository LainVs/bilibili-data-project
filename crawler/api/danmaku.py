"""B站弹幕采集。

数据源双通道：
  A. 历史归档弹幕 /x/v2/dm/web/history/seg.so（protobuf，逐日拉取）
     - 归档弹幕才有"完整历史"，逐日请求保证完整性
     - 注意：近期（约最近几天）的弹幕还未归档，此接口查不到
  B. 实时弹幕池 /x/v1/dm/list.so（XML）
     - 返回当前弹幕池保留的最近弹幕（上限约 1500 条以内）
     - 用于补齐近期视频尚未归档的部分
两通道并用，按弹幕 id 幂等写库（raw_danmaku 主键 id）去重。
"""
from __future__ import annotations

import datetime
import logging
import xml.etree.ElementTree as ET

from crawler.api.client import ApiClient

logger = logging.getLogger(__name__)

# 历史弹幕接口（protobuf 分段，用日期区分每天）
HISTORY_API = "https://api.bilibili.com/x/v2/dm/web/history/seg.so"
# 实时弹幕池接口（XML，pid/oid=cid）
LIVE_API = "https://api.bilibili.com/x/v1/dm/list.so"
# 有无弹幕采样探测：近期逐日探测的天数
STREAK_CHECK_DAYS = 7


class DanmakuFetcher:
    """分段采集某视频全部历史弹幕。"""

    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def collect(self, vid_aid: int, cid: int, pubdate: int,
                item_cb=None, stop_flag=None, progress_cb=None) -> list[dict]:
        """采集 [pubdate, now] 全部弹幕。

        策略：
          1) 先在整个时间跨度上抽样探测，判断视频是否真的有弹幕。
             对于完全无弹幕的视频（如发布后无人发弹幕），直接跳过，
             避免从 pubdate 到今天逐日空跑（原痛点）。
          2) 探测到有弹幕后，从 pubdate 起逐日拉取 seg.so 直至今天。
             因为 B站历史弹幕每天至多返回约 500 条，超出的会顺延到
             后续日期返回，只有逐日请求才能保证完整。

        item_cb(kind, raw)：抓到每条弹幕时回调（kind="danmaku"）。
        stop_flag：可选 threading.Event，置位时尽快停止。
        progress_cb(text)：可选回调，报给上层阶段进展（供 GUI 刷新）。
        """
        if not pubdate:
            return []
        start = datetime.date.fromtimestamp(pubdate)
        end = datetime.date.today()
        if start > end:
            return []
        items: list[dict] = []

        if self._has_any_danmaku(cid, start, end, stop_flag=stop_flag, progress_cb=progress_cb):
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from threading import Lock

            day_workers = getattr(__import__("config", fromlist=["get_config"]).get_config(), "danmaku_day_workers", 4)
            lock = Lock()

            def _fetch_day(day: datetime.date) -> list[dict]:
                if stop_flag is not None and stop_flag.is_set():
                    return []
                try:
                    return self._fetch_day_all(cid, day)
                except Exception as exc:
                    logger.warning("aid=%s %s 弹幕段失败: %s", vid_aid, day, exc)
                    return []

            # 并行逐日：各 worker 用独立请求，但全局 QPS 兜底总速率
            with ThreadPoolExecutor(max_workers=max(1, day_workers)) as pool:
                futures = {pool.submit(_fetch_day, day): day for day in _daterange(start, end)}
                # 按日期有序收集（等待全部，避免乱序进度），但保留 stop 快速退出
                for day in _daterange(start, end):
                    if stop_flag is not None and stop_flag.is_set():
                        break
                    fut = None
                    for f, d in futures.items():
                        if d == day:
                            fut = f
                            break
                    if fut is None:
                        continue
                    try:
                        day_items = fut.result()
                    except Exception:
                        day_items = []
                    with lock:
                        for dm in day_items:
                            dm["video_aid"] = vid_aid
                            if item_cb:
                                try:
                                    item_cb("danmaku", dm)
                                except Exception:
                                    logger.exception("弹幕回调失败")
                        items.extend(day_items)
                    if len(day_items):
                        logger.info("aid=%s %s 弹幕 %s 条 (累计 %s)", vid_aid, day, len(day_items), len(items))
                    if progress_cb:
                        progress_cb(f"弹幕 {day} -> 累计 {len(items)} 条")
            if stop_flag is not None and stop_flag.is_set():
                pass
        else:
            logger.info("aid=%s 抽样探测历史无弹幕(尝试实时池)", vid_aid)
            if progress_cb:
                progress_cb("历史弹幕探测为空，尝试实时弹幕池…")

        # 实时弹幕池补采（覆盖尚未归档进历史的近期弹幕）
        if stop_flag is None or not stop_flag.is_set():
            try:
                live = parse_live_xml(self.client.get_bytes(LIVE_API, params={"oid": cid}))
                for dm in live:
                    dm["video_aid"] = vid_aid
                    if item_cb:
                        try:
                            item_cb("danmaku", dm)
                        except Exception:
                            logger.exception("弹幕回调失败")
                if live:
                    logger.info("aid=%s 实时弹幕池补采 %s 条 (累计 %s)", vid_aid, len(live), len(items))
                items.extend(live)
                if progress_cb:
                    progress_cb(f"实时弹幕池补采 {len(live)} 条")
            except Exception as exc:
                logger.warning("aid=%s 实时弹幕补采失败: %s", vid_aid, exc)
        return items

    def _has_any_danmaku(self, cid: int, start: datetime.date, end: datetime.date,
                         stop_flag=None, progress_cb=None) -> bool:
        """在 [start, end] 范围内抽样探测是否存在弹幕。

        采用"近期密集 + 指数回溯"：
          - 先逐日探测最近 STREAK_CHECK_DAYS 天（近期活跃视频通常每天有弹幕，
            可立即命中，避免多余探测）。
          - 再从 end 向 start 以指数间隔（1,2,4,8,...天）回溯抽样，直至覆盖 pubdate；
            对内容已冷/无人弹幕的视频在几十次请求内即可判空。
          - 最后补探 pubdate 首日，防止早期弹幕被跳过。

        stop_flag 置位时立即停止探测返回 False。
        progress_cb(text) 用于 GUI 展示探测阶段进度。
        """
        recent = STREAK_CHECK_DAYS

        def _may_abort() -> bool:
            return stop_flag is not None and stop_flag.is_set()

        # 近期逐日
        for offset in range(0, min(recent, (end - start).days + 1)):
            if _may_abort():
                return False
            probe = end - datetime.timedelta(days=offset)
            if progress_cb:
                progress_cb(f"探测弹幕 {probe}（近期）")
            if self._has_danmaku(cid, probe):
                logger.info("抽样命中弹幕日期 %s (近期)", probe)
                return True
        # 指数回溯
        gap = 1
        cur = end - datetime.timedelta(days=recent)
        while cur >= start:
            if _may_abort():
                return False
            if progress_cb:
                progress_cb(f"探测弹幕 {cur}（回溯）")
            if self._has_danmaku(cid, cur):
                logger.info("抽样命中弹幕日期 %s (回溯)", cur)
                return True
            gap *= 2
            cur -= datetime.timedelta(days=gap)
        # 早期首日
        if _may_abort():
            return False
        if progress_cb:
            progress_cb(f"探测弹幕 {start}（首日）")
        if self._has_danmaku(cid, start):
            logger.info("抽样命中弹幕日期 %s (pubdate首日)", start)
            return True
        return False

    def _has_danmaku(self, cid: int, day: datetime.date) -> bool:
        """探测某天是否有弹幕（拉取第一段）。"""
        try:
            raw = self.client.get_bytes(
                HISTORY_API,
                params={"type": 1, "oid": cid, "date": day.strftime("%Y-%m-%d"), "segment_index": 1},
            )
            return len(raw) > 0
        except Exception:
            return False

    def fetch_day(self, cid: int, day: datetime.date) -> list[dict]:
        """公开接口：拉取某一天全部弹幕（增量补采单日用）。"""
        return self._fetch_day_all(cid, day)

    def _fetch_day_all(self, cid: int, day: datetime.date) -> list[dict]:
        """拉取某一天全部弹幕。

        实测：seg.so 对每天的完整弹幕一次性全部返回在 index=1（约 500+ 条
        甚至上千条），并不按 segment_index 分页；多余索引同样返回同一份。
        故只请求一次即可拿全当天弹幕。
        """
        raw = self.client.get_bytes(
            HISTORY_API,
            params={"type": 1, "oid": cid, "date": day.strftime("%Y-%m-%d"), "segment_index": 1},
        )
        if not raw:
            return []
        return parse_dms(raw)


# ---- 极简 protobuf 读取器（仅用于 B站弹幕，容错解析） ----


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    @property
    def done(self) -> bool:
        return self.pos >= len(self.data)

    def varint(self) -> int | None:
        result = shift = 0
        while self.pos < len(self.data):
            b = self.data[self.pos]
            self.pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result
            shift += 7
            if shift > 70:
                return None
        return None

    def bytes(self) -> bytes | None:
        n = self.varint()
        if n is None:
            return None
        end = self.pos + n
        if end > len(self.data):
            return None
        blob = self.data[self.pos:end]
        self.pos = end
        return blob


def parse_dms(data: bytes) -> list[dict]:
    """解析 seg.so 返回的 protobuf 主体，拆出每条弹幕。

    bilibili 弹幕 protobuf 节点（DmSegMobileReply）：
      message DmSegMobileReply { repeated DmSegBody elems = 1; }
      message DmSegBody { id=1; progress=2; mode=3; fontsize=4; color=5;
                          mid_hash=6; content=7; ctime=8; pool=9; }
    """
    if not data:
        return []
    out: list[dict] = []
    try:
        elems_field = _collect_fields(data)
        for field_number, raw in elems_field:
            if field_number != 1:  # repeated elems = 1
                continue
            dm = _parse_dm(raw)
            if dm and dm.get("content"):
                out.append(dm)
    except Exception:
        logger.exception("弹幕 protobuf 解析失败(len=%s)", len(data))
    return out


def _collect_fields(data: bytes) -> list[tuple[int, object]]:
    """读取最外层 message 的字段列表 [(field_number, value)]。"""
    reader = _Reader(data)
    fields: list[tuple[int, object]] = []
    while not reader.done:
        key = reader.varint()
        if key is None:
            break
        field_no = key >> 3
        wire = key & 0x7
        try:
            if wire == 2:
                value = reader.bytes()
                if value is None:
                    break
                fields.append((field_no, value))
            else:
                _skip(reader, wire)
        except Exception:
            break
    return fields


def _parse_dm(raw: bytes) -> dict | None:
    reader = _Reader(raw)
    dm: dict = {"id": 0, "progress": 0.0, "mode": 1, "color": 16777215,
                "mid_hash": "", "content": "", "ctime": 0}
    while not reader.done:
        key = reader.varint()
        if key is None:
            break
        field_no = key >> 3
        wire = key & 0x7
        try:
            if wire == 2:
                value = reader.bytes()
                if value is None:
                    break
                _set_dm(dm, field_no, value)
            elif wire == 0:
                value = reader.varint()
                if value is None:
                    break
                _set_dm_var(dm, field_no, value)
            else:
                _skip(reader, wire)
        except Exception:
            break
    return dm


def _set_dm(dm: dict, field_no: int, value: bytes) -> None:
    if field_no == 6:
        dm["mid_hash"] = value.decode("utf-8", errors="ignore")
    elif field_no == 7:
        dm["content"] = value.decode("utf-8", errors="ignore")
    elif field_no == 9:
        dm["pool"] = value.hex() if isinstance(value, bytes) else value


def _set_dm_var(dm: dict, field_no: int, value: int) -> None:
    if field_no == 1:
        dm["id"] = value
    elif field_no == 2:
        dm["progress"] = value / 1000.0
    elif field_no == 3:
        dm["mode"] = value
    elif field_no == 5:
        dm["color"] = value
    elif field_no == 8:
        dm["ctime"] = value


def _skip(reader: _Reader, wire: int) -> None:
    if wire == 0:
        reader.varint()
    elif wire == 1:
        reader.pos += 8
    elif wire == 2:
        reader.bytes()
    elif wire == 5:
        reader.pos += 4
    else:
        raise ValueError(f"unsupported wire type {wire}")


def _daterange(start: datetime.date, end: datetime.date):
    """[start, end] 每日递增的日期生成器。"""
    day = start
    while day <= end:
        yield day
        day += datetime.timedelta(days=1)


def parse_live_xml(data: bytes) -> list[dict]:
    """解析实时弹幕池 XML（/x/v1/dm/list.so）。

    XML 结构：<i><d p="progress,mode,fontsize,color,ctime,pool,midHash,dmid,roomid">内容</d>...</i>
    """
    if not data:
        return []
    text = data.decode("utf-8", errors="ignore")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        logger.warning("实时弹幕池 XML 解析失败: %s", text[:80])
        return []
    out: list[dict] = []
    for node in root.findall("d"):
        attrs = (node.get("p", "") or "").split(",")
        if len(attrs) < 8:
            continue
        try:
            out.append({
                "id": int(attrs[7] or 0),
                "progress": float(attrs[0] or 0),
                "mode": int(attrs[1] or 1),
                "color": int(attrs[3] or 16777215),
                "ctime": int(attrs[4] or 0),
                "mid_hash": str(attrs[6] or "").strip().lower(),
                "content": (node.text or "").strip(),
            })
        except (ValueError, IndexError):
            continue
    return out