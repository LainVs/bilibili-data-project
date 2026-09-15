"""B站评论接口：分页游标遍历 + 一级/二级评论。"""
from __future__ import annotations

import datetime
import json
import logging
import time
import urllib.parse

from crawler.api.client import ApiClient
from crawler.api.wbi import MIXIN_KEY_ENC, enc_wbi, get_wbi_keys

logger = logging.getLogger(__name__)

REPLY_API = "https://api.bilibili.com/x/v2/reply/wbi/main"
REPLY_SUB_API = "https://api.bilibili.com/x/v2/reply/reply"

# 评论对象类型：1=视频 12=专栏 17=动态
REPLY_TYPE_VIDEO = 1
REPLY_TYPE_ARTICLE = 12
REPLY_TYPE_DYNAMIC = 17


class CommentFetcher:
    """按游标拉取某对象（视频/专栏/动态）全部评论（含二级）。"""

    # 并行 worker 的 client 工厂（默认真实现，测试可注入 mock）
    client_factory = ApiClient

    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def _sign(self, base: dict) -> dict:
        """附加 wbi 签名（动态取 key，失败用内置兜底）。"""
        keys = get_wbi_keys(self.client)
        if keys:
            img_key, sub_key = keys
        else:
            img_key, sub_key = MIXIN_KEY_ENC[:16], MIXIN_KEY_ENC[16:32]
        signed, _ = enc_wbi(base, img_key, sub_key)
        return signed

    @staticmethod
    def _make_pagination(offset: str) -> str:
        """构造 pagination_str 游标参数。

        B站 /x/v2/reply 系列接口的 next_offset 是 base64 序列化字符串，
        直接放入 {"offset": "<next_offset>"} 即可翻页（经实测验证）。
        注意：不能再用 JSON 字符串内嵌包裹，否则接口不识别游标并永远返回第一页。
        """
        return json.dumps({"offset": offset}, separators=(",", ":"))

    # 主评论页拉取失败哨兵：与“无下一页”(None) 区分，避免缺页被当采完
    _PAGE_ERROR = object()

    def _fetch_page(self, oid: int, ref_type: int, mode: int,
                    offset: str, stop_flag=None):
        """拉主评论一页：成功返回 data；无下一页/用户停止返回 None；请求出错返回 _PAGE_ERROR。"""
        if stop_flag is not None and stop_flag.is_set():
            return None
        base = {
            "oid": oid,
            "type": ref_type,
            "mode": mode,
            "plat": 1,
            "ps": 30,  # 实测上限；20->30 使翻页请求数 -33%
            "pagination_str": self._make_pagination(offset),
            "web_location": 1315875,
        }
        params = self._sign(base)
        try:
            payload = self.client.get_json(REPLY_API, params=params)
        except Exception as exc:
            logger.warning("评论接口请求失败 oid=%s: %s", oid, exc)
            return self._PAGE_ERROR
        data = payload.get("data") or {}
        if not data.get("replies"):
            logger.info("评论 oid=%s 无下一页（replies空）", oid)
            return None
        return data

    def collect(self, oid: int, *, with_sub: bool = True, ref_type: int = REPLY_TYPE_VIDEO,
                item_cb=None, stop_flag=None,
                mode: int = 3, min_ctime: int | None = None,
                progress_cb=None) -> list[dict]:
        """收集一个对象（视频 aid / 专栏 cvid / 动态id）的全部评论及二级回复。

        oid：对象 ID（视频 aid / 动态 id / 专栏 cvid）。
        ref_type：评论区类型码（1视频 12专栏 17动态）。

        item_cb(kind, raw)：每次抓到一条评论时回调（raw 为原始回复 dict）。
        stop_flag：可选 threading.Event，置位时尽快停止。
        progress_cb(page_no, count)：每翻完一页主评论回调
            （page_no=页码，count=当前累计解析条数），供上层展示实时进度。

        增量参数（避免重复采集）：
          mode：1=热门/按点赞，2=按时间倒序（新→旧），3=默认热度排序。
               增量时建议 mode=2，便于遇到旧评论即停。
          min_ctime：可选最小评论时间戳（unix）。当 mode=2 且当前评论
               时间早于 min_ctime 时，后续只会更旧，直接停止翻页。
               这是“上次已采集到的时刻”，防止从头重拉旧评论。

        流水线：当前页的二级回复并行拉取期间，后台线程已预取下一页主评论，
        让"翻页网络往返"与"二级回复等待"重叠，显著减少单视频总耗时。
        """
        from concurrent.futures import Future, ThreadPoolExecutor
        from threading import Lock

        results: list[dict] = []
        guard = 0
        seq = 0  # 页码序号，用于保证按页有序消费
        # 后台预取下一主评论页（使用同一 client，全局 QPS 仍兜底）
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="main-page-prefetch")
        futures: dict[int, Future] = {}

        offsets: dict[int, str] = {}
        retries: dict[int, int] = {}

        def _prefetch(offset: str) -> None:
            nonlocal seq
            seq += 1
            offsets[seq] = offset
            retries.setdefault(seq, 0)
            futures[seq] = pool.submit(self._fetch_page, oid, ref_type, mode,
                                       offset, stop_flag)

        _prefetch("")  # 第 1 页
        next_seq = 0
        page_error: str | None = None
        stop_reason = "eof"
        while True:
            if stop_flag is not None and stop_flag.is_set():
                stop_reason = "stop"
                break
            next_seq += 1
            fut = futures.get(next_seq)
            if fut is None:
                break
            try:
                data = fut.result()  # 等当前页（若未就绪；就绪则不等待）
            except Exception as exc:
                data = self._PAGE_ERROR
                logger.warning("评论预取任务异常 oid=%s 页#%s: %s", oid, next_seq, exc)
            if data is self._PAGE_ERROR:
                # 瞬时抖动重试一次；仍失败则记错跳出（调用方记错且不标 done，下次增量补）
                if retries.get(next_seq, 0) < 1 and (stop_flag is None or not stop_flag.is_set()):
                    retries[next_seq] = retries.get(next_seq, 0) + 1
                    logger.warning("评论 oid=%s 第%s页失败，重试1次（已得%s条）",
                                   oid, next_seq, len(results))
                    futures[next_seq] = pool.submit(
                        self._fetch_page, oid, ref_type, mode,
                        offsets.get(next_seq, ""), stop_flag)
                    next_seq -= 1
                    continue
                page_error = f"oid={oid} 第{next_seq}页拉取失败（已得{len(results)}条）"
                stop_reason = "error"
                break
            replies_data = data.get("replies") or []

            newest_under_min = False
            sub_jobs: list[tuple[int, int]] = []  # (rpid, rcount)，攒一批并行拉
            for reply in replies_data:
                if not isinstance(reply, dict):
                    continue  # B站会返回 null 占位（已删评论），跳过不断整页
                if stop_flag is not None and stop_flag.is_set():
                    break
                if min_ctime and mode == 2 and (reply.get("ctime") or 0) < min_ctime:
                    newest_under_min = True
                    break
                if item_cb:
                    try:
                        item_cb("comment", reply)
                    except Exception:
                        logger.exception("评论回调失败")
                results.append(self._normalize(reply, oid, ref_type=ref_type))
                if with_sub and reply.get("rcount", 0) > 0:
                    sub_jobs.append((reply["rpid"], reply["rcount"] or 0))

            # 预取下一页（在二级回复等待开始前就发出请求）
            cursor = data.get("cursor") or {}
            next_offset = (cursor.get("pagination_reply") or {}).get("next_offset") or ""
            guard += 1
            if guard > 20000:
                logger.warning("评论分页超过上限，疑似异常中断 oid=%s", oid)
                break

            # 二级回复并行拉取（worker=1 时串行不降级语义）；此期间下一主页已在拉
            if sub_jobs:
                results.extend(self._collect_subs_parallel(
                    sub_jobs, oid, item_cb=item_cb, stop_flag=stop_flag,
                    min_ctime=min_ctime, ref_type=ref_type))

            if progress_cb:
                try:
                    progress_cb(next_seq, len(results))
                except Exception:
                    logger.exception("评论进度回调失败")

            if guard % 10 == 0 or newest_under_min:
                logger.info("oid=%s 评论进度 %s 条%s", oid, len(results),
                            "（增量：已到旧评论边界）" if newest_under_min else "")
            if newest_under_min:
                break
            if not next_offset:
                break
            if stop_flag is not None and stop_flag.is_set():
                stop_reason = "stop"
                break
            _prefetch(next_offset)
        pool.shutdown(wait=False)  # 不等待在途预取；stop 时尽快返回
        if stop_reason == "error" and (stop_flag is None or not stop_flag.is_set()):
            # 缺页不是采完：抛给调用方记错（不标 done，下次增量补采）。
            # 已抓到的部分已经由 item_cb 实时落库，不丢。
            raise RuntimeError(page_error or f"oid={oid} 评论拉取中断")
        logger.info("评论采集完毕 oid=%s 共 %s 条", oid, len(results))
        return results

    def _collect_subs_parallel(self, jobs: list[tuple[int, int]], oid: int,
                               item_cb=None, stop_flag=None,
                               min_ctime: int | None = None,
                               ref_type: int = REPLY_TYPE_VIDEO) -> list[dict]:
        """并行拉取多个一级评论的二级回复（每 worker 独立 client，全局 QPS 兜底）。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from threading import Lock

        cfg = __import__("config", fromlist=["get_config"]).get_config()
        workers = getattr(cfg, "comment_sub_workers", 2)
        if workers <= 1 or len(jobs) <= 1:
            out: list[dict] = []
            for rpid, rcount in jobs:
                try:
                    out.extend(self._collect_sub(rpid, oid, rcount, item_cb=item_cb,
                                                 stop_flag=stop_flag, min_ctime=min_ctime,
                                                 ref_type=ref_type))
                except Exception as exc:
                    # 单条二级楼失败只跳过该楼（与并行路径一致），不掀翻整个视频
                    logger.warning("二级回复拉取失败 oid=%s rpid=%s: %s", oid, rpid, exc)
                    continue
            return out

        lock = Lock()
        ordered: dict[int, list[dict]] = {}

        def _run_one(idx: int, rpid: int, rcount: int) -> tuple[int, list[dict]]:
            # 独立 client：各自节流，但全局 QPS 仍限制合计速率
            client = type(self).client_factory(stop_event=stop_flag)
            try:
                sub_items = self._collect_sub(rpid, oid, rcount, item_cb=item_cb,
                                              stop_flag=stop_flag, min_ctime=min_ctime,
                                              ref_type=ref_type, _client=client)
            finally:
                close = getattr(client, "close", None)
                if close:
                    close()
            return idx, sub_items

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_one, i, rpid, rcount)
                       for i, (rpid, rcount) in enumerate(jobs)]
            for fut in as_completed(futures):
                try:
                    idx, sub_items = fut.result()
                except Exception as exc:
                    logger.warning("二级并发拉取失败: %s", exc)
                    continue
                with lock:
                    ordered[idx] = sub_items
        # 按原顺序拼接
        merged: list[dict] = []
        for i in range(len(jobs)):
            merged.extend(ordered.get(i, []))
        return merged

    def _collect_sub(self, rpid: int, oid: int, rcount: int, item_cb=None,
                     stop_flag=None, min_ctime: int | None = None,
                     ref_type: int = REPLY_TYPE_VIDEO,
                     _client=None) -> list[dict]:
        """拉取一条一级评论下的全部二级回复。"""
        client = _client or self.client
        items: list[dict] = []
        next_offset = ""
        while True:
            if stop_flag is not None and stop_flag.is_set():
                break
            base = {
                "oid": oid,
                "type": ref_type,
                "root": rpid,
                "ps": 20,  # 实测上限20+；10->20 使子回复请求数 -50%
                "pagination_str": self._make_pagination(next_offset),
            }
            params = self._sign(base)
            payload = client.get_json(REPLY_SUB_API, params=params)
            data = payload.get("data") or {}
            replies_data = data.get("replies") or []
            for sub in replies_data:
                if not isinstance(sub, dict):
                    continue  # null 占位跳过
                if stop_flag is not None and stop_flag.is_set():
                    break
                if min_ctime and (sub.get("ctime") or 0) < min_ctime:
                    return items
                if item_cb:
                    try:
                        item_cb("comment", sub)
                    except Exception:
                        logger.exception("评论回调失败")
                items.append(self._normalize(sub, oid, parent=rpid, ref_type=ref_type))
            cursor = data.get("cursor") or {}
            next_offset = (cursor.get("pagination_reply") or {}).get("next_offset") or ""
            if not next_offset:
                break
        return items

    def _normalize(self, r: dict, oid: int, parent: int = 0,
                     ref_type: int = REPLY_TYPE_VIDEO) -> dict:
        if not isinstance(r, dict) or not r.get("rpid"):
            raise ValueError(f"评论原始数据非法 oid={oid}：{str(r)[:80]}")
        member = r.get("member") or {}
        ctrl = r.get("reply_control") or {}
        return {
            "rpid": r["rpid"],
            "video_aid": oid,
            "ref_type": ref_type,
            "parent": parent,
            "uid": r.get("mid", 0),
            "content": (r.get("content") or {}).get("message", ""),
            "like_num": r.get("like", 0),
            "ctime": timestamp_to_dt(r.get("ctime", 0)),
            "ip_location": str(ctrl.get("location", "") or "").replace("IP属地：", ""),
            "member": {
                "uname": member.get("uname", ""),
                "level": (member.get("level_info") or {}).get("current_level", 0),
                "sex": member.get("sex", ""),
                "is_vip": 1 if ((member.get("vip") or {}).get("status", 0) == 1) else 0,
                "signature": member.get("sign", ""),
            },
        }


def timestamp_to_dt(ts: int) -> str:
    """unix 时间戳转 MySQL datetime 字符串。"""
    if not ts:
        return "1970-01-01 00:00:00"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")