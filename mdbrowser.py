# -*- coding: utf-8 -*-
"""mdbrowser.py —— MD 浏览器：把一段 Markdown 文本直接排版成预览。

和「JSON转MD」的区别：那个要先有 JSON、还要按规则挑正文键；这里什么都不要，
**直接把 Markdown 贴进来**就能看排版 —— 从公众号、Typora、ChatGPT 里复制出来的
Markdown 原文、本地 .md/.txt 文件，都可以。

复用 jsonmd 的渲染器（`build_html` / `PreviewWindow` / `FindBar`），所以三处预览的
样式、缩放、搜索手感完全一致，以后改样式也只改一个地方。
"""
import os
import time

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QSplitter, QTextBrowser,
                               QVBoxLayout, QWidget)

import theme
from jsonmd import FindBar, PreviewWindow, build_html, make_search_btn, normalize_headings
from widgets import shadow

# 纯前端渲染，不花 token，所以不像翻译那样卡死上限。
# 但 Python-Markdown 在几十万字符时会明显变慢，超过这个数就给个提醒（不拦）。
SLOW_HINT = 200000


class MdBrowserPage(QWidget):
    """左边贴 Markdown，右边实时看排版。"""

    DEBOUNCE_MS = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self._md = ""
        self._base_dir = ""
        self._big = None
        self._ok = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, theme.px(6), 0, 0)
        lay.setSpacing(theme.px(10))

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(theme.px(10))
        split.addWidget(self._build_input_card())
        split.addWidget(self._build_preview_card())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([theme.px(430), theme.px(560)])
        lay.addWidget(split, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DEBOUNCE_MS)
        self._timer.timeout.connect(self.render)

        self._sync_buttons()
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

    # ---- 左：Markdown 输入 ----
    def _build_input_card(self):
        card = QFrame()
        card.setObjectName("Card")
        shadow(card)
        ll = QVBoxLayout(card)
        ll.setContentsMargins(theme.px(14), theme.px(12), theme.px(14), theme.px(12))
        ll.setSpacing(theme.px(8))

        lh = QHBoxLayout()
        lh.addWidget(self._label("Markdown", 13, theme.TEXT, 700))
        lh.addStretch(1)
        self.lbl_count = self._label("", 10.5, theme.TEXT_FAINT, 400)
        lh.addWidget(self.lbl_count)
        ll.addLayout(lh)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("Code")
        self.editor.setPlaceholderText(
            "把 Markdown 贴到这里，右边立刻排版。\n\n"
            "# 一级标题\n\n正文，**加粗**，`行内代码`。\n\n"
            "- 列表项\n\n| 列 A | 列 B |\n|---|---|\n| 1 | 2 |")
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSizeF(theme.pt(10))
        self.editor.setFont(f)
        self.editor.setMinimumHeight(theme.px(200))
        self.editor.textChanged.connect(self._on_changed)
        ll.addWidget(self.editor, 1)

        row = QHBoxLayout()
        row.setSpacing(theme.px(8))
        row.addWidget(self._btn("打开文件", self.open_file, primary=True))
        row.addWidget(self._btn("从剪贴板", self.paste_clipboard))
        self.btn_clear = self._btn("清空", self.clear_all)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        ll.addLayout(row)
        return card

    # ---- 右：预览 ----
    def _build_preview_card(self):
        card = QFrame()
        card.setObjectName("Card")
        shadow(card)
        rl = QVBoxLayout(card)
        rl.setContentsMargins(theme.px(14), theme.px(12), theme.px(14), theme.px(12))
        rl.setSpacing(theme.px(8))

        rh = QHBoxLayout()
        rh.setSpacing(theme.px(8))
        rh.addWidget(self._label("预览", 13, theme.TEXT, 700))
        self.lbl_state = self._label("等待输入…", 10.5, theme.TEXT_FAINT, 400)
        rh.addWidget(self.lbl_state)
        rh.addStretch(1)

        self.preview = QTextBrowser()
        self.preview.setObjectName("Preview")
        self.preview.setOpenExternalLinks(True)

        self.find_bar = FindBar(self.preview)
        self.btn_find = make_search_btn()
        # 和 JSON转MD 一样：搜索条内联在标题栏，不挤动正文
        rh.insertWidget(2, self.find_bar)
        self.btn_find.clicked.connect(self._toggle_find)
        self.find_bar.closed.connect(self._on_find_closed)

        self.btn_copy = self._btn("复制", self.copy_md)
        self.btn_big = self._btn("大窗口预览", self.open_big, primary=True)
        rh.addWidget(self.btn_copy)
        rh.addWidget(self.btn_big)
        rl.addLayout(rh)

        rl.addWidget(self.preview, 1)

        sc = QShortcut(QKeySequence.Find, self)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self._open_find)

        foot = QHBoxLayout()
        foot.setSpacing(theme.px(8))
        self.btn_now = self._btn("更新预览", self.render)
        self.btn_now.setShortcut("Ctrl+Return")
        foot.addWidget(self.btn_now)
        self.lbl_updated = self._label("", 10.5, theme.TEXT_FAINT, 400)
        foot.addWidget(self.lbl_updated)
        foot.addStretch(1)
        rl.addLayout(foot)
        return card

    # ---- 输入 ----
    def _on_changed(self):
        txt = self.editor.toPlainText()
        n = len(txt)
        self.lbl_count.setText("{:,} 字符".format(n) if n else "")
        self.lbl_count.setStyleSheet(
            f"color:{theme.WARN if n > SLOW_HINT else theme.TEXT_FAINT};"
            f"font-size:{theme.px(10.5)}px; background:transparent;")
        if n > SLOW_HINT:
            self.lbl_count.setToolTip("超过 {:,} 字符，渲染会变慢".format(SLOW_HINT))
        else:
            self.lbl_count.setToolTip("")
        self._sync_buttons()
        self._timer.start()

    def _sync_buttons(self):
        has = bool(self.editor.toPlainText().strip())
        self.btn_clear.setEnabled(has)

    def paste_clipboard(self):
        txt = QApplication.clipboard().text()
        if not txt.strip():
            self._set_state("剪贴板里没有文字", error=True)
            return
        self.editor.setPlainText(txt)
        self.refresh_now()

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Markdown 文件", self._base_dir or "",
            "Markdown / 文本 (*.md *.markdown *.mdown *.txt);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                txt = f.read()
        except UnicodeDecodeError:
            try:
                with open(path, "r", encoding="gbk", errors="replace") as f:
                    txt = f.read()
            except Exception as e:
                self._set_state("读不了这个文件：%s" % e, error=True)
                return
        except Exception as e:
            self._set_state("读不了这个文件：%s" % e, error=True)
            return
        self._base_dir = os.path.dirname(path)
        self.preview.setSearchPaths([self._base_dir])
        self.editor.setPlainText(txt)
        self.refresh_now()

    def clear_all(self):
        self.editor.clear()
        self._md, self._ok = "", False
        self._render_empty()
        self._set_state("等待输入…", error=False)
        self.lbl_updated.setText("")
        self._sync_buttons()

    def copy_md(self):
        if not self._md:
            return
        QApplication.clipboard().setText(self._md)
        self._set_state("Markdown 已复制", error=False)

    # ---- 渲染 ----
    def refresh_now(self):
        self._timer.stop()
        self.render()

    def render(self):
        txt = self.editor.toPlainText()
        if not txt.strip():
            self._md, self._ok = "", False
            self._render_empty()
            self._set_state("等待输入…", error=False)
            self.lbl_updated.setText("")
            return
        try:
            # 只做标题规范化，其余一个字都不改 —— 这里没有「挑正文」的需求
            md = normalize_headings(txt).strip("\n")
            sb = self.preview.verticalScrollBar()
            ratio = sb.value() / float(max(1, sb.maximum()))
            self.preview.setHtml(build_html(md, 1.0))
            sb.setValue(int(round(ratio * sb.maximum())))
            self._md, self._ok = md, True
            self.lbl_updated.setText("更新于 " + time.strftime("%H:%M:%S"))
            self.find_bar.refresh()
            self._set_state("{:,} 字符".format(len(md)), error=False)
        except Exception as e:      # 渲染失败要看得见，不能静默不动
            self._md, self._ok = "", False
            self._render_empty("渲染出错：%s" % e, error=True)
            self._set_state("渲染出错：%s" % type(e).__name__, error=True)

    def _render_empty(self, text="", error=False):
        color = "#c06a72" if error else "#a8c0d2"
        self.preview.setHtml(
            '<div style="color:%s; font-size:%dpx; margin-top:%dpx;">%s</div>'
            % (color, theme.px(12), theme.px(30),
               (text or "预览会显示在这里").replace("<", "&lt;")))

    def _set_state(self, text, error=False):
        text = text if len(text) <= 60 else text[:58] + "…"
        self.lbl_state.setText(text)
        self.lbl_state.setStyleSheet(
            f"color:{'#c06a72' if error else theme.TEXT_MUTED};"
            f"font-size:{theme.px(10.5)}px; background:transparent;")

    # ---- 搜索（和 JSON转MD 同一套收纳逻辑）----
    def _open_find(self):
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

    # ---- 大窗口 ----
    def open_big(self):
        if not self._md:
            return
        if self._big is None:
            self._big = PreviewWindow(self.window(), self._md, base_dir=self._base_dir,
                                      title="MD 预览")
        else:
            self._big._md = self._md
            self._big._apply()
        self._big.show()
        self._big.raise_()
        self._big.activateWindow()

    def showEvent(self, e):
        super().showEvent(e)
        if self.editor.toPlainText().strip() and not self._ok:
            self._timer.start(0)
