# -*- coding: utf-8 -*-
"""widgets.py —— 自绘控件（圆角卡片、开关、Toast、猫娘背景、SVG 图标、飘字）"""
import os
import random

from PySide6.QtCore import (QEasingCurve, QByteArray, QPoint, QPointF, QPropertyAnimation,
                            QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect,
                               QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

import store
import theme

ASSETS = store.resource("assets")


def svg_pixmap(name, size=18, tint=None, dpr=2.0, recolor=None):
    """把 assets 里的 SVG 渲染成指定尺寸的 QPixmap（高 DPI 清晰）。

    tint    —— 把整个图形涂成单一颜色（用于单色图标）。
    recolor —— {原色: 新色} 只替换指定颜色，保留图标原有的多色设计。
    """
    path = name if os.path.isabs(name) else os.path.join(ASSETS, name)
    px = max(8, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    if not os.path.exists(path):
        return pm
    try:
        if recolor:
            with open(path, "r", encoding="utf-8") as f:
                txt = f.read()
            for old, new in recolor.items():
                txt = txt.replace(old, new)
            r = QSvgRenderer(QByteArray(txt.encode("utf-8")))
        else:
            r = QSvgRenderer(path)
    except Exception:
        return pm
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    r.render(p, QRectF(0, 0, px, px))
    if tint is not None:
        # 用 SourceIn 只保留图形本身，颜色统一成 tint
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(pm.rect(), QColor(tint))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def svg_icon(name, size=18, tint=None, hover_tint=None, disabled_tint=None, recolor=None,
             hover_recolor=None):
    """构造带 hover / disabled 状态的 QIcon，避免禁用时图标还是亮蓝色。"""
    from PySide6.QtGui import QIcon
    ic = QIcon()
    ic.addPixmap(svg_pixmap(name, size, tint, recolor=recolor), QIcon.Normal, QIcon.Off)
    if hover_tint or hover_recolor:
        ic.addPixmap(svg_pixmap(name, size, hover_tint, recolor=hover_recolor or recolor),
                     QIcon.Active, QIcon.Off)
    if disabled_tint:
        ic.addPixmap(svg_pixmap(name, size, disabled_tint, recolor=recolor),
                     QIcon.Disabled, QIcon.Off)
    return ic


def shadow(widget, blur=28, color=QColor(61, 111, 150, 46), dy=6):
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setColor(color)
    eff.setOffset(0, dy)
    widget.setGraphicsEffect(eff)
    return eff


class Card(QFrame):
    """圆角白卡片。"""

    def __init__(self, parent=None, soft=False, accent=False, pad=18, shadow_on=True):
        super().__init__(parent)
        self.setObjectName("CardAccent" if accent else ("CardSoft" if soft else "Card"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(pad, pad, pad, pad)
        lay.setSpacing(theme.px(10))
        if shadow_on:
            shadow(self, 30 if not soft else 18)

    def body(self):
        return self.layout()


class Divider(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)


class StatPill(QWidget):
    """数字 + 说明的统计块。"""

    def __init__(self, value="0", label="", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        self.v = QLabel(value)
        self.v.setObjectName("StatValue")
        self.l = QLabel(label)
        self.l.setObjectName("StatLabel")
        lay.addWidget(self.v)
        lay.addWidget(self.l)

    def set(self, value, label=None):
        self.v.setText(str(value))
        if label is not None:
            self.l.setText(label)


class TagLabel(QLabel):
    """小圆角标签（自绘背景）。"""

    def __init__(self, text="", kind="info", parent=None):
        super().__init__(text, parent)
        self.kind = kind
        self._apply()
        f = self.font()
        f.setPointSizeF(theme.pt(8.6))
        self.setFont(f)
        self.setAlignment(Qt.AlignCenter)

    def set_kind(self, kind):
        self.kind = kind
        self._apply()

    def _apply(self):
        colors = {
            "info": (theme.PRIMARY_SOFT, theme.PRIMARY_PRESS, "#cbdef0"),
            "accent": (theme.ACCENT_SOFT, "#c4697f", "#f6ccd7"),
            "success": (theme.SUCCESS_SOFT, "#2f8b64", "#bfe4d6"),
            "warn": (theme.WARN_SOFT, "#a8721f", "#f0dcb8"),
            "danger": (theme.DANGER_SOFT, "#bb5b66", "#f2ccd1"),
            "muted": ("#f1f6fa", theme.TEXT_MUTED, "#e0eaf2"),
        }
        bg, fg, bd = colors.get(self.kind, colors["info"])
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {bd};"
            f"border-radius:8px; padding:2px 9px;"
        )


class Switch(QWidget):
    """小拨杆开关（自绘：圆角轨道 + 白点，开时轨道变主色）。"""

    toggled = Signal(bool)

    W, H, KNOB = 40, 22, 18      # 设计稿尺寸

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self._checked = bool(checked)
        self._t = 1.0 if checked else 0.0
        self.setFixedSize(theme.px(self.W), theme.px(self.H))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    # ---- 状态 ----
    def isChecked(self):
        return self._checked

    def setChecked(self, v, animate=False, notify=False):
        v = bool(v)
        if v == self._checked and not animate:
            self._t = 1.0 if v else 0.0
            self.update()
            return
        self._checked = v
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._t)
            self._anim.setEndValue(1.0 if v else 0.0)
            self._anim.start()
        else:
            self._t = 1.0 if v else 0.0
            self.update()
        if notify:
            self.toggled.emit(v)

    def toggle(self):
        self.setChecked(not self._checked, animate=True, notify=True)

    def _on_anim(self, v):
        self._t = float(v)
        self.update()

    # ---- 交互 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.isEnabled():
            self.toggle()
            e.accept()
            return
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.toggle()
            e.accept()
            return
        super().keyPressEvent(e)

    # ---- 绘制 ----
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.scale(theme.S, theme.S)

        w, h = float(self.W), float(self.H)
        knob = float(self.KNOB)
        pad = (h - knob) / 2.0
        t = self._t

        # 轨道：关=浅灰蓝，开=主色（两段颜色按 t 插值）
        off = QColor("#cbdcea")
        on = QColor(theme.PRIMARY)
        track = QColor(
            int(off.red() + (on.red() - off.red()) * t),
            int(off.green() + (on.green() - off.green()) * t),
            int(off.blue() + (on.blue() - off.blue()) * t),
        )
        if not self.isEnabled():
            track.setAlpha(110)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2.0, h / 2.0)

        # 白点
        x = pad + (w - knob - 2 * pad) * t
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QRectF(x, pad, knob, knob))


class CatgirlBackdrop(QWidget):
    """左下角的猫娘背景：贴底、左侧裁切、右侧柔和渐隐，不拦截鼠标。"""

    MAX_H_RATIO = 0.52     # 最高不超过窗口高度的一半，避免喧宾夺主

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._pix = QPixmap(os.path.join(ASSETS, "catgirl.png"))   # 1038×1000，解出来约 4MB
        self._op = QGraphicsOpacityEffect(self)
        self._op.setOpacity(0.0)
        self.setGraphicsEffect(self._op)
        self._anim = QPropertyAnimation(self._op, b"opacity", self)
        self._anim.setDuration(1000)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(0.78)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def fade_in(self):
        self._anim.start()

    def release_pixmaps(self):
        """窗口最小化/长时间闲置时把大图还给系统（回到前台再 reload）。"""
        self._pix = QPixmap()
        self._canvas = None
        self._draw_rect = None
        self.update()

    def reload(self):
        if self._pix.isNull():
            self._pix = QPixmap(os.path.join(ASSETS, "catgirl.png"))
            self.update()

    def show_now(self):
        """直接显示（重建界面后不要再淡入一次）。"""
        self._anim.stop()
        self._op.setOpacity(0.78)
        self.show()
        self.lower()

    def paintEvent(self, e):
        if self._pix.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)

        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        # 宽度铺满左栏；高度超限时按高度缩，保证只占左下角一块
        scaled = self._pix.scaledToWidth(int(w * 1.02), Qt.SmoothTransformation)
        cap = h * self.MAX_H_RATIO
        if scaled.height() > cap:
            scaled = scaled.scaledToHeight(int(cap), Qt.SmoothTransformation)

        x = int(-scaled.width() * 0.03)
        y = h - scaled.height() + int(scaled.height() * 0.02)

        # 用 alpha 蒙版实现「右上淡出」，避免硬边
        canvas = QPixmap(scaled.size())
        canvas.fill(Qt.transparent)
        cp = QPainter(canvas)
        cp.setRenderHint(QPainter.SmoothPixmapTransform, True)
        cp.drawPixmap(0, 0, scaled)
        cp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        grad = QLinearGradient(0, 0, 0, canvas.height())
        grad.setColorAt(0.00, QColor(0, 0, 0, 90))     # 顶部淡入
        grad.setColorAt(0.26, QColor(0, 0, 0, 214))
        grad.setColorAt(1.00, QColor(0, 0, 0, 240))
        cp.fillRect(canvas.rect(), grad)
        grad2 = QLinearGradient(0, 0, canvas.width(), 0)
        grad2.setColorAt(0.00, QColor(0, 0, 0, 255))
        grad2.setColorAt(0.74, QColor(0, 0, 0, 215))
        grad2.setColorAt(1.00, QColor(0, 0, 0, 0))     # 右侧淡出
        cp.fillRect(canvas.rect(), grad2)
        cp.end()

        # 记下这次的绘制范围与蒙版，供点击命中测试用
        self._canvas = canvas
        self._draw_rect = QRect(x, y, canvas.width(), canvas.height())
        p.drawPixmap(x, y, canvas)

    def hit_test(self, pt):
        """pt 是本控件坐标；只有落到人物本体的不透明像素上才算命中。"""
        rect = getattr(self, "_draw_rect", None)
        canvas = getattr(self, "_canvas", None)
        if rect is None or canvas is None or canvas.isNull():
            return False
        if not rect.contains(pt):
            return False
        img = canvas.toImage()
        px = int(pt.x()) - rect.x()
        py = int(pt.y()) - rect.y()
        if not (0 <= px < img.width() and 0 <= py < img.height()):
            return False
        return img.pixelColor(px, py).alpha() > 24


