# -*- coding: utf-8 -*-
"""theme.py —— 配色、字体与 QSS 样式表（淡蓝 / 圆角 / 无系统灰控件）

整体等比缩放：所有尺寸都以「设计稿 1120×780」为基准，用 set_scale() 定一个系数，
px() 与 qss() 里的所有 px 数值都会跟着一起缩放，于是布局的相对位置不变。
"""
import os
import re
import sys

# ---- 调色板 ----
BG_TOP = "#f2f9ff"
BG_BOTTOM = "#dceefb"
CARD = "#ffffff"
CARD_SOFT = "#f6fbff"
BORDER = "#c3def2"
BORDER_STRONG = "#9dc9e8"

TEXT = "#2c4a63"
TEXT_MUTED = "#7d9cb5"
TEXT_FAINT = "#a8c0d2"

PRIMARY = "#4a9fd8"
PRIMARY_HOVER = "#5cb0e6"
PRIMARY_PRESS = "#3c8cc2"
PRIMARY_SOFT = "#e3f2fd"

ACCENT = "#f5a9bf"      # 猫耳粉
ACCENT_SOFT = "#fde8ee"
SUCCESS = "#4fb78a"
SUCCESS_SOFT = "#e2f6ee"
WARN = "#e8a33d"
WARN_SOFT = "#fdf2e0"
DANGER = "#e07a85"
DANGER_SOFT = "#fce9eb"

FONT_UI = "Microsoft YaHei UI"
FONT_FALLBACK = "Microsoft YaHei"

# ---- 设计基准与全局缩放 ----
BASE_W = 1120
BASE_H = 780
S = 1.0                 # 当前缩放系数
BASE_PT = 9.0           # 基础字号（pt）


def set_scale(s):
    global S
    S = max(0.62, min(1.6, float(s)))
    return S


def px(v):
    """按当前缩放系数换算逻辑像素。"""
    return int(round(v * S))


def pt(v):
    """按当前缩放系数换算字号（pt）。"""
    return v * S


def _asset_url(name):
    """assets/ 下资源的绝对路径（打包后是 sys._MEIPASS），QSS 里的 url() 要用它。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", name).replace("\\", "/")


def qss():
    return _scale_px(f"""
/* ---------- 全局 ---------- */
* {{
    font-family: "{FONT_UI}", "{FONT_FALLBACK}", sans-serif;
    outline: none;
}}

QWidget#Root {{
    background: qlineargradient(x1:0, y1:0, x2:0.3, y2:1,
        stop:0 {BG_TOP}, stop:0.55 #e8f5ff, stop:1 {BG_BOTTOM});
}}

/* ---------- 标题栏 ---------- */
QWidget#TitleBar {{
    background: transparent;
}}
QLabel#AppTitle {{
    color: {TEXT};
    font-size: 17px;
    font-weight: 600;
    letter-spacing: 0.3px;
}}
QLabel#AppSubtitle {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QPushButton#WinBtn {{
    background: transparent;
    border: none;
    border-radius: 8px;
    color: {TEXT_MUTED};
    font-size: 15px;
    padding: 0px;
}}
QPushButton#WinBtn:hover {{
    background: rgba(74, 159, 216, 0.14);
    color: {TEXT};
}}
QPushButton#WinBtn:pressed {{
    background: rgba(74, 159, 216, 0.26);
}}
QPushButton#WinClose:hover {{
    background: #f0a0aa;
    color: #ffffff;
}}
QPushButton#WinClose:pressed {{
    background: #d9747f;
    color: #ffffff;
}}

/* ---------- 卡片 ---------- */
QFrame#Card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
QFrame#CardSoft {{
    background: {CARD_SOFT};
    border: 1px solid #d7e9f7;
    border-radius: 14px;
}}
QFrame#CardAccent {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #f2faff, stop:1 #e6f4ff);
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
QFrame#Divider {{
    background: #e3f0fa;
    border: none;
    max-height: 1px;
    min-height: 1px;
}}

/* ---------- 文字层级 ---------- */
QLabel#H1 {{
    color: {TEXT};
    font-size: 21px;
    font-weight: 600;
}}
QLabel#H2 {{
    color: {TEXT};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#Body {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}
