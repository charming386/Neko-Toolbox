# -*- coding: utf-8 -*-
"""wordcount.py —— 字数统计工具（页面 + 设置面板 + 统计函数）。

统计口径（避免歧义，界面上也写了提示）：
  字数        = 中文汉字数 + 英文单词数   （中文写作里最常用的「字数」）
  字符数      = 全部字符数，含空格与换行
  行数        = 按换行切分的行数
  段落数      = 按回车分段的段数（单独的空行不算一段，等于非空行数）
  中文字数    = 汉字个数
  中文字符数  = 汉字 + 中文标点
  英文词数    = 英文单词个数
  英文字符数  = 英文字母个数（不含空格）

性能：全部是纯字符串/正则扫描，1 万字以内基本 1~3ms，10 万字也就几十毫秒，
所以做成「输入停止 120ms 后自动统计」，不需要按钮，也不会占着 CPU。

⚠️ 查找条的高亮必须「延迟 + 限量」：正文一变就全量重算的话，删除大段内容时
textChanged 会连发几十次，每次上百毫秒，事件队列直接堆死（表现为整个工具箱卡住）。
"""
import bisect
import os
import re
import time

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QKeySequence, QShortcut, QTextCharFormat,
                           QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QTextEdit,
                               QVBoxLayout, QWidget)

import store
import theme
from widgets import shadow, svg_icon

# 汉字：基本区 + 扩展 A + 兼容区
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# 中文标点：CJK 标点区 + 全角符号区
_CJK_PUNCT = "\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
_RE_CJK = re.compile("[%s]" % _CJK)
_RE_CJK_SYM = re.compile("[%s%s]" % (_CJK, _CJK_PUNCT))
_RE_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*")
_RE_EN_CHAR = re.compile(r"[A-Za-z]")
_RE_PARAS = re.compile(r"\n\s*\n")

# (key, 标题, 说明)  第一个是必选项，不能取消
FIELDS = [
    ("words", "字数", "中文汉字数 + 英文单词数"),
    ("chars", "字符数", "全部字符数，含空格与换行"),
    ("lines", "行数", "按换行切分的行数"),
    ("paras", "段落数", "按回车分段；空行不额外算一段"),
    ("cjk", "中文字数", "汉字个数"),
    ("cjksym", "中文字符数", "汉字 + 中文标点"),
    ("en_words", "英文词数", "英文单词个数"),
    ("en_chars", "英文字符数", "英文字母个数（不含空格）"),
]
ALWAYS_ON = "words"

# ---- 卡顿留证 ----
# 正常使用不产生任何输出；只有界面真的被卡住超过 PERF_MIN_MS 才往 data/perf.log 记一行。
# 起因：用户反馈「查找 + 删除一大段 + 撤销重做后整个工具箱卡死」，但本地各种文档规模、
# 单行/多行、emoji、全选删除都复现不出来（最坏 200ms 级）。既然猜不准，就让程序自己留证据：
# 日志里能看出卡了多久、当时文档多大、多少段、命中多少、查找条开没开。
PERF_MIN_MS = 400


