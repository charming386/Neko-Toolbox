# -*- coding: utf-8 -*-
"""htmlview.py —— HTML 显示器：把一段 HTML 源码直接渲染出来看效果。

和「MD浏览器」是一对：那个吃 Markdown，这个吃 HTML 源码。

关于渲染精度（这一点必须说清楚，界面上也写了一句提示）：
内嵌预览用的是 QTextBrowser（Qt 的富文本引擎），支持 HTML4 子集 + 一部分
CSS 2.1 —— 表格、图片、字体、颜色、margin/padding、边框都正常，
但**不支持 flex / grid / position / 媒体查询，也不执行 JavaScript**。
所以它适合回答「这段 HTML 结构对不对、文字有没有错、表格有没有歪」，
不适合验证真实网页的排版效果。

要看准确效果就点「用浏览器打开」：源码写成临时 .html 丢给系统默认浏览器，
那边是完整内核，所见即所得。这也顺便替代了「大窗口预览」——
系统浏览器的窗口比应用内任何窗口都大，而且渲染 100% 正确。
"""
import os
import re
import tempfile
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QSizePolicy, QSplitter,
                               QTextBrowser, QVBoxLayout, QWidget)

import theme
from jsonmd import FindBar, make_search_btn
from widgets import shadow

# 纯前端渲染不花 token，但超大文档会让 Qt 的解析变慢，超过就给个提醒（不拦）
SLOW_HINT = 300000

# 「用浏览器打开」的落地目录：系统临时目录，关机自动清
TMP_DIR = os.path.join(tempfile.gettempdir(), "猫娘工具箱-HTML预览")
KEEP_FILES = 6            # 只留最近几个，免得堆一坨

# 「这是 HTML 标签」的判定：< 后面跟字母/斜杠/感叹号
_RE_TAG = re.compile(r"<[a-zA-Z!/][^>]*>")


def _base_css():
    """包在用户 HTML 外面的基础样式：让没写样式的片段也有正常的阅读体验。

    故意写得克制 —— 只定字体、字号、行高、表格边框和图片上限，
    不碰颜色主题以外的东西，免得盖掉用户自己的样式。
    """
    base = theme.px(13)
    return (
        'body{margin:8px 10px 24px 6px; color:#24405a; background:transparent;'
        ' font-family:"%s"; font-size:%.1fpx; line-height:162%%;}'
        'img{max-width:96%%;}'
        'table{border-collapse:collapse; margin:8px 0;}'
        'td,th{border:1px solid #d7e8f5; padding:6px 10px;}'
        'th{background:#e8f4fc; color:#23445f;}'
        'a{color:#3c8cc2;}'
        'pre{background:#f6fbff; border:1px solid #e0eefa; padding:10px 12px;'
        ' white-space:pre-wrap; font-family:"Consolas","Courier New",monospace;}'
        'code{font-family:"Consolas","Courier New",monospace; background:#f1f7fc;'
        ' color:#2a5c80;}'
        'blockquote{margin:10px 0; padding:2px 12px; color:#5b7b95;'
        ' border-left:3px solid #c3def2; background:#f6fbff;}'
        'hr{border:none; border-top:1px solid #d7e8f5; margin:16px 0;}'
        % (theme.FONT_UI, base)
    )


