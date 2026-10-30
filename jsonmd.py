# -*- coding: utf-8 -*-
"""jsonmd.py —— 投稿 JSON → Markdown → Markdown 预览（不生成 Word）。

思路和「文章投稿排版器」一致，只是最后不落 Word，而是直接在软件里按 Markdown 预览：
  1. 把 JSON 解析出来，找出 title / cover / markdown（兼容多种字段名与嵌套层级）；
  2. 取不到 markdown 的普通 JSON，就按「对象→标题+键值、数组→列表、等长对象数组→表格」转成 Markdown；
  3. Markdown → HTML（Python-Markdown，extra 扩展：表格 / 围栏代码 / 脚注 / 行内 HTML）；
  4. 用 QTextBrowser 显示，支持缩放（按钮 / Ctrl+滚轮），也可以在独立的大窗口里看。

全部本地完成：百万字符量级也就几十毫秒，所以输入停止 260ms 后自动重绘，不需要按钮。
"""
import json
import os
import re

import markdown as md_lib
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QKeySequence, QShortcut, QTextCharFormat,
                          QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSizeGrip, QSplitter,
                               QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

import store
import theme
from widgets import shadow

# 正文可能叫这些名字（按优先级）；实际用哪几个可以在设置里改
MD_KEYS = tuple(store.JM_KEYS)
TITLE_KEYS = ("title", "name", "headline", "subject")
COVER_KEYS = ("cover", "cover_url", "image", "thumbnail")

MIN_ZOOM, MAX_ZOOM, ZOOM_STEP = 0.6, 2.4, 0.1
BASE_PX = 15.0


# ============================================================ JSON → Markdown

def _s(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _is_scalar(v):
    return not isinstance(v, (dict, list))


def _uniform_scalar_dicts(items):
    """一组对象是否都能直接排成表格（键一致、值都是标量、且不太长）。"""
    if not items or not all(isinstance(x, dict) for x in items):
        return False
    keys = list(items[0].keys())
    if not keys or len(keys) > 8:
        return False
    for x in items:
        if list(x.keys()) != keys:
            return False
        for k in keys:
            v = x.get(k)
            if not _is_scalar(v):
                return False
            if len(_s(v)) > 120 or "\n" in _s(v):
                return False
    return True


def _table(rows, keys):
    head = "| " + " | ".join(str(k) for k in keys) + " |"
    line = "|" + "|".join([" --- "] * len(keys)) + "|"
    body = []
    for r in rows:
        body.append("| " + " | ".join(_s(r.get(k)).replace("|", "\\|").replace("\n", " ")
                                      for k in keys) + " |")
    return "\n".join([head, line] + body)


def _obj_to_md(obj, depth=0, skip=()):
    """把任意 JSON 结构转成 Markdown（只在没有正文 markdown 字段时用）。"""
    h = "#" * min(6, depth + 2)
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if depth == 0 and k in skip:      # 已经当成标题用掉的键
                continue
            label = str(k)
            if _is_scalar(v):
                s = _s(v).strip()
                if not s:
                    continue
                if "\n" in s or len(s) > 90:
                    out += ["%s %s" % (h, label), "", s, ""]
                else:
                    out.append("- **%s**：%s" % (label, s))
            else:
                out += ["", "%s %s" % (h, label), "", _obj_to_md(v, depth + 1), ""]
        return "\n".join(out).strip()
    if isinstance(obj, list):
        if _uniform_scalar_dicts(obj):
            return _table(obj, list(obj[0].keys()))
        for it in obj:
            if _is_scalar(it):
                s = _s(it).strip()
                if s:
                    out.append("- " + s.replace("\n", " "))
            elif isinstance(it, dict) and all(_is_scalar(v) for v in it.values()):
                # {名称: 值} 这种一行一个的，压成一条
                first = True
                for k, v in it.items():
                    s = _s(v).strip()
                    if not s:
                        continue
                    if first:
                        out.append("- **%s**：%s" % (k, s))
                        first = False
                    else:
                        out.append("  - %s：%s" % (k, s))
            else:
                out += ["", _obj_to_md(it, depth + 1), ""]
        return "\n".join(out).strip()
    return _s(obj)


def _maybe_json(v):
    """值是「JSON 字符串」时（接口有时会把整段 JSON 再包成字符串）解出来。"""
    if isinstance(v, str):
        head = v.lstrip()[:1]
        if head in ("{", "["):
            try:
                return json.loads(v)
            except Exception:
                return None
    return None


def body_keys():
    """当前生效的正文候选键：设置里的顺序，再剔掉「不提取」的键。

    提取逻辑不是「只认 markdown」：按这个顺序挨个看，取第一个有内容的字符串。
    """
    keys = store.jm_keys()
    ex = set(store.jm_exclude())
    out = [k for k in keys if k not in ex]
    return out or [k for k in MD_KEYS if k not in ex] or ["markdown"]


def _pick(d, keys):
    for k in keys:
        if isinstance(d, dict) and k in d and not _is_scalar(d[k]):
            continue
        if isinstance(d, dict) and k in d and _s(d[k]).strip():
            return _s(d[k])
    return ""


def _pick_pair(d, keys):
    """和 _pick 一样，但把命中的键名也返回，界面上可以显示「取自 xxx」。"""
    for k in keys:
        if isinstance(d, dict) and k in d:
            v = d[k]
            if _is_scalar(v) and _s(v).strip():
                return k, _s(v)
    return "", ""


def extract(json_text):
    """解析投稿 JSON，返回 {'ok', 'title', 'cover', 'markdown', 'kind', 'note'}。"""
    if not json_text.strip():
        return {"ok": False, "note": "", "title": "", "cover": "", "markdown": "",
                "kind": "", "error": ""}
    try:
        data = json.loads(json_text)
        # 双保险：整段是字符串、或者值里又嵌了一层 JSON，都拆一次
        for _ in range(2):
            inner = _maybe_json(data)
            if inner is None:
                break
            data = inner
    except Exception as e:
        msg = str(e)
        if "Expecting" in msg:
            msg = "JSON 还没写完整（%s）" % msg.split(":")[0].strip()
        return {"ok": False, "note": "", "title": "", "cover": "", "markdown": "",
                "kind": "", "error": msg}

    note = ""
    cands = body_keys()
    # 数组：取第一篇（并说明一共几篇）
    if isinstance(data, list):
        if not data:
            return {"ok": False, "title": "", "cover": "", "markdown": "", "kind": "",
                    "note": "", "error": "JSON 是空数组"}
        if len(data) > 1:
            note = "共 %d 项，预览的是第 1 项" % len(data)
        data = data[0]

    node = data
    if isinstance(data, dict):
        # 常见的包一层：ai_content / article / data / result
        for k in ("ai_content", "article", "data", "result", "post"):
            inner = data.get(k)
            if isinstance(inner, str):          # 值是 JSON 字符串，先解开
                inner = _maybe_json(inner)
            if isinstance(inner, dict) and _pick(inner, cands):
                node = inner
                break
        # 只包了一层、内层才是真数据的情况
        if node is data and isinstance(data.get("data"), dict) and not _pick(data, cands):
            node = data["data"]

    ukey, body = _pick_pair(node, cands) if isinstance(node, dict) else ("", "")
    if body:
        title = _pick(node, TITLE_KEYS)
        cover = _pick(node, COVER_KEYS)
        body = clean_markdown(body, title)
        return {"ok": True, "title": title, "cover": cover, "markdown": body,
                "kind": "article", "note": note, "error": "", "key": ukey}

    # 没有正文 markdown 字段 → 通用转换
    title = _pick(data, TITLE_KEYS) if isinstance(data, dict) else ""
    used = [k for k in TITLE_KEYS if isinstance(data, dict) and _s(data.get(k)).strip()][:1]
    gen = _obj_to_md(data, skip=used)
    if not (gen.strip() or title):
        return {"ok": False, "title": "", "cover": "", "markdown": "", "kind": "",
                "note": note, "error": "这个 JSON 里没有可显示的内容"}
    md = ("# %s\n\n" % title if title else "") + gen
    return {"ok": True, "title": title, "cover": "", "markdown": md.strip(),
            "kind": "generic", "note": note, "error": "", "key": ""}


def _is_wide_head(ch):
    """这个字符是不是「中文/日文/韩文」这类宽字符。"""
    if not ch:
        return False
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0xAC00 <= o <= 0xD7A3 or 0xFF00 <= o <= 0xFF60
            or 0x1F000 <= o <= 0x1FAFF)