def _perf(tag, ms, extra=""):
    if ms < PERF_MIN_MS:
        return
    try:
        import datetime
        store.ensure_dir()
        with open(os.path.join(store.DATA_DIR, "perf.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s\t%-14s\t%7.0f ms\t%s\n" % (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                tag, ms, extra))
    except Exception:
        pass            # 留证本身绝不能反过来影响功能


def _n(rx, t):
    """数一个正则匹配了多少次。

    不用 len(rx.findall(t))：10 万字的文本里汉字占一半，findall 会立刻造出
    几万个临时字符串对象，既慢又吃内存。subn 在 C 层边扫边数，实测快 3~4 倍，
    100 万字 92ms vs 296ms。
    """
    return rx.subn("", t)[1]


def count_text(t, paras_mode="enter"):
    """一次扫描算出所有统计项。

    paras_mode: "enter" 按回车分段（非空行数，空行不额外算）/"blank" 按空行分段。
    """
    if not t:
        return {k: 0 for k, _n_, _d in FIELDS}
    cjk = _n(_RE_CJK, t)
    en_words = _n(_RE_EN_WORD, t)
    return {
        "words": cjk + en_words,
        "chars": len(t),
        "lines": len(t.splitlines()),
        "paras": (len([ln for ln in t.splitlines() if ln.strip()])
                  if paras_mode != "blank"
                  else len([b for b in _RE_PARAS.split(t) if b.strip()])),
        "cjk": cjk,
        "cjksym": _n(_RE_CJK_SYM, t),
        "en_words": en_words,
        "en_chars": _n(_RE_EN_CHAR, t),
    }


class FindBar(QWidget):
    """输入框上方的查找条：Ctrl+F 打开，Enter 下一个，Shift+Enter 上一个，Esc 关闭。

    和预览里的搜索条同一套手感，但作用对象是 QPlainTextEdit：
    QPlainTextEdit 没有富文本的 find 高亮，所以命中位置要自己用
    QTextDocument.find 走一遍算出来，再用 setExtraSelections 标黄，
    当前命中靠 setTextCursor 的选区高亮表示（避免再画一层）。

    性能上有四条硬规矩，少一条都会在大文档里卡死：
      1. 正文变化（refresh）只「打标记」，延迟 REFRESH_MS 再算一次 —— 删除大段
         内容时 textChanged 会连发几十次，每次都全量扫描就是几百毫秒的活。
      2. 正文变化时绝不碰光标。正在用 Backspace 删东西时把光标拽到命中处，
         删除就完全失控了，用户看到的就是「卡住 + 乱跳」。
      3. 高亮只画当前命中附近的 PAINT_WINDOW 处。ExtraSelection 挂上去之后
         每次编辑 Qt 都要维护它的位置，几千个会明显拖慢键入。
      4. 收集命中要**分片**（SLICE_MS）：一次扫完 3000 处在几十万字的文档上
         要几百毫秒，那是实打实的卡顿。分片之后每轮只占 8ms，界面永远能响应，
         代价只是高亮分几轮补齐。
    """

    closed = Signal()
    MAX_HITS = 3000          # 命中太多就不数了，避免超大文档卡住
    REFRESH_MS = 260         # 正文变化后延迟这么久再重算
    PAINT_WINDOW = 240       # 只高亮当前命中附近的这么多处
    SLICE_MS = 8             # 每片重算最多占用主线程多少毫秒
    CHUNK = 240              # 每片最多收这么多处，收够就检查一次用时

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._hits = []
        self._idx = -1
        self._painted = None      # 上次真交给 editor 的高亮区间，避免重复重绘
        self._job = None          # 正在分片进行的收集任务
        self.setVisible(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.px(5))

        self.edit = QLineEdit()
        self.edit.setObjectName("FindEdit")
        self.edit.setPlaceholderText("在文字里查找…")
        self.edit.setFixedWidth(theme.px(168))
        self.edit.setFixedHeight(theme.px(26))
        self.edit.textChanged.connect(self._on_text)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit)

        # 正文变动后的延迟重算（用户停手才真正执行一次）
        self._recalc = QTimer(self)
        self._recalc.setSingleShot(True)
        self._recalc.setInterval(self.REFRESH_MS)
        self._recalc.timeout.connect(self._recalc_hits)

        self.lbl = QLabel("")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setFixedWidth(theme.px(60))      # 容得下「1/3000+」
        lay.addWidget(self.lbl)

        self.btn_prev = self._btn("↑", self.prev_hit)
        self.btn_next = self._btn("↓", self.next_hit)
        self.btn_close = self._btn("✕", self.close_bar)
        for b in (self.btn_prev, self.btn_next, self.btn_close):
            lay.addWidget(b)
        lay.addStretch(1)
        self._style_label()

    def _btn(self, text, slot):
        b = QPushButton(text)
        b.setFixedSize(theme.px(26), theme.px(26))
        b.setCursor(Qt.PointingHandCursor)
        # padding 归零：全局 QPushButton 的内边距会把 26px 宽按钮的字挤没
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
        # 到上限了要显示 3000+，否则用户会以为总共就这么几处
        limit = self._limit()
        capped = len(self._hits) >= limit
        if empty:
            self.lbl.setText("")
        elif ok:
            # _idx 可能是 -1（光标在所有命中之前，还没定位过这次的结果），
            # 这时显示 1 —— 按 Enter 正是会落到第 1 处
            self.lbl.setText("%d/%d%s" % (max(self._idx + 1, 1), len(self._hits),
                                          "+" if capped else ""))
        else:
            self.lbl.setText("无结果")
        self.lbl.setToolTip("命中超过 %d 处，只统计前 %d 处" % (limit, limit)
                            if capped else "")
        self.lbl.setStyleSheet(
            f"color:{color}; font-size:{theme.px(11)}px; background:transparent;")
        self.btn_prev.setEnabled(ok)
        self.btn_next.setEnabled(ok)

    # ---- 开 / 关 ----
    def open(self, preset=""):
        sel = self._editor.textCursor().selectedText().strip()
        if preset:
            self.edit.setText(preset)
        elif sel and "\n" not in sel and len(sel) <= 60:
            self.edit.setText(sel)
        self.show()
        self.edit.setFocus()
        self.edit.selectAll()
        self._on_text()          # 同一段文字再搜一次也要刷新（内容可能变过）

    def close_bar(self):
        self._recalc.stop()
        self._job = None
        self.hide()
        self._painted = None
        self._editor.setExtraSelections([])
        self._editor.setFocus()
        self.closed.emit()

    def refresh(self):
        """正文变了：命中位置全变了，重新算一遍。

        这里**不**同步算。删除大段内容时 textChanged 会连发几十次，每次全量
        扫描 + 重画高亮要上百毫秒，事件队列会一直追不上 —— 整个程序就卡住了。
        改成延迟：用户还在动就一直把计时推后，手一停才算一次。也不去动光标，
        不然正在删的字会被拽到别处。
        """
        if not self.isVisible():
            self._recalc.stop()
            self._job = None
            return
        self._job = None        # 在跑的分片作废，等这次延迟到点重新开始
        self._recalc.start(self.REFRESH_MS)

    def _flush(self):
        """有挂起的重算就当场跑完（用户按上下箭头/回车时要用最新命中）。"""
        if self._recalc.isActive():
            self._recalc.stop()
            self._recalc_hits()
        if self._job is not None:
            self._step_job(full=True)

    # ---- 搜索 ----
    def _limit(self):
        """超大文档降低计数上限：30 万字以上扫 3000 处太费时间，1000 处够定位了。"""
        if self._editor.document().characterCount() < 300000:
            return self.MAX_HITS
        return 1000

    def _on_text(self):
        """查找词变了（用户主动输入）：立刻重算，并跳到第一处命中。

        第一片给 60ms 预算：普通文档（十万字以内）一次就能跑完，用户看到的
        是「输入即出结果」；只有超大文档才会继续分片，不至于边打字边卡。
        """
        self._recalc.stop()
        self._recalc_hits(jump=True, first_ms=60)

    # ---- 收集命中（分片执行，绝不长时间占住主线程）----
    def _recalc_hits(self, jump=False, first_ms=None):
        self._job = {
            "token": object(),
            "jump": jump,
            "needle": self.edit.text(),
            "hits": [],
            "cur": QTextCursor(self._editor.document()),
            "limit": self._limit(),
        }
        self._step_job(first_ms=first_ms)

    def _step_job(self, token=None, full=False, first_ms=None):
        """跑一片收集。完整跑完就收尾，没跑完就排队下一轮事件循环。

        full=True 是不分片的「一次跑到底」—— 只在用户主动按键（Enter/箭头）
        等着要结果时用，宁可阻塞这一下，也不能给出过期的命中。
        """
        job = self._job
        if job is None or (token is not None and job["token"] is not token):
            return              # 任务已被新一轮替换 / 取消
        needle = job["needle"]
        if not needle:
            self._finish_job(job)
            return
        doc = self._editor.document()
        flags = QTextDocument.FindFlags()
        hits = job["hits"]
        cur = job["cur"]
        limit = job["limit"]
        last = hits[-1][0] if hits else -1
        if full:
            budget = 60.0
        elif first_ms is not None:
            budget = first_ms / 1000.0
        else:
            budget = self.SLICE_MS / 1000.0
        deadline = time.perf_counter() + budget
        while len(hits) < limit:
            cur = doc.find(needle, cur, flags)
            if cur.isNull():
                break
            s, e = cur.selectionStart(), cur.selectionEnd()
            if s <= last:
                # 位置没往前走 —— 正常匹配长度至少 1，这里只是防死循环兜底，
                # 万一真出现会转成几十万圈的忙等，界面彻底无响应
                break
            last = s
            hits.append((s, e))
            if len(hits) % self.CHUNK == 0 and time.perf_counter() > deadline:
                job["cur"] = cur
                tok = job["token"]
                QTimer.singleShot(0, lambda: self._step_job(tok))
                return
        job["cur"] = cur
        self._finish_job(job)

    def _finish_job(self, job):
        t0 = time.perf_counter()
        hits = job["hits"]
        self._job = None
        changed = (hits != self._hits)
        self._hits = hits
        n = len(hits)
        if job["jump"]:
            self._idx = 0 if n else -1
        elif not n:
            self._idx = -1
        else:
            # 不跳转时把「当前」定在光标之前最近的一处：用户删改完接着按 Enter，
            # 正好落到光标之后第一处，和标签上「下一个」的语义对齐；位置也顺便
            # 当高亮窗口的中心，窗口自然跟着光标走。
            pos = self._editor.textCursor().position()
            j = bisect.bisect_left([h[0] for h in hits], pos)
            self._idx = min(j, n) - 1
        if changed:
            self._paint(force=True)
        if job["jump"] and n:
            self._goto()
        else:
            self._style_label()
        doc = self._editor.document()
        _perf("收集命中", (time.perf_counter() - t0) * 1000.0,
              "命中 %d / 文档 %d 字 / %d 段"
              % (n, doc.characterCount(), doc.blockCount()))

    def _paint(self, force=False):
        """只画当前命中附近的窗口。

        force=True 表示命中集合本身变了（位置全变），必须重画；
        否则窗口没挪动就跳过 —— setExtraSelections 会触发一次完整的视口重绘。
        """
        hits = self._hits
        if not hits:
            if self._painted is not None:
                self._painted = None
                self._editor.setExtraSelections([])
            return
        n = len(hits)
        w = self.PAINT_WINDOW
        if n <= w:
            lo, hi = 0, n
        else:
            idx = self._idx if self._idx >= 0 else 0
            lo = max(0, min(idx - w // 2, n - w))
            hi = lo + w
        if not force and self._painted == (lo, hi):
            return
        self._painted = (lo, hi)
        t0 = time.perf_counter()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#fff0a8"))
        doc = self._editor.document()
        sels = []
        for s, e in hits[lo:hi]:
            c = QTextCursor(doc)
            c.setPosition(s)
            c.setPosition(e, QTextCursor.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = c
            sel.format = fmt
            sels.append(sel)
        self._editor.setExtraSelections(sels)
        _perf("重画高亮", (time.perf_counter() - t0) * 1000.0,
              "%d 处 / 文档 %d 字 / %d 段"
              % (len(sels), doc.characterCount(), doc.blockCount()))

    def _goto(self):
        if not (0 <= self._idx < len(self._hits)):
            self._style_label()
            return
        t0 = time.perf_counter()
        s, e = self._hits[self._idx]
        doc = self._editor.document()
        top = max(0, doc.characterCount() - 1)
        # 命中的位置可能来自上一次统计（正文刚被删过），越界就夹回去，
        # 否则 setPosition 会静默失败、光标乱跳
        s = min(s, top)
        e = min(max(e, s), top)
        c = QTextCursor(doc)
        c.setPosition(s)
        c.setPosition(e, QTextCursor.KeepAnchor)
        self._editor.setTextCursor(c)
        self._editor.centerCursor()      # 居中比 ensureCursorVisible 好找
        self._paint()                    # 当前命中换位置了，高亮窗口跟着挪
        self._style_label()
        _perf("跳转命中", (time.perf_counter() - t0) * 1000.0,
              "第 %d/%d 处 / 文档 %d 字 / %d 段"
              % (self._idx + 1, len(self._hits), doc.characterCount(), doc.blockCount()))

    def next_hit(self, from_scratch=False):
        self._flush()
        if not self._hits:
            self._style_label()
            return
        self._idx = 0 if from_scratch else (self._idx + 1) % len(self._hits)
        self._goto()

    def prev_hit(self):
        self._flush()
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


def make_find_btn():
    """输入框右上角的放大镜按钮（可勾选，用来开合查找条）。"""
    b = QPushButton()
    b.setCheckable(True)
    b.setFixedSize(theme.px(26), theme.px(23))
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip("查找（Ctrl+F）")
    b.setIcon(svg_icon("搜索.svg", theme.px(13), tint="#7d9cb5",
                       hover_tint=theme.PRIMARY_PRESS))
    b.setIconSize(QSize(theme.px(13), theme.px(13)))
    b.setStyleSheet(
        "QPushButton{background:rgba(255,255,255,0.86);"
        f"border:1px solid {theme.BORDER_STRONG}; border-radius:8px; padding:0px;}}"
        f"QPushButton:hover{{background:#ffffff; border-color:{theme.PRIMARY};}}"
        f"QPushButton:checked{{background:{theme.PRIMARY_SOFT};"
        f"border-color:{theme.PRIMARY};}}")
    return b


class StatCard(QFrame):
    """一个统计小卡片：大数字 + 名称。"""

    def __init__(self, title, hint, parent=None):
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setToolTip(hint)
        self.setMinimumHeight(theme.px(74))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.px(14), theme.px(10), theme.px(14), theme.px(10))
        lay.setSpacing(theme.px(1))
        self.value = QLabel("0")
        self.value.setStyleSheet(
            f"color:{theme.PRIMARY_PRESS}; font-size:{theme.px(23)}px; font-weight:700;"
            "background:transparent;"
        )
        self.name = QLabel(title)
        self.name.setStyleSheet(
            f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11)}px; background:transparent;"
        )
        lay.addWidget(self.value)
        lay.addWidget(self.name)

    def set_value(self, v):
        self.value.setText("%d" % v)