QLabel#Hint {{
    color: {TEXT_FAINT};
    font-size: 11px;
}}
QLabel#Mono {{
    color: #5b7f9c;
    font-size: 11px;
    font-family: "Cascadia Mono", "Consolas", monospace;
}}
QLabel#StatValue {{
    color: {PRIMARY};
    font-size: 26px;
    font-weight: 600;
}}
QLabel#StatLabel {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}

/* ---------- 左侧导航栏 ---------- */
QWidget#Rail {{
    background: transparent;
}}
QStackedWidget#Stack {{
    background: transparent;
}}
QPushButton#Nav {{
    background: rgba(255, 255, 255, 0.46);
    color: {TEXT_MUTED};
    border: 1px solid rgba(213, 232, 246, 0.9);
    border-radius: 12px;
    padding: 0px 14px;
    margin: 0px 2px 0px 0px;
    font-size: 13px;
    font-weight: 500;
    text-align: left;
}}
QPushButton#Nav:hover {{
    background: rgba(255, 255, 255, 0.92);
    color: {PRIMARY};
    border-color: {BORDER_STRONG};
}}
QPushButton#Nav:checked {{
    background: {CARD};
    color: {PRIMARY_PRESS};
    border: 1px solid {PRIMARY};
    font-weight: 700;
}}
QPushButton#Nav:pressed {{
    background: #dcefff;
}}
QPushButton#Nav:disabled {{
    color: {TEXT_FAINT};
    border-color: #e6eff6;
    background: rgba(255, 255, 255, 0.3);
}}

/* ---------- 标签页 ---------- */
QTabWidget::pane {{
    border: none;
    background: transparent;
}}
QTabBar {{
    background: transparent;
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: rgba(255, 255, 255, 0.55);
    color: {TEXT_MUTED};
    border: 1px solid #d5e8f6;
    border-radius: 12px;
    padding: 8px 22px;
    margin-right: 8px;
    font-size: 13px;
    font-weight: 500;
    min-width: 86px;
}}
QTabBar::tab:hover {{
    background: rgba(255, 255, 255, 0.92);
    color: {PRIMARY};
    border-color: {BORDER_STRONG};
}}
QTabBar::tab:selected {{
    background: {CARD};
    color: {PRIMARY};
    border: 1px solid {PRIMARY};
    font-weight: 600;
}}

/* ---------- 主按钮 ---------- */
QPushButton {{
    background: {CARD};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 9px 18px;
    font-size: 13px;
}}
QPushButton:hover {{
    background: {PRIMARY_SOFT};
    border-color: {BORDER_STRONG};
    color: {PRIMARY_PRESS};
}}
QPushButton:pressed {{
    background: #d5e9f8;
}}
QPushButton:disabled {{
    background: #f4f8fb;
    color: {TEXT_FAINT};
    border-color: #e6eff6;
}}

QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {PRIMARY_HOVER}, stop:1 {PRIMARY});
    color: #ffffff;
    border: 1px solid {PRIMARY_PRESS};
    border-radius: 11px;
    padding: 11px 26px;
    font-size: 13.5px;
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #6dbdf0, stop:1 {PRIMARY_HOVER});
}}
QPushButton#Primary:pressed {{
    background: {PRIMARY_PRESS};
}}
QPushButton#Primary:disabled {{
    background: #cfe0ed;
    color: #ffffff;
    border-color: #c2d6e5;
}}

QPushButton#Ghost {{
    background: rgba(255, 255, 255, 0.7);
    color: {PRIMARY_PRESS};
    border: 1px solid {BORDER_STRONG};
    border-radius: 10px;
    padding: 9px 18px;
}}
QPushButton#Ghost:hover {{
    background: #ffffff;
    border-color: {PRIMARY};
}}
QPushButton#Ghost:disabled {{
    color: {TEXT_FAINT};
    border-color: #e2eef7;
    background: rgba(255, 255, 255, 0.5);
}}

QPushButton#Danger {{
    background: {DANGER_SOFT};
    color: #c2545f;
    border: 1px solid #f2c6cb;
    border-radius: 9px;
    padding: 7px 14px;
    font-size: 12px;
}}
QPushButton#Danger:hover {{
    background: #f9dde1;
    border-color: {DANGER};
}}