def compose_html(src):
    """把用户输入的片段补成一份完整的、带 UTF-8 声明和基础样式的 HTML。

    五种输入都要吃得下，且每种都保证输出是规范的一份完整文档：
      1. 有 <head>            → 样式插进 head，其余不动（最理想）
      2. 只有 <html> 没 head   → 补一个 head
      3. 只有 <!DOCTYPE>       → 补 html/head/body
      4. 只有 <body> 片段      → 前面补 html/head
      5. 纯片段 / 纯文本        → 整段包进 body
    """
    src = src or ""
    bits = '<meta charset="utf-8"><style>%s</style>' % _base_css()
    low = src.lower()

    def _open_tag_end(tag):
        """返回 <tag ...> 右尖括号的下标，找不到返回 -1。"""
        i = low.find(tag)
        if i < 0:
            return -1
        return low.find(">", i)

    j = _open_tag_end("<head")
    if j >= 0:
        return src[:j + 1] + bits + src[j + 1:]

    j = _open_tag_end("<html")
    if j >= 0:
        return src[:j + 1] + "<head>" + bits + "</head>" + src[j + 1:]

    j = _open_tag_end("<!doctype")
    if j >= 0:
        return (src[:j + 1] + "<html><head>" + bits + "</head><body>"
                + src[j + 1:] + "</body></html>")

    i = low.find("<body")
    if i >= 0:
        tail = "" if "</html>" in low else "</html>"
        return "<html><head>" + bits + "</head>" + src[i:] + tail

    # 到这里已经没有 html/head/body 结构了。还要再分一次：
    #   * 有真正的标签（<p>、<h1>、<br/>、<!-- 之类）→ 当 HTML 片段
    #   * 一个标签都没有 → 当纯文本，转义后保留换行
    # 后者是为了「5 < 10」「a <符号> b」这种输入：尖括号不能被当成标签吞掉。
    if not _RE_TAG.search(src):
        src = "<p style='white-space:pre-wrap;'>%s</p>" % (
            src.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "<html><head>" + bits + "</head><body>" + src + "</body></html>"


def _cleanup_tmp(keep=KEEP_FILES):
    """只留最近几个预览文件，多了删掉（按修改时间倒序）。"""
    try:
        files = sorted(((os.path.getmtime(os.path.join(TMP_DIR, f)), f)
                        for f in os.listdir(TMP_DIR) if f.endswith(".html")),
                       reverse=True)
    except OSError:
        return
    for _t, f in files[keep:]:
        try:
            os.remove(os.path.join(TMP_DIR, f))
        except OSError:
            pass


class HtmlViewPage(QWidget):
    """左边贴 HTML，右边实时渲染。"""

    DEBOUNCE_MS = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self._html = ""
        self._base_dir = ""
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
        split.setSizes([theme.px(460), theme.px(530)])
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
        # 固定宽度策略：底部那行说明文字会抢空间，不锁住按钮就被压成两行
        b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        b.clicked.connect(slot)
        return b

    # ---- 左：源码 ----
    def _build_input_card(self):
        card = QFrame()
        card.setObjectName("Card")
        shadow(card)
        ll = QVBoxLayout(card)
        ll.setContentsMargins(theme.px(14), theme.px(12), theme.px(14), theme.px(12))
        ll.setSpacing(theme.px(8))

        lh = QHBoxLayout()
        lh.addWidget(self._label("HTML 源码", 13, theme.TEXT, 700))
        lh.addStretch(1)
        self.lbl_count = self._label("", 10.5, theme.TEXT_FAINT, 400)
        lh.addWidget(self.lbl_count)
        ll.addLayout(lh)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("Code")
        self.editor.setPlaceholderText(
            "把 HTML 贴到这里，右边立刻渲染。\n\n"
            "完整页面、只贴 <body> 里的片段、甚至纯文本都可以。\n\n"
            "<h1>标题</h1>\n<p>正文，<b>加粗</b>，<a href=\"#\">链接</a></p>\n"
            "<table><tr><th>列 A</th><th>列 B</th></tr><tr><td>1</td><td>2</td></tr></table>")
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
        rh.addWidget(self._label("渲染", 13, theme.TEXT, 700))
        self.lbl_state = self._label("等待输入…", 10.5, theme.TEXT_FAINT, 400)
        rh.addWidget(self.lbl_state)
        rh.addStretch(1)

        self.preview = QTextBrowser()
        self.preview.setObjectName("Preview")
        self.preview.setOpenExternalLinks(True)

        self.find_bar = FindBar(self.preview)
        self.btn_find = make_search_btn()
        rh.insertWidget(2, self.find_bar)
        self.btn_find.clicked.connect(self._toggle_find)
        self.find_bar.closed.connect(self._on_find_closed)

        self.btn_copy = self._btn("复制源码", self.copy_src)
        self.btn_open = self._btn("用浏览器打开", self.open_in_browser, primary=True)
        rh.addWidget(self.btn_copy)
        rh.addWidget(self.btn_open)
        rl.addLayout(rh)

        rl.addWidget(self.preview, 1)

        sc = QShortcut(QKeySequence.Find, self)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self._open_find)

        foot = QHBoxLayout()
        foot.setSpacing(theme.px(8))
        self.btn_now = self._btn("重新渲染", self.render)
        self.btn_now.setShortcut("Ctrl+Return")
        self.btn_now.setMinimumWidth(theme.px(96))     # 4 个汉字 + 内边距
        foot.addWidget(self.btn_now)
        self.lbl_note = self._label(
            "不含 JS / flex / grid，准确效果请点「用浏览器打开」",
            10.5, theme.TEXT_FAINT, 400)
        self.lbl_note.setWordWrap(True)
        # Ignored：中文没有空格，整句会被当成一个「词」算出很大的最小宽度，
        # 不放开这个策略就会把左边的按钮压到换行。
        self.lbl_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        foot.addWidget(self.lbl_note, 1)
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
        self.lbl_count.setToolTip(
            "超过 {:,} 字符，渲染会变慢".format(SLOW_HINT) if n > SLOW_HINT else "")
        self._sync_buttons()
        self._timer.start()

    def _sync_buttons(self):
        has = bool(self.editor.toPlainText().strip())
        self.btn_clear.setEnabled(has)
        self.btn_copy.setEnabled(has)
        self.btn_open.setEnabled(has)

    def paste_clipboard(self):
        txt = QApplication.clipboard().text()
        if not txt.strip():
            self._set_state("剪贴板里没有文字", error=True)
            return
        self.editor.setPlainText(txt)
        self.refresh_now()

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 HTML 文件", self._base_dir or "",
            "HTML 文件 (*.html *.htm *.xhtml);;网页与样式 (*.html *.htm *.css);;所有文件 (*)")
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
        # 相对路径的图片/样式按文件所在目录找
        self.preview.setSearchPaths([self._base_dir])
        self.editor.setPlainText(txt)
        self.refresh_now()

    def clear_all(self):
        self.editor.clear()
        self._html, self._ok = "", False
        self._render_empty()
        self._set_state("等待输入…", error=False)
        self._sync_buttons()

    def copy_src(self):
        txt = self.editor.toPlainText()
        if not txt:
            return
        QApplication.clipboard().setText(txt)
        self._set_state("源码已复制", error=False)

    # ---- 渲染 ----
    def refresh_now(self):
        self._timer.stop()
        self.render()

    def render(self):
        txt = self.editor.toPlainText()
        if not txt.strip():
            self._html, self._ok = "", False
            self._render_empty()
            self._set_state("等待输入…", error=False)
            return
        try:
            full = compose_html(txt)
            sb = self.preview.verticalScrollBar()
            ratio = sb.value() / float(max(1, sb.maximum()))
            self.preview.setHtml(full)
            sb.setValue(int(round(ratio * sb.maximum())))
            self._html, self._ok = full, True
            self.find_bar.refresh()
            self._set_state("{:,} 字符".format(len(txt)), error=False)
        except Exception as e:      # 渲染失败要看得见，不能静默不动
            self._html, self._ok = "", False
            self._render_empty("渲染出错：%s" % e, error=True)
            self._set_state("渲染出错：%s" % type(e).__name__, error=True)

    def _render_empty(self, text="", error=False):
        color = "#c06a72" if error else "#a8c0d2"
        self.preview.setHtml(
            '<div style="color:%s; font-size:%dpx; margin-top:%dpx;">%s</div>'
            % (color, theme.px(12), theme.px(30),
               (text or "渲染结果会显示在这里").replace("<", "&lt;")))

    def _set_state(self, text, error=False):
        text = text if len(text) <= 60 else text[:58] + "…"
        self.lbl_state.setText(text)
        self.lbl_state.setStyleSheet(
            f"color:{'#c06a72' if error else theme.TEXT_MUTED};"
            f"font-size:{theme.px(10.5)}px; background:transparent;")

    # ---- 搜索（和 MD 浏览器同一套收纳逻辑）----
    def _open_find(self):
        self.lbl_state.setVisible(False)
        self.btn_copy.setVisible(False)
        self.btn_open.setVisible(False)
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
        self.btn_open.setVisible(True)
        self.btn_find.setChecked(False)

    # ---- 用系统浏览器打开（准确渲染的唯一办法）----
    def open_in_browser(self):
        txt = self.editor.toPlainText()
        if not txt.strip():
            return
        try:
            os.makedirs(TMP_DIR, exist_ok=True)
            name = "预览-%s.html" % time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(TMP_DIR, name)
            # 写盘必须用 compose 后的完整文档：单独一段 HTML 没有 charset，中文会乱码
            with open(path, "w", encoding="utf-8") as f:
                f.write(compose_html(txt))
            _cleanup_tmp()
            ok = QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            self._set_state("已在浏览器中打开" if ok else "打不开浏览器", error=not ok)
        except Exception as e:
            self._set_state("打开失败：%s" % e, error=True)

    def showEvent(self, e):
        super().showEvent(e)
        if self.editor.toPlainText().strip() and not self._ok:
            self._timer.start(0)
