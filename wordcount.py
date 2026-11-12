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
"""
import re

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


def count_text(t, paras_mode="enter"):
    """一次扫描算出所有统计项。

    paras_mode: "enter" 按回车分段（非空行数，空行不额外算）/"blank" 按空行分段。
    """
    if not t:
        return {k: 0 for k, _n, _d in FIELDS}
    cjk = len(_RE_CJK.findall(t))
    en_words = len(_RE_EN_WORD.findall(t))
    return {
        "words": cjk + en_words,
        "chars": len(t),
        "lines": len(t.splitlines()),
        "paras": (len([ln for ln in t.splitlines() if ln.strip()])
                  if paras_mode != "blank"
                  else len([b for b in _RE_PARAS.split(t) if b.strip()])),
        "cjk": cjk,
        "cjksym": len(_RE_CJK_SYM.findall(t)),
        "en_words": en_words,
        "en_chars": len(_RE_EN_CHAR.findall(t)),
    }


class FindBar(QWidget):
    """输入框上方的查找条：Ctrl+F 打开，Enter 下一个，Shift+Enter 上一个，Esc 关闭。

    和预览里的搜索条同一套手感，但作用对象是 QPlainTextEdit：
    QPlainTextEdit 没有富文本的 find 高亮，所以命中位置要自己用
    QTextDocument.find 走一遍算出来，再用 setExtraSelections 全部标黄，
    当前命中靠 setTextCursor 的选区高亮表示（避免再画一层）。
    """

    closed = Signal()
    MAX_HITS = 3000          # 命中太多就不数了，避免超大文档卡住

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self._editor = editor
        self._hits = []
        self._idx = -1
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
        capped = len(self._hits) >= self.MAX_HITS
        if empty:
            self.lbl.setText("")
        elif ok:
            self.lbl.setText("%d/%d%s" % (self._idx + 1, len(self._hits),
                                          "+" if capped else ""))
        else:
            self.lbl.setText("无结果")
        self.lbl.setToolTip("命中超过 %d 处，只统计前 %d 处" % (self.MAX_HITS, self.MAX_HITS)
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
        self.hide()
        self._editor.setExtraSelections([])
        self._editor.setFocus()
        self.closed.emit()

    def refresh(self):
        """正文变了：命中位置全变了，重新算一遍。"""
        if self.isVisible():
            self._on_text()

    # ---- 搜索 ----
    def _collect(self):
        text = self.edit.text()
        if not text:
            return []
        doc = self._editor.document()
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
            c = QTextCursor(self._editor.document())
            c.setPosition(s)
            c.setPosition(e, QTextCursor.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = c
            sel.format = fmt
            sels.append(sel)
        self._editor.setExtraSelections(sels)

    def _goto(self):
        s, e = self._hits[self._idx]
        c = QTextCursor(self._editor.document())
        c.setPosition(s)
        c.setPosition(e, QTextCursor.KeepAnchor)
        self._editor.setTextCursor(c)
        self._editor.centerCursor()      # 居中比 ensureCursorVisible 好找
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
        import time
        t0 = time.perf_counter()
        stats = count_text(self.editor.toPlainText(), self._paras_mode)
        ms = (time.perf_counter() - t0) * 1000.0
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