class Toast(QWidget):
    """底部居中浮出的小提示条。"""

    def __init__(self, parent, text="", kind="info", ms=2600):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # QWidget 默认不绘制样式表背景，必须打开这个属性
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._kind = kind
        self._lab = QLabel(text, self)
        self._lab.setWordWrap(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.px(18), theme.px(10), theme.px(18), theme.px(10))
        lay.addWidget(self._lab)
        self._op = QGraphicsOpacityEffect(self)
        self._op.setOpacity(0.0)
        self.setGraphicsEffect(self._op)
        self._style_for(kind)
        self._fade = QPropertyAnimation(self._op, b"opacity", self)
        self._fade.setDuration(200)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._hide_anim)
        self._ms = ms
        self.adjustSize()

    def _style_for(self, kind):
        bg, fg = {
            "info": ("#eaf6ff", "#2f6b93"),
            "success": ("#e4f7ee", "#2b7d5c"),
            "warn": ("#fdf4e5", "#a0701f"),
            "error": ("#fdebee", "#b8545f"),
        }.get(kind, ("#eaf6ff", "#2f6b93"))
        self.setStyleSheet(
            f"background:{bg}; border:1px solid rgba(120,170,205,0.22);"
            f"border-radius:{theme.px(14)}px;"
        )
        self._lab.setStyleSheet(
            f"color:{fg}; font-size:{theme.px(12.5)}px; background:transparent;"
            f"border:none; padding:0px;"
        )

    def pop(self, text=None, kind=None, ms=None):
        if text is not None:
            self._lab.setText(text)
        if kind is not None:
            self._kind = kind
            self._style_for(kind)
        self._fit()
        self._reposition()
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(self._op.opacity())
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._timer.start(ms or self._ms)

    def _fit(self):
        """短提示单行铺开；很长的才折行，避免出现「10\\n个标签页」这种断行。"""
        par = self.parentWidget()
        limit = (par.width() - theme.px(160)) if par else theme.px(520)
        limit = max(theme.px(240), min(theme.px(560), limit))
        pad = theme.px(18) * 2
        self._lab.setWordWrap(False)
        need = self._lab.sizeHint().width() + pad
        if need > limit:
            self._lab.setWordWrap(True)
            self._lab.setFixedWidth(limit - pad)
        else:
            self._lab.setFixedWidth(need - pad)
        self.setFixedWidth(self._lab.width() + pad)
        self.adjustSize()

    def _hide_anim(self):
        self._fade.stop()
        self._fade.setStartValue(self._op.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        QTimer.singleShot(220, self.hide)

    def _reposition(self):
        par = self.parentWidget()
        if par:
            self._fit()
            # 标题栏正中（左右两侧分别是标题和窗口按钮，中间是空白）
            x = (par.width() - self.width()) // 2
            y = max(theme.px(10), (theme.px(64) - self.height()) // 2)
            self.move(max(theme.px(10), x), y)
            self.raise_()


class ConfirmDialog(QDialog):
    """自绘确认弹窗，替代系统 QMessageBox.question。"""

    def __init__(self, parent, title, message, ok_text="确定", danger=False, detail=""):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedWidth(theme.px(410))

        mark = theme.DANGER if danger else theme.PRIMARY
        tint = theme.DANGER_SOFT if danger else theme.PRIMARY_SOFT
        glyph = "!" if danger else "?"

        card = QFrame(self)
        card.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(card)

        body = QVBoxLayout(card)
        body.setContentsMargins(theme.px(24), theme.px(22), theme.px(24), theme.px(18))
        body.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(theme.px(12))
        badge = QLabel(glyph)
        badge.setFixedSize(theme.px(36), theme.px(36))
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            f"background:{tint}; color:{mark}; border-radius:{theme.px(18)}px;"
            f"font-size:{theme.px(17)}px; font-weight:700;"
        )
        head.addWidget(badge, 0, Qt.AlignTop)

        tcol = QVBoxLayout()
        tcol.setSpacing(5)
        h = QLabel(title)
        h.setStyleSheet(f"color:{theme.TEXT}; font-size:{theme.px(15)}px; font-weight:600;")
        h.setWordWrap(True)
        tcol.addWidget(h)
        m = QLabel(message)
        m.setWordWrap(True)
        m.setStyleSheet(f"color:{theme.TEXT_MUTED}; font-size:{theme.px(12)}px;")
        tcol.addWidget(m)
        if detail:
            d = QLabel(detail)
            d.setWordWrap(True)
            d.setStyleSheet(f"color:{theme.TEXT_FAINT}; font-size:{theme.px(11)}px;")
            tcol.addWidget(d)
        head.addLayout(tcol, 1)
        body.addLayout(head)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setMinimumHeight(theme.px(36))
        cancel.setMinimumWidth(theme.px(88))
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        ok = SoftButton(ok_text, kind="danger" if danger else "primary")
        ok.setMinimumHeight(theme.px(36))
        ok.setMinimumWidth(theme.px(104))
        ok.clicked.connect(self.accept)
        ok.setDefault(True)
        row.addWidget(ok)
        body.addLayout(row)

        shadow(card, 40, QColor(30, 70, 105, 80), 6)
        self.setWindowTitle(title)
        self.adjustSize()

    @staticmethod
    def ask(parent, title, message, ok_text="确定", danger=False, detail=""):
        d = ConfirmDialog(parent, title, message, ok_text, danger, detail)
        return d.exec() == QDialog.Accepted

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)