QPushButton#TinyIcon {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 4px 8px;
    color: {TEXT_MUTED};
    font-size: 12px;
}}
QPushButton#TinyIcon:hover {{
    background: {PRIMARY_SOFT};
    color: {PRIMARY};
}}

/* ---------- 下拉框（翻译：风格 / 领域 / 语种） ---------- */
QComboBox {{
    background: rgba(255, 255, 255, 0.9);
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 4px 8px 4px 10px;
    font-size: 12px;
    min-height: 20px;
}}
QComboBox:hover {{
    border-color: {PRIMARY};
    background: #ffffff;
}}
QComboBox:focus {{
    border-color: {PRIMARY};
}}
QComboBox:disabled {{
    color: #a9c3d6;
    background: rgba(255, 255, 255, 0.5);
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}}
QComboBox::down-arrow {{
    image: url("{_asset_url("下拉.svg")}");
    width: 10px;
    height: 10px;
}}
QComboBox QAbstractItemView {{
    background: #ffffff;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    font-size: 12px;
    selection-background-color: {PRIMARY_SOFT};
    selection-color: {PRIMARY_PRESS};
}}
QComboBox QAbstractItemView::item {{
    min-height: 24px;
    padding: 2px 8px;
    border-radius: 5px;
}}

/* ---------- 分段按钮（翻译档位） ---------- */
QPushButton#Seg {{
    background: rgba(255, 255, 255, 0.86);
    color: {PRIMARY_PRESS};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 0px 12px;
    font-size: 11.5px;
    font-weight: 600;
}}
QPushButton#Seg:hover {{
    background: #ffffff;
    border-color: {PRIMARY};
}}
QPushButton#Seg:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {PRIMARY}, stop:1 {PRIMARY_PRESS});
    color: #ffffff;
    border-color: {PRIMARY_PRESS};
}}

/* ---------- 输入框 ---------- */
QLineEdit {{
    background: {CARD_SOFT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 9px 13px;
    font-size: 13px;
    selection-background-color: #b8dcf5;
    selection-color: {TEXT};
}}
QLineEdit:focus {{
    border: 1px solid {PRIMARY};
    background: #ffffff;
}}
QLineEdit::placeholder {{
    color: {TEXT_FAINT};
}}
QLineEdit#PathEdit {{
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
}}

/* ---------- 列表 ---------- */
QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    background: {CARD_SOFT};
    border: 1px solid #dcecf8;
    border-radius: 12px;
    margin: 4px 2px;
    padding: 0px;
    /* 必须显式给 color：系统深色主题下 palette 的 Text 是白色，
       而这里的背景是浅色，不写就是白底白字（历史列表踩过）。 */
    color: {TEXT};
}}
QListWidget::item:hover {{
    background: #eef8ff;
    border-color: {BORDER_STRONG};
    color: {TEXT};
}}
QListWidget::item:selected {{
    background: #e2f2fe;
    border: 1px solid {PRIMARY};
    color: {TEXT};
}}

/* ---------- 滚动条 ---------- */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 9px;
    margin: 4px 2px 4px 0px;
}}
QScrollBar::handle:vertical {{
    background: #c3dcee;
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: #a6c9e2;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 9px;
    margin: 0px 4px 2px 4px;
}}
QScrollBar::handle:horizontal {{
    background: #c3dcee;
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

/* ---------- 提示气泡 ---------- */
QToolTip {{
    background: #ffffff;
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 12px;
}}


/* ---------- 复选框（设置窗口用） ---------- */
QCheckBox {{
    color: {TEXT};
    font-size: 12px;
    spacing: 8px;
    background: transparent;
}}
QCheckBox:disabled {{
    color: #9fb6c8;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    background: #ffffff;
}}
QCheckBox::indicator:hover {{
    border-color: {PRIMARY};
    background: #f6fbff;
}}
QCheckBox::indicator:checked {{
    background: {PRIMARY};
    border-color: {PRIMARY_PRESS};
    image: url("{_asset_url("勾选.svg")}");
}}
QCheckBox::indicator:checked:hover {{
    background: {PRIMARY_HOVER};
}}
QCheckBox::indicator:disabled {{
    background: #f2f7fc;
    border-color: #d9e7f3;
}}
QCheckBox::indicator:checked:disabled {{
    background: #b9d3e6;
    border-color: #a9c6dc;
    image: url("{_asset_url("勾选.svg")}");
}}

/* ---------- 工具内标签页（顶部胶囊） ---------- */
QPushButton#ToolTab {{
    background: rgba(255, 255, 255, 0.86);
    color: {PRIMARY_PRESS};
    border: 1px solid {BORDER_STRONG};
    border-radius: 11px;
    padding: 0px 18px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton#ToolTab:hover {{
    background: #ffffff;
    border-color: {PRIMARY};
}}
QPushButton#ToolTab:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {PRIMARY}, stop:1 {PRIMARY_PRESS});
    color: #ffffff;
    border: 1px solid {PRIMARY_PRESS};
    font-weight: 700;
}}
QPushButton#ToolTab:checked:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {PRIMARY_HOVER}, stop:1 {PRIMARY});
}}
QPushButton#ToolTab:disabled {{
    color: #a9c3d6;
    border-color: rgba(213, 232, 246, 0.9);
    background: rgba(255, 255, 255, 0.5);
}}

