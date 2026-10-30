# -*- coding: utf-8 -*-
"""trhistory.py —— 翻译历史（存系统临时目录，关机自动清空）。

为什么放 temp 而不是 exe 旁边的 data/：
  译文往往是大段文字，攒几十条就几十兆；而历史只是「回头看一眼」的便利功能，
  不值得长期占盘。放 `%TEMP%` 下，Windows 自己会在重启/清理时整目录删掉，
  用户不用管，我们也不用写「一键清空」之外的其他运维逻辑。

存储结构（`%TEMP%/猫娘工具箱-翻译历史/`）：
    index.json          所有条目的元信息（时间、语种、风格、字符数、预览…）
    items/<id>.json     每条的正文明细（原文 + 译文）
正文单独存文件，列表只读 index —— 否则打开历史窗口要把几十兆全读一遍。
"""
import json
import os
import re
import tempfile
import time

APP_DIR = "猫娘工具箱-翻译历史"
MAX_ITEMS = 100                  # 条数上限
MAX_BYTES = 20 * 1024 * 1024     # 总容量上限 20 MB
PREVIEW_LEN = 80                 # 列表里显示的摘要长度


def dir_path():
    """历史目录（每次调用都算一遍：temp 目录可能被系统改掉）。"""
    return os.path.join(tempfile.gettempdir(), APP_DIR)


def items_dir():
    return os.path.join(dir_path(), "items")


def _ensure():
    os.makedirs(items_dir(), exist_ok=True)


def index_path():
    return os.path.join(dir_path(), "index.json")


def _read_index():
    try:
        with open(index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_index(items):
    try:
        _ensure()
        tmp = index_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, index_path())
    except Exception:
        pass


def _safe_id(s):
    return re.sub(r"[^0-9A-Za-z_-]", "", s or "")


def item_path(item_id):
    return os.path.join(items_dir(), _safe_id(item_id) + ".json")


def _one_line(s, n=PREVIEW_LEN):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def _size_of(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def total_bytes():
    n = 0
    for it in _read_index():
        n += _size_of(item_path(it.get("id", "")))
    return n


def save(src_text, out_text, meta=None):
    """存一条历史。返回存下来的元信息（失败返回 None，不抛异常）。

    meta 里认这些键：src / dst / level / style / domain / tokens / cost。
    """
    if not (src_text or "").strip() or not (out_text or "").strip():
        return None
    _ensure()
    ts = time.time()
    item_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts)) + ("-%03d" % int(ts * 1000 % 1000))
    meta = dict(meta or {})
    entry = {
        "id": item_id,
        "ts": ts,
        "src": meta.get("src") or "",
        "dst": meta.get("dst") or "",
        "level": meta.get("level") or "",
        "style": meta.get("style") or "",
        "domain": meta.get("domain") or "",
        "tokens": int(meta.get("tokens") or 0),
        "cost": round(float(meta.get("cost") or 0), 2),
        "chars": len(src_text),
        "preview": _one_line(src_text),
    }
    try:
        with open(item_path(item_id), "w", encoding="utf-8") as f:
            json.dump({"src_text": src_text, "out_text": out_text}, f, ensure_ascii=False)
    except Exception:
        return None

    items = _read_index()
    items.insert(0, entry)
    _trim(items)
    _write_index(items)
    return entry


def _trim(items):
    """按条数和总容量双重裁剪，从最旧的开始丢（index 是新的在前）。"""
    # 1) 条数
    while len(items) > MAX_ITEMS:
        drop = items.pop()
        _remove_file(drop.get("id"))
    # 2) 容量
    total = sum(_size_of(item_path(it.get("id", ""))) for it in items)
    while items and total > MAX_BYTES:
        drop = items.pop()
        total -= _size_of(item_path(drop.get("id", "")))
        _remove_file(drop.get("id"))


def _remove_file(item_id):
    if not item_id:
        return
    try:
        os.remove(item_path(item_id))
    except Exception:
        pass


def load_all():
    """按时间倒序返回所有元信息（不读正文）。"""
    return _read_index()


def load_item(item_id):
    """读一条的完整内容。读不到返回 None。"""
    try:
        with open(item_path(item_id), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return None


def delete(item_id):
    items = [it for it in _read_index() if it.get("id") != item_id]
    _remove_file(item_id)
    _write_index(items)


def clear_all():
    """清空历史：删正文 + 重置索引。目录本身留着（temp 里空目录无害）。"""
    for it in _read_index():
        _remove_file(it.get("id"))
    _write_index([])


def human_size(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def human_time(ts):
    """今天只显示时分秒，昨天/更早带上日期 —— 列表里一眼分清新旧。"""
    lt = time.localtime(ts)
    today = time.localtime()
    hm = time.strftime("%H:%M:%S", lt)
    if (lt.tm_year, lt.tm_yday) == (today.tm_year, today.tm_yday):
        return hm
    if lt.tm_year == today.tm_year:
        return time.strftime("%m-%d ", lt) + hm
    return time.strftime("%Y-%m-%d ", lt) + hm