class DoneDialog(QDialog):
    """与主界面同一套配色风格的「恢复完毕」弹窗（替代系统 QMessageBox）。

    系统 QMessageBox 在深色系统主题下会变成黑底，与浅蓝界面割裂，
    且把换行渲染成 /n，所以这里自绘一个。
    """

    def __init__(self, parent, title, headline, lines, kind="success"):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedWidth(theme.px(430))

        tint = {"success": "#e4f7ee", "warn": "#fdf4e5", "info": "#eaf6ff"}.get(kind, "#eaf6ff")
        mark = {"success": "#4fb78a", "warn": "#e8a33d", "info": "#4a9fd8"}.get(kind, "#4a9fd8")
        glyph = {"success": "✓", "warn": "!", "info": "i"}.get(kind, "i")

        card = QFrame(self)
        card.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(card)

        body = QVBoxLayout(card)
        body.setContentsMargins(theme.px(24), theme.px(22), theme.px(24), theme.px(18))
        body.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(12)
        badge = QLabel(glyph)
        badge.setFixedSize(theme.px(38), theme.px(38))
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            f"background:{tint}; color:{mark}; border-radius:{theme.px(19)}px;"
            f"font-size:{theme.px(19)}px; font-weight:700;"
        )
        head.addWidget(badge, 0, Qt.AlignTop)

        tcol = QVBoxLayout()
        tcol.setSpacing(4)
        h = QLabel(headline)
        h.setStyleSheet(f"color:{theme.TEXT}; font-size:{theme.px(16)}px; font-weight:600;")
        tcol.addWidget(h)
        for i, ln in enumerate(lines):
            l = QLabel(ln)
            l.setWordWrap(True)
            l.setStyleSheet(
                "color:%s; font-size:%s; line-height:150%%;"
                % (theme.TEXT_MUTED,
                   "%dpx" % theme.px(12) if i == 0 else "%dpx" % theme.px(11.5))
            )
            tcol.addWidget(l)
        head.addLayout(tcol, 1)
        body.addLayout(head)

        body.addSpacing(theme.px(16))
        row = QHBoxLayout()
        row.addStretch(1)
        ok = SoftButton("知道了", kind="primary")
        ok.setMinimumWidth(theme.px(112))
        ok.setMinimumHeight(theme.px(38))
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        body.addLayout(row)

        shadow(card, 40, QColor(30, 70, 105, 80), 6)
        self.setWindowTitle(title)
        self.adjustSize()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            return
        super().keyPressEvent(e)