/* ---------- 鼠标提示（小而贴文字，不要默认大白框） ---------- */
QToolTip {{
    background: #2c4a63;
    color: #f2f9ff;
    border: none;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 11px;
}}

/* ---------- 紧凑按钮（工具页面里的小操作） ---------- */
QPushButton#Mini {{
    background: rgba(255, 255, 255, 0.86);
    color: {PRIMARY_PRESS};
    border: 1px solid {BORDER_STRONG};
    border-radius: 9px;
    padding: 0px 14px;
    font-size: 12px;
}}
QPushButton#Mini:hover {{
    background: #ffffff;
    border-color: {PRIMARY};
}}
QPushButton#Mini:pressed {{
    background: {PRIMARY_SOFT};
}}
QPushButton#Mini:disabled {{
    color: #a9c3d6;
    border-color: rgba(213, 232, 246, 0.9);
    background: rgba(255, 255, 255, 0.5);
}}
QPushButton#MiniPrimary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {PRIMARY_HOVER}, stop:1 {PRIMARY});
    color: #ffffff;
    border: 1px solid {PRIMARY_PRESS};
    border-radius: 9px;
    padding: 0px 16px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#MiniPrimary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #6dbdf0, stop:1 {PRIMARY_HOVER});
}}
QPushButton#MiniPrimary:pressed {{
    background: {PRIMARY_PRESS};
}}
QPushButton#MiniPrimary:disabled {{
    background: #cfe0ed;
    color: #ffffff;
    border-color: #c2d6e5;
}}

/* ---------- 投稿预览（JSON → Markdown） ---------- */
QPlainTextEdit#Code {{
    background: #fbfdff;
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 8px 10px;
    color: {TEXT};
    selection-background-color: #bfe0f7;
    selection-color: {TEXT};
}}
QPlainTextEdit#Code:focus {{
    border: 1px solid {PRIMARY};
}}
QTextBrowser#Preview {{
    background: #ffffff;
    /* 必须显式给 color：系统深色主题下 palette 的 Text 是白色，
       纯文本（setPlainText）就会白底白字看着像空的。 */
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 4px 8px;
    selection-background-color: #bfe0f7;
    selection-color: {TEXT};
}}

/* ---------- 预览里的搜索条 ---------- */
QLineEdit#FindEdit {{
    background: #ffffff;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 3px 8px;
    font-size: 12px;
    selection-background-color: #bfe0f7;
    selection-color: {TEXT};
}}
QLineEdit#FindEdit:focus {{
    border: 1px solid {PRIMARY};
}}

/* ---------- 字数统计 ---------- */
QPlainTextEdit#Editor {{
    background: #ffffff;
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 10px 12px;
    color: {TEXT};
    selection-background-color: #bfe0f7;
    selection-color: {TEXT};
}}
QPlainTextEdit#Editor:focus {{
    border: 1px solid {PRIMARY};
}}
QFrame#StatCard {{
    background: #f7fbff;
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
""")


_PX_RE = re.compile(r"(-?\d+(?:\.\d+)?)px")


def _scale_px(css):
    """把样式表里所有 Npx 按当前系数缩放（0px 保持 0）。"""
    if abs(S - 1.0) < 0.005:
        return css
    return _PX_RE.sub(lambda m: "%dpx" % max(0, int(round(float(m.group(1)) * S))), css)