class WordCountPage(QWidget):
    """字数统计主体：一个大输入框 + 下面的统计卡片。"""

    DEBOUNCE_MS = 120
    WATCHDOG_MS = 100       # 卡顿探针的节拍
    STALL_MIN_MS = 400      # 晚到超过这么多才算「卡了一下」，才写日志

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dirty = True
        self._cards = {}
        self._paras_mode = store.wc_paras_mode()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, theme.px(6), 0, 0)
        lay.setSpacing(theme.px(12))

        # ---- 输入框 ----
        box = QFrame()
        box.setObjectName("Card")
        shadow(box)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(theme.px(14), theme.px(12), theme.px(14), theme.px(12))
        bl.setSpacing(theme.px(6))

        tip = QLabel("把文字粘贴或输入到下面，统计结果会实时更新")
        tip.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(11)}px; background:transparent;"
        )
        # 清空按钮放在这一行右端（输入框右上角，位置由用户指定），
        # 做得比底部按钮小一号，不抢输入框的视觉重心。
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("Mini")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setFixedSize(theme.px(52), theme.px(23))
        self.btn_clear.setToolTip("清空输入框，统计结果归零")
        # ⚠️ #Mini 全局带 padding: 0 14px，52px 宽里放不下两个字 —— 必须显式归零
        self.btn_clear.setStyleSheet(
            f"QPushButton{{background:rgba(255,255,255,0.86); color:{theme.PRIMARY_PRESS};"
            f"border:1px solid {theme.BORDER_STRONG}; border-radius:8px; padding:0px;"
            f"font-size:{theme.px(11)}px;}}"
            f"QPushButton:hover{{background:#ffffff; border-color:{theme.PRIMARY};}}"
            f"QPushButton:pressed{{background:{theme.PRIMARY_SOFT};}}"
            f"QPushButton:disabled{{color:{theme.TEXT_FAINT};"
            f"border-color:rgba(213,232,246,0.9); background:rgba(255,255,255,0.5);}}")
        self.btn_clear.clicked.connect(self.clear_text)

        # 放大镜放在「清空」左边，和它同一行
        self.btn_find = make_find_btn()
        self.btn_find.clicked.connect(self._toggle_find)

        tip_row = QHBoxLayout()
        tip_row.setContentsMargins(0, 0, 0, 0)
        tip_row.setSpacing(theme.px(6))
        tip_row.addWidget(tip)
        tip_row.addStretch(1)
        tip_row.addWidget(self.btn_find)
        tip_row.addWidget(self.btn_clear)
        bl.addLayout(tip_row)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("Editor")
        self.editor.setPlaceholderText("在这里粘贴文字…（纯文本，不带格式）")
        self.editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        f = QFont(theme.FONT_UI)
        f.setPointSizeF(theme.pt(11))
        self.editor.setFont(f)
        self.editor.setMinimumHeight(theme.px(220))
        self.editor.textChanged.connect(self._on_text_changed)

        # 查找条默认收起（隐藏的控件不占布局高度），Ctrl+F 或点放大镜展开
        self.find_bar = FindBar(self.editor)
        self.find_bar.closed.connect(lambda: self.btn_find.setChecked(False))
        bl.addWidget(self.find_bar)

        bl.addWidget(self.editor, 1)
        lay.addWidget(box, 1)

        # Ctrl+F 在整个页面内都能唤起查找（焦点在输入框里时也生效）
        self._sc_find = QShortcut(QKeySequence.Find, self)
        self._sc_find.setContext(Qt.WidgetWithChildrenShortcut)
        self._sc_find.activated.connect(self._open_find)

        # ---- 统计结果 ----
        res = QFrame()
        res.setObjectName("Card")
        shadow(res)
        rl = QVBoxLayout(res)
        rl.setContentsMargins(theme.px(16), theme.px(12), theme.px(16), theme.px(14))
        rl.setSpacing(theme.px(10))

        head = QHBoxLayout()
        head.setSpacing(theme.px(8))
        h = QLabel("统计结果")
        h.setStyleSheet(
            f"color:{theme.TEXT}; font-size:{theme.px(13)}px; font-weight:700;"
            "background:transparent;"
        )
        head.addWidget(h)
        self.lbl_elapsed = QLabel("")
        self.lbl_elapsed.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;"
        )
        head.addStretch(1)
        head.addWidget(self.lbl_elapsed)
        rl.addLayout(head)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(theme.px(10))
        self.grid.setVerticalSpacing(theme.px(10))
        rl.addLayout(self.grid)
        lay.addWidget(res, 0)

        for key, title, hint in FIELDS:
            self._cards[key] = StatCard(title, hint)
        self.apply_settings()

        # 输入变动 → 轻微延迟后统计（连续输入时不会每键都算）
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DEBOUNCE_MS)
        self._timer.timeout.connect(self.recount)
        # 起始是空框 → 「清空」按钮先灰着
        self._sync_clear_btn()

        # 卡顿探针：正常每 100ms 响一次，某次明显晚到就说明主线程刚被卡住了。
        # 开销极低（一次时钟读取），只有超过阈值才写日志。
        self._wd_last = time.perf_counter()
        self._wd = QTimer(self)
        self._wd.setInterval(self.WATCHDOG_MS)
        self._wd.timeout.connect(self._watchdog)
        self._wd.start()

    def _watchdog(self):
        now = time.perf_counter()
        gap = (now - self._wd_last) * 1000.0
        self._wd_last = now
        if not self.isVisible() or gap < self.STALL_MIN_MS:
            return
        doc = self.editor.document()
        _perf("界面阻塞", gap, "文档 %d 字 / %d 段 / 命中 %d / 高亮 %d / 查找条 %s"
              % (doc.characterCount(), doc.blockCount(), len(self.find_bar._hits),
                 len(self.editor.extraSelections()),
                 "开" if self.find_bar.isVisible() else "关"))

    # ---- 设置 ----
    def apply_settings(self):
        """按设置显示/隐藏统计项，重新排网格（每行最多 4 个）。"""
        enabled = store.wc_fields()
        self._paras_mode = store.wc_paras_mode()
        self._cards["paras"].setToolTip(
            "按回车分段；空行不额外算一段" if self._paras_mode != "blank"
            else "按空行分段；连续空行才分一段")
        cols = 4
        for c in self._cards.values():
            c.setVisible(False)
            self.grid.removeWidget(c)
        i = 0
        for key, _title, _hint in FIELDS:
            if key not in enabled:
                continue
            card = self._cards[key]
            card.setVisible(True)
            self.grid.addWidget(card, i // cols, i % cols)
            i += 1
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        self._dirty = True
        # 设置里改了「段落数怎么算」，当场就要按新口径重算一次；
        # 页面不在前台时只留脏标记，等切回来 showEvent 再算，避免白耗 CPU
        if self.isVisible():
            self.recount()

    # ---- 统计 ----
    def _current_interval(self):
        """长文本自动放宽节流间隔：5 万字以内 120ms，20 万以内 300ms，再长 600ms。"""
        n = self.editor.document().characterCount()      # O(1)，不用把全文取出来
        if n < 50000:
            return self.DEBOUNCE_MS
        if n < 200000:
            return 300
        return 600

    def _on_text_changed(self):
        self._sync_clear_btn()
        self.find_bar.refresh()          # 正文变了，命中位置要重算
        # 不在前台/被切走时只记「脏」，等切回来再算，避免白耗 CPU
        if not self.isVisible():
            self._dirty = True
            return
        self._timer.start(self._current_interval())

    # ---- 查找 ----
    def _open_find(self):
        self.find_bar.open()
        self.btn_find.setChecked(True)

    def _toggle_find(self):
        if self.find_bar.isVisible():
            self.find_bar.close_bar()
        else:
            self._open_find()

    def _sync_clear_btn(self):
        """没内容时「清空」按钮变灰 —— 空框上点它没意义。"""
        if hasattr(self, "btn_clear"):
            self.btn_clear.setEnabled(not self.editor.document().isEmpty())

    def showEvent(self, e):
        super().showEvent(e)
        if self._dirty:
            self.recount()

    def recount(self):
        t0 = time.perf_counter()
        stats = count_text(self.editor.toPlainText(), self._paras_mode)
        ms = (time.perf_counter() - t0) * 1000.0
        _perf("统计", ms, "文档 %d 字 / %d 段"
              % (self.editor.document().characterCount(),
                 self.editor.document().blockCount()))
        for key, card in self._cards.items():
            if key in stats:
                card.set_value(stats[key])
        self.lbl_elapsed.setText("耗时 %.1f ms" % ms if ms >= 0.05 else "已完成")
        self._dirty = False

    # ---- 供外部用 ----
    def set_text(self, text):
        """别的工具（JSON 转 MD / 翻译）把文字送过来：替换内容并立刻统计。"""
        self.editor.setPlainText(text or "")
        self.recount()
        self.find_bar.refresh()

    def clear_text(self):
        self.editor.clear()
        self._sync_clear_btn()
        self.recount()
        self.find_bar.refresh()      # 清空后查找结果应显示「无结果」


class WordCountSettings(QWidget):
    """字数统计的设置面板：勾选要显示哪些统计项。"""

    def __init__(self, on_change=None, parent=None):
        super().__init__(parent)
        self._on_change = on_change
        self._boxes = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.px(4), theme.px(4), theme.px(4), theme.px(4))
        lay.setSpacing(theme.px(10))

        t = QLabel("要显示哪些统计项")
        t.setStyleSheet(
            f"color:{theme.TEXT}; font-size:{theme.px(13)}px; font-weight:700;"
            "background:transparent;"
        )
        lay.addWidget(t)
        hint = QLabel("「字数」是必选项，其余可自由勾选；改动会立即生效。")
        hint.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(11)}px; background:transparent;"
        )
        lay.addWidget(hint)

        enabled = store.wc_fields()
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.px(18))
        grid.setVerticalSpacing(theme.px(10))
        for i, (key, title, desc) in enumerate(FIELDS):
            cb = QCheckBox("%s　（%s）" % (title, desc))
            cb.setChecked(key in enabled)
            cb.setCursor(Qt.PointingHandCursor)
            if key == ALWAYS_ON:
                cb.setChecked(True)
                cb.setEnabled(False)          # 必选项：勾上并变灰
            cb.stateChanged.connect(self._commit)
            self._boxes[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        lay.addLayout(grid)

        # 段落数的算法二选一
        lay.addSpacing(theme.px(6))
        mode_t = QLabel("段落数怎么算")
        mode_t.setStyleSheet(
            f"color:{theme.TEXT}; font-size:{theme.px(12.5)}px; font-weight:700;"
            "background:transparent;"
        )
        lay.addWidget(mode_t)
        row = QHBoxLayout()
        row.setSpacing(theme.px(8))
        from PySide6.QtWidgets import QButtonGroup, QPushButton
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        mode = store.wc_paras_mode()
        for key, label in (("enter", "按回车分段"), ("blank", "按空行分段")):
            b = QPushButton(label)
            b.setObjectName("ToolTab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(theme.px(30))
            b.setChecked(mode == key)
            b.clicked.connect(lambda _=False, k=key: self._set_paras_mode(k))
            grp.addButton(b)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        mode_hint = QLabel("回车分段：一行一段，空行不额外算　·　空行分段：连续空行才分出一段")
        mode_hint.setWordWrap(True)
        mode_hint.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;"
        )
        lay.addWidget(mode_hint)
        lay.addStretch(1)

    def _set_paras_mode(self, mode):
        store.save_wc_paras_mode(mode)
        if self._on_change:
            self._on_change()

    def _commit(self):
        keys = [k for k, cb in self._boxes.items() if cb.isChecked() or k == ALWAYS_ON]
        if ALWAYS_ON not in keys:
            keys.insert(0, ALWAYS_ON)
        store.save_wc_fields(keys)
        if self._on_change:
            self._on_change()