def normalize_headings(text):
    """把标题语法规范成「# + 一个半角空格 + 文字」。

    为什么需要：
      * `#标题`（# 后没空格）Python-Markdown 能认，但 CommonMark（公众号、
        Typora、GitHub、多数在线工具）**不认**，粘出去就变普通段落了；
      * `#　标题`（中文全角空格）更麻烦，两边都不认。

    所以统一补成半角空格。两处**故意不动**：
      * 围栏代码块（三个反引号 / 三个波浪线）里的内容 —— 那是代码，改了会坏；
      * `#include` / `#define` / `#hashtag` 这类 ASCII 开头的 —— 可能是指令或标签，
        不是标题。只有紧跟中日韩文字 / 全角字符 / emoji 才判定为「挤在一起的标题」。
    """
    if not text:
        return text
    lines = text.split("\n")
    out = []
    in_fence = False
    fence_mark = ""
    for ln in lines:
        s = ln.strip()
        if s.startswith("```") or s.startswith("~~~"):
            mark = s[:3]
            if not in_fence:
                in_fence, fence_mark = True, mark
            elif mark == fence_mark:
                in_fence, fence_mark = False, ""
            out.append(ln)
            continue
        if in_fence:
            out.append(ln)
            continue

        m = re.match(r"^([ \t]{0,3})(#{1,6})(.*)$", ln)
        if m:
            indent, hashes, rest = m.groups()
            body = rest.lstrip(" \t\u3000")
            if rest[:1] in (" ", "\t", "\u3000"):
                out.append("%s%s %s" % (indent, hashes, body))
                continue
            if _is_wide_head(body[:1]):
                out.append("%s%s %s" % (indent, hashes, body))
                continue
        out.append(ln)
    return "\n".join(out)


def clean_markdown(text, title=""):
    """清掉从 JSON 里带出来的包装残留（不碰正文内容）。

    ⚠️ 这里**不能**删「跟 title 重复的 H1」。
    以前为了「避免预览里出现两个标题」加过一条去重，结果是个坑：
      * `title` 字段在预览里**根本不会渲染**（build_html 只吃 cover / note），
        所以删掉正文里的 H1 = 预览里彻底没有标题；
      * 而且那条正则只匹配「# 后面有空格」的写法 —— 于是 `# 标题` 被吞、
        `#标题` 反而留着，表现就是「有时候标题没了，全看有没有空格」。

    现在只做规范化（`#标题` → `# 标题`），一行标题都不删。
    `title` 参数保留是为了兼容调用方，不再参与清洗。
    """
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    # 纯 HTML 包裹行（<p align="center"> / </p> / <div>）单独占一行时去掉，图还是留着的
    t = re.sub(r"(?m)^[ \t]*</?(?:p|div|center|span)\b[^>\n]*>[ \t]*$\n?", "", t)
    t = normalize_headings(t)
    return t.strip("\n")