class SoftButton(QPushButton):
    """带轻微阴影的主按钮包装。"""

    def __init__(self, text="", parent=None, kind="primary"):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName({"primary": "Primary", "ghost": "Ghost", "danger": "Danger"}.get(kind, ""))
        if kind == "primary":
            shadow(self, 22, QColor(74, 159, 216, 90), 3)


# ============================================================ 喵语彩蛋

MEOW_FILE = "喵语.txt"

DEFAULT_MEOWS = [
    "喵~", "喵喵~", "喵呜~", "喵？", "喵！", "喵呜喵呜~",
    "喵♪", "喵帕斯~", "喵喵喵？", "喵——", "喵酱", "喵嗷~",
    "Ciallo～(∠・ω< )⌒★",
]


def ensure_meow_file():
    """首次运行时在程序目录生成一份可自定义的喵语清单（每行一条）。"""
    path = os.path.join(store.ROOT, MEOW_FILE)
    if os.path.exists(path):
        return path
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 点左下角那只猫娘，会从这里随机挑一句飘出来。\n")
            f.write("# 每行写一句，以 # 开头的行会被忽略。改完直接保存，不用重启，下次点击就生效。\n")
            for s in DEFAULT_MEOWS:
                f.write(s + "\n")
    except Exception:
        pass
    return path


