"""文本洞察纯函数：清洗/分词/高频词/情感/时段/长度分布。

全部为纯函数（不连库），可被 GUI / CLI / 单测复用。
分词策略：优先 jieba（若安装），否则回退为“中文2-gram + 字母数字词”混合切分，
保证零依赖可用、有 jieba 时更准。
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter

from etl.cleaner import clean_text

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_SEQ_RE = re.compile(r"[\u4e00-\u9fff]+")

# 精简停用词（命中即丢）：标点/语气/刷屏词
STOPWORDS = frozenset(
    ["的了是在我有和就人不一一个上也到说要去你会着没有看好这那啊哦嗯哈哇呀呢吧吗啦这个那个这里那里什么怎么为什么非常特别真的确实有点比较都还再更最太很可以能够因为所以但是不过然后于是以及或者还是虽然尽管如果即使每各各位大家一起给跟把被让向视频弹幕评论点赞收藏投币转发哈哈哈呵呵哔哩哔哩前排沙发顶", "UP", "主"]
    + ["", " ", "　", "、", "，", "。", "！", "？", "…", "～", "~", "·",
       "「", "」", "『", "』", "（", "）", "(", ")", "[", "]", "【", "】"]
)

# 极简情感词典（词典法，适合短文本打分；命中即 +/-1，可后续换模型）
POS_WORDS = frozenset(
    ["好喜欢爱赞棒强神优秀精彩好看感动泪目好笑笑死支持顶加油牛逼厉害推荐安利宝藏神仙可爱帅美绝绝子惊艳震撼封神泪喜欢爱了好评五星"]
    + ["好", "赞", "顶", "牛", "神"]
)
NEG_WORDS = frozenset(
    ["差烂垃圾恶心难看无聊失望拉胯辣鸡难听讨厌反对抵制差评一星避雷翻车无语离谱下头烂片难看尬尴尬水", "plagiarism", "抄袭"]
    + ["烂", "差", "坑", "雷", "尬"]
)


def _tokenize_fallback(text: str) -> list[str]:
    """零依赖分词：字母数字按词切，中文按2-gram滑窗。"""
    toks: list[str] = []
    for w in _WORD_RE.findall(text.lower()):
        if len(w) >= 2 and w not in STOPWORDS:
            toks.append(w)
    for seq in _CJK_SEQ_RE.findall(text):
        if len(seq) == 1:
            if seq not in STOPWORDS:
                toks.append(seq)
        else:
            for i in range(len(seq) - 1):
                bigram = seq[i:i + 2]
                if bigram not in STOPWORDS:
                    toks.append(bigram)
    return toks


try:
    import jieba  # type: ignore
    _HAS_JIEBA = True
except Exception:
    _HAS_JIEBA = False


def tokenize(text: str) -> list[str]:
    """分词：有 jieba 用 jieba 精确模式，否则回退 2-gram。"""
    text = clean_text(text)
    if not text:
        return []
    if _HAS_JIEBA:
        try:
            toks = [t.strip().lower() for t in jieba.cut(text, cut_all=False)]
            return [t for t in toks if t and t not in STOPWORDS and len(t) >= 2
                    or (len(t) == 1 and _CJK_RE.match(t) and t not in STOPWORDS)]
        except Exception:
            pass
    return _tokenize_fallback(text)


def top_terms(texts: list[str], top: int = 20) -> list[dict]:
    """高频词 TopN：返回 [{term, cnt}]。"""
    c: Counter = Counter()
    for t in texts or []:
        for tok in tokenize(t):
            c[tok] += 1
    return [{"term": k, "cnt": v} for k, v in c.most_common(max(1, top))]


def sentiment_of(text: str) -> int:
    """单条情感打分：+1 正向 / -1 负向 / 0 中性（子串命中计数比较）。"""
    t = clean_text(text)
    if not t:
        return 0
    pos = sum(1 for w in POS_WORDS if w and w in t)
    neg = sum(1 for w in NEG_WORDS if w and w in t)
    if pos > neg:
        return 1
    if neg > pos:
        return -1
    return 0


def sentiment_dist(texts: list[str]) -> dict:
    """情感分布：{pos, neu, neg, total}。"""
    out = {"pos": 0, "neu": 0, "neg": 0, "total": 0}
    for t in texts or []:
        s = sentiment_of(t)
        out["total"] += 1
        if s > 0:
            out["pos"] += 1
        elif s < 0:
            out["neg"] += 1
        else:
            out["neu"] += 1
    return out


def _coerce_dt(v) -> _dt.datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, _dt.datetime):
        return v
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(v)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    try:
        return _dt.datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def daily_series(rows: list[dict], time_key: str = "ctime",
                 days: int = 30, anchor: _dt.date | None = None) -> list[dict]:
    """按日计数序列（近 N 天）：返回 [{day, cnt}]，缺失日期补 0。

    自适应时间锚点：
    若 rows 中最新发言日期距今超过 3 天（老视频或历史归档数据），自动以最新发言日期作为时间锚点，
    展现真实的 30 天互动波峰，彻底消除最近几天全为 0 的贴地水平线。
    """
    days = max(1, int(days or 30))
    valid_dts = []
    for r in rows or []:
        dt = _coerce_dt((r or {}).get(time_key))
        if dt:
            valid_dts.append(dt)

    today = _dt.date.today()
    if anchor is not None:
        target_anchor = anchor
    elif valid_dts:
        max_d = max(d.date() for d in valid_dts)
        target_anchor = today if (today - max_d).days <= 3 else max_d
    else:
        target_anchor = today

    base = {str(target_anchor - _dt.timedelta(days=i)): 0 for i in range(days - 1, -1, -1)}
    for dt in valid_dts:
        key = str(dt.date())
        if key in base:
            base[key] += 1
    return [{"day": k, "cnt": base[k]} for k in sorted(base)]


def hour_dist(rows: list[dict], time_key: str = "ctime") -> list[dict]:
    """24 小时分布：返回 [{hour, cnt}]，ctime 解析失败的行忽略。"""
    cnt = [0] * 24
    for r in rows or []:
        dt = _coerce_dt((r or {}).get(time_key))
        if dt is None:
            continue
        cnt[dt.hour] += 1
    return [{"hour": h, "cnt": cnt[h]} for h in range(24)]


def length_dist(texts: list[str]) -> dict:
    """文本长度分档：短(<=10)/中(11-50)/长(51-140)/超长(>140)。"""
    out = {"short": 0, "medium": 0, "long": 0, "xlong": 0, "total": 0}
    for t in texts or []:
        n = len(clean_text(t))
        out["total"] += 1
        if n <= 10:
            out["short"] += 1
        elif n <= 50:
            out["medium"] += 1
        elif n <= 140:
            out["long"] += 1
        else:
            out["xlong"] += 1
    return out


def like_dist(rows: list[dict], like_key: str = "like_num") -> dict:
    """点赞分档：0 / 1-9 / 10-99 / 100+。"""
    out = {"zero": 0, "low": 0, "mid": 0, "hot": 0, "total": 0}
    for r in rows or []:
        try:
            n = int((r or {}).get(like_key) or 0)
        except (TypeError, ValueError):
            n = 0
        out["total"] += 1
        if n <= 0:
            out["zero"] += 1
        elif n < 10:
            out["low"] += 1
        elif n < 100:
            out["mid"] += 1
        else:
            out["hot"] += 1
    return out


def ip_dist(rows: list[dict], top: int = 15) -> list[dict]:
    """IP 属地 TopN：返回 [{ip, cnt}]，空属地归为“未知”。"""
    c: Counter = Counter()
    for r in rows or []:
        ip = str((r or {}).get("ip_location") or "").strip() or "未知"
        c[ip] += 1
    return [{"ip": k, "cnt": v} for k, v in c.most_common(max(1, top))]