def to_html_body(md_text):
    """Markdown → HTML 片段。失败时退化成 <pre>，不让预览整个空掉。"""
    try:
        md = md_lib.Markdown(extensions=["extra", "sane_lists"], output_format="html5")
        return md.convert(md_text or "")
    except Exception:
        esc = (md_text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return "<pre>%s</pre>" % esc


_HTML_TPL = """<html><head><meta charset="utf-8"><style>
body {{ font-family: "{font}"; font-size: {base:.1f}px; color: #24405a;
       line-height: 168%; margin: 6px 10px 22px 6px; }}
h1 {{ font-size: {h1:.1f}px; color: #1d3b55; font-weight: 700;
      margin: 22px 0 10px 0; }}
h2 {{ font-size: {h2:.1f}px; color: #23445f; font-weight: 700;
      margin: 20px 0 8px 0; border-left: 4px solid #4a9fd8; padding-left: 8px; }}
h3 {{ font-size: {h3:.1f}px; color: #2c4a63; font-weight: 700; margin: 16px 0 6px 0; }}
h4, h5, h6 {{ font-size: {base:.1f}px; color: #2c4a63; font-weight: 700; margin: 12px 0 5px 0; }}
p {{ margin: 8px 0; }}
ul, ol {{ margin: 8px 0 8px 20px; }}
li {{ margin: 4px 0; }}
blockquote {{ margin: 10px 0; padding: 2px 12px; color: #5b7b95;
              border-left: 3px solid #c3def2; background: #f6fbff; }}
table {{ border-collapse: collapse; margin: 10px 0; }}
th {{ background: #e8f4fc; border: 1px solid #c3def2; padding: 6px 10px;
      font-weight: 700; color: #23445f; }}
td {{ border: 1px solid #d7e8f5; padding: 6px 10px; }}
code {{ font-family: "Consolas", "Courier New", monospace; font-size: {base:.1f}px;
        background: #f1f7fc; color: #2a5c80; }}
pre {{ background: #f6fbff; border: 1px solid #e0eefa; padding: 10px 12px;
       white-space: pre-wrap; }}
pre code {{ background: transparent; }}
hr {{ border: none; border-top: 1px solid #d7e8f5; margin: 16px 0; }}
a {{ color: #3c8cc2; }}
img {{ max-width: 96%; }}
.meta {{ color: #7d9cb5; font-size: {base_s:.1f}px; margin: 0 0 10px 0; }}
</style></head><body>{meta}{body}</body></html>"""


def build_html(md_text, zoom=1.0, cover="", note=""):
    """把 Markdown 拼成一页可以直接塞给 QTextBrowser 的 HTML。"""
    # BASE_PX 要跟着窗口缩放走，否则高 DPI 屏上预览文字会比界面小一圈
    base = theme.px(BASE_PX) * max(MIN_ZOOM, min(MAX_ZOOM, zoom))
    body = to_html_body(md_text)
    meta = ""
    if cover:
        meta += '<p class="meta">封面：%s</p>' % cover.replace("<", "&lt;")
    if note:
        meta += '<p class="meta">%s</p>' % note.replace("<", "&lt;")
    return _HTML_TPL.format(font=theme.FONT_UI, base=base, base_s=base * 0.86,
                            h1=base * 1.72, h2=base * 1.4, h3=base * 1.16,
                            meta=meta, body=body)


def _centre_preview(win, host=None):
    """把独立预览窗放到主窗口中间偏上，太靠边会跑出屏幕。"""
    scr = QApplication.primaryScreen()
    avail = scr.availableGeometry() if scr else None
    w, h = win.width(), win.height()
    if host is not None and host.isVisible():
        g = host.frameGeometry()
        x, y = g.center().x() - w // 2, g.top() + theme.px(30)
    elif avail:
        x, y = avail.center().x() - w // 2, avail.top() + theme.px(40)
    else:
        return
    if avail:
        x = max(avail.left() + 8, min(x, avail.right() - w - 8))
        y = max(avail.top() + 8, min(y, avail.bottom() - h - 8))
    win.move(x, y)


# ============================================================ 独立预览窗口

class PreviewWindow(QDialog):
    """大窗口预览：可拉伸、可缩放（按钮 / Ctrl+滚轮）。"""

    def __init__(self, parent, md_text, cover="", note="", zoom=1.0, base_dir="",
                 title="预览"):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._md, self._cover, self._note = md_text, cover, note
        self._zoom = zoom
        self._drag = None

        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        w, h = theme.px(1080), theme.px(820)
        if avail:
            w = min(w, int(avail.width() * 0.92))
            h = min(h, int(avail.height() * 0.9))
        self.resize(w, h)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 48, QColor(30, 70, 105, 90), 8)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(16), theme.px(14), theme.px(16), theme.px(12))
        lay.setSpacing(theme.px(8))

        head = QHBoxLayout()
        head.setSpacing(theme.px(8))
        t = QLabel(title)
        t.setObjectName("H1")
        head.addWidget(t)
        self.lbl_zoom = QLabel("100%")
        self.lbl_zoom.setStyleSheet(
            f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11.5)}px; background:transparent;"
            f"min-width:{theme.px(40)}px;")
        self.lbl_zoom.setAlignment(Qt.AlignCenter)
        head.addStretch(1)
        head.addWidget(self._mini("-", self.zoom_out))
        head.addWidget(self.lbl_zoom)
        head.addWidget(self._mini("+", self.zoom_in))
        reset = self._mini("重置", self.zoom_reset)
        reset.setFixedWidth(theme.px(52))
        head.addWidget(reset)
        x = self._mini("✕", self.close)
        x.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:8px;padding:0px;"
            "color:#7d9cb5;font-size:13px;}"
            "QPushButton:hover{background:#f0a0aa;color:#ffffff;}")
        head.addWidget(x)
        lay.addLayout(head)

        self.browser = QTextBrowser()
        self.browser.setObjectName("Preview")
        self.browser.setOpenExternalLinks(True)
        self.browser.setFocusPolicy(Qt.StrongFocus)
        if base_dir:
            self.browser.setSearchPaths([base_dir])

        self.find_bar = FindBar(self.browser)
        self.btn_find = make_search_btn()
        # 塞进标题栏那一行（放在缩放按钮左边）：行高不变，弹出时不会把浏览区往下挤
        head.insertWidget(2, self.find_bar)
        head.insertWidget(3, self.btn_find)
        self.btn_find.clicked.connect(self.find_bar.toggle)
        self.find_bar.closed.connect(lambda: self.btn_find.setChecked(False))
        lay.addWidget(self.browser, 1)
        sc = QShortcut(QKeySequence.Find, self)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self._open_find)

        # 右下角拉伸把手
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(card))
        lay.addLayout(grip_row)

        self._apply()
        _centre_preview(self, parent)

    def _mini(self, text, slot, tip=""):
        b = QPushButton(text)
        b.setFixedSize(theme.px(34), theme.px(26))
        b.setCursor(Qt.PointingHandCursor)
        # padding 必须显式归零：全局 QPushButton 规则带 9px 18px 内边距，
        # 在这些 34px 宽的小按钮上会把文字整个挤没（看起来就是空框）
        b.setStyleSheet(
            f"QPushButton{{background:rgba(255,255,255,0.75); color:{theme.PRIMARY_PRESS};"
            f"border:1px solid {theme.BORDER}; border-radius:8px; padding:0px;"
            f"font-size:{theme.px(12)}px;}}"
            f"QPushButton:hover{{background:#ffffff; border-color:{theme.PRIMARY};}}")
        b.clicked.connect(slot)
        return b

    # ---- 缩放 ----
    def set_zoom(self, z):
        z = max(MIN_ZOOM, min(MAX_ZOOM, round(z, 2)))
        if abs(z - self._zoom) < 0.001:
            return
        self._zoom = z
        self._apply()

    def zoom_in(self):
        self.set_zoom(self._zoom + ZOOM_STEP)

    def zoom_out(self):
        self.set_zoom(self._zoom - ZOOM_STEP)

    def zoom_reset(self):
        self.set_zoom(1.0)

    def _open_find(self):
        self.find_bar.open()
        self.btn_find.setChecked(True)

    def _apply(self):
        sb = self.browser.verticalScrollBar()
        old_max = max(1, sb.maximum())
        ratio = sb.value() / float(old_max)
        self.browser.setHtml(build_html(self._md, self._zoom, self._cover, self._note))
        sb.setValue(int(round(ratio * sb.maximum())))
        if hasattr(self, "find_bar"):
            self.find_bar.refresh()
        self.lbl_zoom.setText("%d%%" % round(self._zoom * 100))

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.set_zoom(self._zoom + (ZOOM_STEP if e.angleDelta().y() > 0 else -ZOOM_STEP))
            e.accept()
            return
        super().wheelEvent(e)

    # ---- 拖动标题栏 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() <= theme.px(48):
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)