def load_meows():
    """读喵语清单；读不到或全空就用内置的默认值。"""
    path = ensure_meow_file()
    out = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)
    except Exception:
        pass
    return out or list(DEFAULT_MEOWS)


_MEOW_CACHE = {"mtime": None, "list": None}


def meows_cached():
    """带 mtime 缓存：用户改完 txt 不用重启，下次点击就生效。"""
    path = ensure_meow_file()
    try:
        mt = os.path.getmtime(path)
    except Exception:
        return list(DEFAULT_MEOWS)
    if _MEOW_CACHE["mtime"] != mt or _MEOW_CACHE["list"] is None:
        _MEOW_CACHE["list"] = load_meows()
        _MEOW_CACHE["mtime"] = mt
    return _MEOW_CACHE["list"]


class FloatingWord(QWidget):
    """从「喵～」右上角飘出的一小句文字：轻微弹一下登场，向右上漂走并淡出。"""

    MAX_W = 96       # 单句最宽（逻辑像素）：左栏只有 186 宽，超了就自动缩字号

    def __init__(self, parent, text, start, drift, color, px, angle, life=1600):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        f = QFont(theme.FONT_UI)
        f.setPixelSize(max(9, int(round(px * theme.S))))
        f.setBold(True)
        # 长句按比例缩字号，避免「Ciallo～(∠・ω< )⌒★」这种把按钮盖住
        max_w = self.MAX_W * theme.S
        while f.pixelSize() > 9 and QFontMetricsF(f).horizontalAdvance(text) > max_w:
            f.setPixelSize(f.pixelSize() - 1)
        self._f = f
        self._text = text
        self._color = QColor(color)
        self._angle = angle
        self._start = QPointF(start)
        self._drift = QPointF(drift)
        self._scale = 0.55
        self._alpha = 0.0
        self._t = 0.0

        fm = QFontMetricsF(f)
        self._tw = fm.horizontalAdvance(text)
        self._th = fm.height()
        self._asc = fm.ascent()
        self.setFixedSize(int(self._tw) + theme.px(30), int(self._th) + theme.px(26))
        self.move(int(start.x()), int(start.y()))

        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(life)
        self._anim.setEasingCurve(QEasingCurve.Linear)
        self._anim.valueChanged.connect(self._step)
        self._anim.finished.connect(self.deleteLater)
        self.show()
        self.raise_()
        self._anim.start()

    def _step(self, v):
        t = float(v)
        self._t = t

        # 位移：先快后慢地漂走（向上为主，带一点向右）
        e_out = 1 - pow(1 - t, 2.4)
        x = self._start.x() + self._drift.x() * e_out
        y = self._start.y() + self._drift.y() * e_out
        self.move(int(x), int(y))

        # 缩放：前 16% 弹一下（带轻微超调），之后保持
        p = min(1.0, t / 0.16)
        ease = p * p * (3 - 2 * p)
        pop = 1.0 + 0.12 * (1 - ease)
        self._scale = (0.62 + 0.38 * ease) * pop

        # 透明度：前 55% 全显，之后淡出
        self._alpha = 1.0 if t < 0.55 else max(0.0, (1 - t) / 0.45)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setOpacity(self._alpha)

        p.translate(self.width() / 2.0, self.height() / 2.0)
        p.rotate(self._angle)
        p.scale(self._scale, self._scale)

        path = QPainterPath()
        path.addText(-self._tw / 2.0, -self._th / 2.0 + self._asc, self._f, self._text)

        # 白色描边 + 彩色填充：压在任何背景上都清楚
        p.setPen(QPen(QColor(255, 255, 255, 225), 3.4))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawPath(path)