# ============================================================ 工具页面

class JsonMdPage(QWidget):
    """左边粘 JSON，右边直接看排版效果。"""

    def __init__(self, on_wordcount=None, on_translate=None, parent=None):
        super().__init__(parent)
        self._on_wordcount = on_wordcount
        self._on_translate = on_translate
        self._md = ""
        self._cover = ""
        self._note = ""
        self._base_dir = ""
        self._big = None
        self._ok = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, theme.px(6), 0, 0)
        lay.setSpacing(theme.px(10))

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(theme.px(10))

        # ---------- 左：JSON ----------
        left = QFrame()
        left.setObjectName("Card")
        shadow(left)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(theme.px(14), theme.px(12), theme.px(14), theme.px(12))
        ll.setSpacing(theme.px(8))

        lh = QHBoxLayout()
        lh.addWidget(self._label("JSON 输入", 13, theme.TEXT, 700))
        lh.addStretch(1)
        # 显示当前取正文用的键（设置里能改），省的猜「它到底读哪个字段」
        self.lbl_keys = self._label("", 10.5, theme.TEXT_FAINT, 400)
        lh.addWidget(self.lbl_keys)
        ll.addLayout(lh)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("Code")
        self.editor.setPlaceholderText(
            '把投稿 JSON 粘到这里，例如：\n\n'
            '{\n  "ai_content": {\n    "title": "文章标题",\n'
            '    "cover": "https://...",\n    "markdown": "# 标题\\n\\n正文…"\n  }\n}')
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSizeF(theme.pt(10))
        self.editor.setFont(f)
        self.editor.setMinimumHeight(theme.px(200))
        self.editor.textChanged.connect(self._on_changed)
        ll.addWidget(self.editor, 1)

        row = QHBoxLayout()
        row.setSpacing(theme.px(8))
        b_open = self._btn("打开 JSON 文件", self.open_file, primary=True)
        b_paste = self._btn("从剪贴板", self.paste_clipboard)
        b_clear = self._btn("清空", self.clear_all)
        row.addWidget(b_open)
        row.addWidget(b_paste)
        row.addWidget(b_clear)
        row.addStretch(1)
        ll.addLayout(row)
        split.addWidget(left)

        # ---------- 右：预览 ----------
        right = QFrame()
        right.setObjectName("Card")
        shadow(right)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(theme.px(14), theme.px(12), theme.px(14), theme.px(12))
        rl.setSpacing(theme.px(8))

        rh = QHBoxLayout()
        rh.setSpacing(theme.px(8))
        rh.addWidget(self._label("Markdown 预览", 13, theme.TEXT, 700))
        self.lbl_state = self._label("等待输入…", 10.5, theme.TEXT_FAINT, 400)
        rh.addWidget(self.lbl_state)
        rh.addStretch(1)
        self.btn_copy = self._btn("复制 Markdown", self.copy_markdown)
        self.btn_big = self._btn("大窗口预览", self.open_big, primary=True)
        self.btn_copy.setEnabled(False)
        self.btn_big.setEnabled(False)
        rh.addWidget(self.btn_copy)
        rh.addWidget(self.btn_big)
        rl.addLayout(rh)

        self.preview = QTextBrowser()
        self.preview.setObjectName("Preview")
        self.preview.setOpenExternalLinks(True)

        # Ctrl+F 在这个页面（含输入框、预览）里都能唤起搜索条
        self.find_bar = FindBar(self.preview)
        self.btn_find = make_search_btn()
        # 和状态文字换位：搜索时就别挤「取自 xxx · N 字符」了，省出横向空间
        rh.insertWidget(2, self.find_bar)
        rh.insertWidget(rh.indexOf(self.btn_copy), self.btn_find)
        self.btn_find.clicked.connect(self._toggle_find)
        self.find_bar.closed.connect(self._on_find_closed)
        rl.addWidget(self.preview, 1)
        sc = QShortcut(QKeySequence.Find, self)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self._open_find)

        # 底部操作条：立即更新 / 送去别的工具接着用
        foot = QHBoxLayout()
        foot.setSpacing(theme.px(8))
        self.btn_update = self._btn("更新预览", self.refresh_now)
        self.btn_update.setShortcut("Ctrl+Return")
        foot.addWidget(self.btn_update)
        self.btn_wc = self._btn("统计字数", self.send_to_wordcount)
        self.btn_wc.setEnabled(False)
        foot.addWidget(self.btn_wc)
        # 送去翻译：只把正文塞进翻译输入框并跳过去，**不自动发请求** ——
        # 翻译是按字符烧钱的，发不发得由用户自己按「翻译」决定。
        self.btn_tr = self._btn("送去翻译", self.send_to_translate)
        self.btn_tr.setToolTip("把正文放进翻译的输入框，翻译仍要自己点「翻译」按钮")
        self.btn_tr.setEnabled(False)
        foot.addWidget(self.btn_tr)
        self.lbl_updated = self._label("", 10.5, theme.TEXT_FAINT, 400)
        foot.addWidget(self.lbl_updated)
        foot.addStretch(1)
        rl.addLayout(foot)
        split.addWidget(right)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([theme.px(430), theme.px(560)])
        lay.addWidget(split, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.convert)

        self.apply_settings()
        self._render_empty()

    # ---- 小部件工厂 ----
    def _label(self, text, size, color, weight):
        lb = QLabel(text)
        lb.setStyleSheet(
            f"color:{color}; font-size:{theme.px(size)}px; font-weight:{weight};"
            "background:transparent;")
        return lb

    def _btn(self, text, slot, primary=False):
        b = QPushButton(text)
        b.setObjectName("MiniPrimary" if primary else "Mini")
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(theme.px(32))
        b.clicked.connect(slot)
        return b

    # ---- 输入 ----
    def _on_changed(self):
        self._timer.start()

    def paste_clipboard(self):
        txt = QApplication.clipboard().text()
        if txt.strip():
            self.editor.setPlainText(txt)

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 JSON 文件", self._base_dir or "",
            "JSON 文件 (*.json *.txt);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                self.editor.setPlainText(f.read())
        except UnicodeDecodeError:
            with open(path, "r", encoding="gbk", errors="replace") as f:
                self.editor.setPlainText(f.read())
        except Exception as e:
            self._set_state("读不了这个文件：%s" % e, error=True)
            return
        self._base_dir = os.path.dirname(path)
        self.preview.setSearchPaths([self._base_dir])
        self.convert()

    def clear_all(self):
        self.editor.clear()
        self._md, self._cover, self._note, self._ok = "", "", "", False
        self._render_empty()
        self._set_state("等待输入…", error=False)
        self.btn_copy.setEnabled(False)
        self.btn_big.setEnabled(False)
        self.btn_wc.setEnabled(False)
        self.btn_tr.setEnabled(False)

    def copy_markdown(self):
        if not self._md:
            return
        QApplication.clipboard().setText(self._md)
        self._set_state("Markdown 已复制", error=False)

    # ---- 转换 ----
    def _open_find(self):
        # 这一行放不下「标题 + 状态 + 搜索条 + 两个按钮」，搜索期间把次要的收起来，
        # 关掉搜索立刻恢复（否则硬挤会把文字压出省略号）
        self.lbl_state.setVisible(False)
        self.btn_copy.setVisible(False)
        self.btn_big.setVisible(False)
        self.find_bar.open()
        self.btn_find.setChecked(True)

    def _toggle_find(self):
        if self.find_bar.isVisible():
            self.find_bar.close_bar()
        else:
            self._open_find()

    def _on_find_closed(self):
        self.lbl_state.setVisible(True)
        self.btn_copy.setVisible(True)
        self.btn_big.setVisible(True)
        self.btn_find.setChecked(False)

    def refresh_now(self):
        """立刻重转一次（不等防抖）。改完想马上看效果，或者按 Ctrl+Enter。"""
        self._timer.stop()
        self.convert()

    def convert(self):
        try:
            self._convert()
        except Exception as e:          # 出问题要看得见，不能悄悄不动
            self._md, self._ok = "", False
            self._render_empty("转换出错：%s" % e, error=True)
            self._set_state("转换出错：%s" % type(e).__name__, error=True)

    def _convert(self):
        res = extract(self.editor.toPlainText())
        if not res["ok"]:
            self._md, self._ok = "", False
            placeholder = res.get("error") or "等待输入…"
            self._render_empty(placeholder, error=bool(res.get("error")))
            if res.get("error"):
                self._set_state(res["error"][:46], error=True)
            else:
                self._set_state("等待输入…", error=False)
            self.btn_copy.setEnabled(False)
            self.btn_big.setEnabled(False)
            self.btn_wc.setEnabled(False)
            self.btn_tr.setEnabled(False)
            return

        self._md = res["markdown"]
        self._cover = res["cover"]
        self._note = res["note"]
        self._ok = True
        # 保留原来的滚动位置：改一句话不至于整个预览跳回顶部
        sb = self.preview.verticalScrollBar()
        old_max = max(1, sb.maximum())
        ratio = sb.value() / float(old_max)
        self.preview.setHtml(build_html(self._md, 1.0, self._cover, self._note))
        sb.setValue(int(round(ratio * sb.maximum())))
        import time as _t
        self.lbl_updated.setText("更新于 " + _t.strftime("%H:%M:%S"))
        self.find_bar.refresh()
        n = len(self._md)
        if res["kind"] == "article" and res.get("key"):
            kind = "取自 %s" % res["key"]
        else:
            kind = "通用 JSON"
        bits = [kind, "{:,} 字符".format(n)]
        if res["note"]:
            bits.append(res["note"])
        self._set_state(" · ".join(bits), error=False)
        self.btn_copy.setEnabled(True)
        self.btn_big.setEnabled(True)
        self.btn_wc.setEnabled(True)
        self.btn_tr.setEnabled(True)

    def _render_empty(self, text="", error=False):
        color = "#c06a72" if error else "#a8c0d2"
        self.preview.setHtml(
            '<div style="color:%s; font-size:%dpx; margin-top:%dpx;">%s</div>'
            % (color, theme.px(12), theme.px(30),
               (text or "预览会显示在这里").replace("<", "&lt;")))

    def _set_state(self, text, error=False):
        # 太长会把右边的按钮挤出去，截断就好（完整信息在预览里能看到）
        text = text if len(text) <= 64 else text[:62] + "…"
        self.lbl_state.setText(text)
        self.lbl_state.setStyleSheet(
            f"color:{'#c06a72' if error else theme.TEXT_MUTED};"
            f"font-size:{theme.px(10.5)}px; background:transparent;")
        self.lbl_state.setText(text)

    def send_to_wordcount(self):
        """把当前 Markdown 交给字数统计工具，并跳过去。"""
        if not self._md:
            return
        if self._on_wordcount:
            self._on_wordcount(self._md)

    def send_to_translate(self):
        """把当前 Markdown 放进翻译的输入框并跳过去。

        刻意**不触发翻译**：翻译按字符计费，什么时候发请求应该由用户按
        「翻译」按钮决定。这里只做搬运。
        """
        if not self._md:
            return
        if self._on_translate:
            self._on_translate(self._md)

    def apply_settings(self):
        """设置里改了正文键 / 排除键：更新提示，并立刻按新规则重转一次。"""
        keys = store.jm_keys()
        ex = set(store.jm_exclude())
        eff = [k for k in keys if k not in ex]
        self.lbl_keys.setText("取字段：" + " → ".join(eff[:4]) + ("…" if len(eff) > 4 else ""))
        if self.editor.toPlainText().strip():
            self.refresh_now()

    def open_big(self):
        if not self._md:
            return
        if self._big is None:
            self._big = PreviewWindow(self.window(), self._md, self._cover, self._note,
                                      base_dir=self._base_dir)
        else:
            self._big._md, self._big._cover, self._big._note = self._md, self._cover, self._note
            self._big._apply()
        self._big.show()
        self._big.raise_()
        self._big.activateWindow()

    def showEvent(self, e):
        super().showEvent(e)
        if self.editor.toPlainText().strip() and not self._ok:
            self._timer.start(0)