class ClickableRail(QWidget):
    """左侧导航栏容器：空白处（也就是猫娘身上）可以点，点一下飘出喵语。

    导航按钮是它的子控件，会自己吃掉点击，所以不会误触。
    故意不设 tooltip、也不改光标 —— 不想在人物身上挂任何说明气泡。
    """

    clicked = Signal(QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Rail")
        self._hot_top = theme.px(210)     # 这以下才是「人物区域」，上面是导航

    def set_hot_top(self, y):
        self._hot_top = y

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() >= self._hot_top:
            self.clicked.emit(e.position().toPoint())
            e.accept()
            return
        super().mousePressEvent(e)


MEOW_COLORS = ["#3f8fc4", "#e8849c", "#4aa88a", "#d99437", "#6b8fd4"]


def spawn_meow(host, anchor, count=None, bounds=None):
    """在 anchor 附近飘出 1-2 句喵语，向上漂走并淡出。

    host   —— 画在哪个控件上（主窗口的 centralWidget）
    anchor —— 起点，host 的控件坐标（一般就是点击位置）
    bounds —— 允许活动的矩形（host 坐标）；不给就放宽到整个窗口
    """
    if host is None:
        return
    phrases = meows_cached() or list(DEFAULT_MEOWS)
    if count is None:
        count = 1 if random.random() < 0.68 else 2
    pad = theme.px(6)
    if bounds is None:
        left, right = pad, host.width() - pad
    else:
        left, right = bounds.left(), bounds.right()

    for i in range(count):
        text = random.choice(phrases)
        w = FloatingWord(host, text, QPointF(anchor.x(), anchor.y()),
                         QPointF(0, 0),
                         random.choice(MEOW_COLORS),
                         random.choice((13, 14, 15)),
                         random.uniform(-9, 5),
                         random.randint(1500, 2000))

        # 起点：点击处略微偏右上，再夹进 bounds
        sx = anchor.x() + theme.px(6)
        sy = anchor.y() - theme.px(20) - i * theme.px(30)
        sx = min(max(left, sx), max(left, right - w.width()))
        sy = min(max(theme.px(4), sy), max(theme.px(4), host.height() - w.height() - theme.px(4)))

        # 漂移方向：朝空间更大的那一侧走，不会撞到边界
        dx = theme.px(random.randint(10, 28))
        if sx + w.width() + abs(dx) > right:
            dx = -abs(dx)
        if sx + dx < left:
            dx = abs(dx)
        dy = -theme.px(random.randint(16, 38))

        w._start = QPointF(sx, sy)
        w._drift = QPointF(dx, dy)
        w.move(int(sx), int(sy))