# ============================================================ 设置面板

KEY_HINTS = {
    "markdown": "标准正文（最常见）",
    "md": "简写",
    "content": "通用内容字段",
    "body": "正文",
    "article": "整篇文章",
    "text": "纯文本",
}


def _split_keys(text):
    """把「a, b，c」拆成去重后的列表（中英文逗号、空格都认）。"""
    out = []
    for part in (text or "").replace("，", ",").replace(" ", ",").split(","):
        k = part.strip().strip("\"'")
        if k and k not in out:
            out.append(k)
    return out


class JsonMdSettings(QWidget):
    """JSON 转 Markdown 的设置：取哪些键当正文、哪些键不要。"""

    def __init__(self, on_change=None, parent=None):
        super().__init__(parent)
        self._on_change = on_change
        self._boxes = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.px(4), theme.px(4), theme.px(4), theme.px(4))
        lay.setSpacing(theme.px(10))

        t = QLabel("从哪些字段取正文")
        t.setStyleSheet(
            f"color:{theme.TEXT}; font-size:{theme.px(13)}px; font-weight:700;"
            "background:transparent;")
        lay.addWidget(t)
        hint = QLabel("按顺序逐个找，取第一个「有内容的字符串」——所以 JSON 里同时有 markdown 和 "
                      "summary 时，只会用排在前面的那个，另一个完全不会混进正文。")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(11)}px; background:transparent;")
        lay.addWidget(hint)

        keys = store.jm_keys()
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.px(16))
        grid.setVerticalSpacing(theme.px(8))
        for i, key in enumerate(store.JM_KEYS):
            cb = QCheckBox("%s　（%s）" % (key, KEY_HINTS.get(key, "")))
            cb.setChecked(key in keys)
            cb.setCursor(Qt.PointingHandCursor)
            cb.stateChanged.connect(self._commit)
            self._boxes[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        lay.addLayout(grid)

        extras = [k for k in keys if k not in store.JM_KEYS]

        def _lab(text):
            lb = QLabel(text)
            lb.setStyleSheet(f"color:{theme.TEXT}; font-size:{theme.px(12)}px;"
                             "background:transparent;")
            lb.setFixedWidth(theme.px(96))
            return lb

        row1 = QHBoxLayout()
        row1.setSpacing(theme.px(8))
        self.edit_extra = QLineEdit(", ".join(extras))
        self.edit_extra.setPlaceholderText("比如 summary, body_html（逗号分隔，可留空）")
        self.edit_extra.editingFinished.connect(self._commit)
        row1.addWidget(_lab("另外要提取"), 0, Qt.AlignVCenter)
        row1.addWidget(self.edit_extra, 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(theme.px(8))
        self.edit_exclude = QLineEdit(", ".join(store.jm_exclude()))
        self.edit_exclude.setPlaceholderText("比如 summary, text（优先于上面的勾选）")
        self.edit_exclude.editingFinished.connect(self._commit)
        row2.addWidget(_lab("不要提取"), 0, Qt.AlignVCenter)
        row2.addWidget(self.edit_exclude, 1)
        lay.addLayout(row2)

        self.lbl_order = QLabel("")
        self.lbl_order.setWordWrap(True)
        self.lbl_order.setStyleSheet(
            f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11)}px; background:transparent;")
        lay.addWidget(self.lbl_order)

        note = QLabel("想只认某一个字段：把其他都取消勾选、再清空「另外要提取」。"
                      "改动即时生效，存在 data/settings.json。")
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;")
        lay.addWidget(note)
        lay.addStretch(1)

        self._refresh()

    def _refresh(self):
        keys = store.jm_keys()
        ex = set(store.jm_exclude())
        eff = [k for k in keys if k not in ex]
        dead = [k for k in keys if k in ex]
        txt = "当前顺序：" + (" → ".join(eff) if eff else "（空，会用 markdown）")
        if dead:
            txt += "　　已排除：" + "、".join(dead) + "（不生效）"
        self.lbl_order.setText(txt)

    def _commit(self):
        picked = [k for k in store.JM_KEYS if self._boxes[k].isChecked()]
        keys = []
        for k in picked + _split_keys(self.edit_extra.text()):
            if k not in keys:
                keys.append(k)
        if not keys:                       # 全取消了就没得取了，至少留一个
            keys = ["markdown"]
            self._boxes["markdown"].setChecked(True)
        store.save_jm_keys(keys, _split_keys(self.edit_exclude.text()))
        self._refresh()
        if self._on_change:
            self._on_change()

# ============================================================ 预览里搜索

class FindBar(QWidget):
    """预览里的搜索条：内联放在标题栏那一行，Ctrl+F / 放大镜按钮 打开。

    Enter 下一个，Shift+Enter 上一个，Esc 关闭。
    命中数是自己用 QTextDocument.find 走一遍算的，所以能显示「3/12」并把所有命中标黄。
    """

    closed = Signal()
    MAX_HITS = 3000          # 命中太多就不数了，避免超大文档卡住

    def __init__(self, browser, parent=None):
        super().__init__(parent)
        self._browser = browser
        self._hits = []
        self._idx = -1
        self.setVisible(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.px(5))

        self.edit = QLineEdit()
        self.edit.setObjectName("FindEdit")
        self.edit.setPlaceholderText("在预览里搜索…")
        self.edit.setFixedWidth(theme.px(150))
        self.edit.setFixedHeight(theme.px(26))
        self.edit.textChanged.connect(self._on_text)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit)

        self.lbl = QLabel("")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setFixedWidth(theme.px(42))
        lay.addWidget(self.lbl)

        self.btn_prev = self._btn("↑", self.prev_hit)
        self.btn_next = self._btn("↓", self.next_hit)
        self.btn_close = self._btn("✕", self.close_bar)
        for b in (self.btn_prev, self.btn_next, self.btn_close):
            lay.addWidget(b)
        self._style_label()

    def _btn(self, text, slot):
        b = QPushButton(text)
        b.setFixedSize(theme.px(24), theme.px(26))
        b.setCursor(Qt.PointingHandCursor)
        # padding 归零：全局 QPushButton 的 9px 18px 内边距会把 24px 宽按钮的字挤没
        b.setStyleSheet(
            f"QPushButton{{background:#ffffff; color:{theme.PRIMARY_PRESS};"
            f"border:1px solid {theme.BORDER}; border-radius:7px; padding:0px;"
            f"font-size:{theme.px(12)}px;}}"
            f"QPushButton:hover{{border-color:{theme.PRIMARY};}}"
            f"QPushButton:disabled{{color:#b9cfe0; border-color:#dfeaf4;}}")
        b.clicked.connect(slot)
        return b

    def _style_label(self):
        ok = bool(self._hits)
        empty = not self.edit.text()
        color = theme.TEXT_MUTED if (ok or empty) else "#c06a72"
        self.lbl.setText("" if empty else ("%d/%d" % (self._idx + 1, len(self._hits)) if ok
                                           else "无结果"))
        self.lbl.setStyleSheet(
            f"color:{color}; font-size:{theme.px(11)}px; background:transparent;")
        self.btn_prev.setEnabled(ok)
        self.btn_next.setEnabled(ok)

    # ---- 开 / 关 ----
    def toggle(self):
        if self.isVisible():
            self.close_bar()
        else:
            self.open()

    def open(self):
        sel = self._browser.textCursor().selectedText().strip()
        if sel and "\n" not in sel and len(sel) <= 60:
            self.edit.setText(sel)
        self.show()
        self.edit.setFocus()
        self.edit.selectAll()
        self._on_text()          # 同一段文字再搜一次也要刷新（文档可能重绘过）

    def close_bar(self):
        self.hide()
        self._browser.setExtraSelections([])
        self._browser.setFocus()
        self.closed.emit()

    def refresh(self):
        """预览重新渲染后调用：命中位置全变了，重新算一遍。"""
        if self.isVisible():
            self._on_text()

    # ---- 搜索 ----
    def _collect(self):
        text = self.edit.text()
        if not text:
            return []
        doc = self._browser.document()
        flags = QTextDocument.FindFlags()
        hits = []
        cur = QTextCursor(doc)
        while len(hits) < self.MAX_HITS:
            cur = doc.find(text, cur, flags)
            if cur.isNull():
                break
            hits.append((cur.selectionStart(), cur.selectionEnd()))
        return hits

    def _on_text(self):
        self._hits = self._collect()
        self._idx = -1
        self._paint()
        if self._hits:
            self.next_hit(from_scratch=True)
        else:
            self._style_label()

    def _paint(self):
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#fff0a8"))
        sels = []
        for s, e in self._hits[:600]:
            c = QTextCursor(self._browser.document())
            c.setPosition(s)
            c.setPosition(e, QTextCursor.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = c
            sel.format = fmt
            sels.append(sel)
        self._browser.setExtraSelections(sels)

    def _goto(self):
        s, e = self._hits[self._idx]
        c = QTextCursor(self._browser.document())
        c.setPosition(s)
        c.setPosition(e, QTextCursor.KeepAnchor)
        self._browser.setTextCursor(c)
        self._browser.ensureCursorVisible()
        self._style_label()

    def next_hit(self, from_scratch=False):
        if not self._hits:
            self._style_label()
            return
        self._idx = 0 if from_scratch else (self._idx + 1) % len(self._hits)
        self._goto()

    def prev_hit(self):
        if not self._hits:
            self._style_label()
            return
        self._idx = (self._idx - 1) % len(self._hits)
        self._goto()

    def eventFilter(self, obj, e):
        if obj is self.edit and e.type() == QEvent.KeyPress:
            if e.key() in (Qt.Key_Return, Qt.Key_Enter):
                if e.modifiers() & Qt.ShiftModifier:
                    self.prev_hit()
                else:
                    self.next_hit()
                return True
            if e.key() == Qt.Key_Escape:
                self.close_bar()
                return True
        return super().eventFilter(obj, e)


def make_search_btn():
    """标题栏上的放大镜按钮（可勾选，用来开合搜索条）。"""
    from widgets import svg_icon
    b = QPushButton()
    b.setCheckable(True)
    b.setFixedSize(theme.px(30), theme.px(26))
    b.setCursor(Qt.PointingHandCursor)
    b.setIcon(svg_icon("搜索.svg", theme.px(14), tint="#7d9cb5",
                       hover_tint=theme.PRIMARY_PRESS))
    b.setIconSize(QSize(theme.px(14), theme.px(14)))
    b.setStyleSheet(
        "QPushButton{background:transparent;border:1px solid transparent;"
        "border-radius:7px;padding:0px;}"
        f"QPushButton:hover{{background:#ffffff;border-color:{theme.BORDER};}}"
        f"QPushButton:checked{{background:{theme.PRIMARY_SOFT};"
        f"border-color:{theme.PRIMARY};}}")
    return b
