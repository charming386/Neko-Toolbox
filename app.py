# -*- coding: utf-8 -*-
"""app.py —— 文件夹恢复大师 · 猫娘版（主窗口）"""
import gc
import os
import sys

from PySide6.QtCore import (QEvent, QPoint, QRect, QRectF, QSize, Qt, QThread, QTimer,
                            Signal)
from PySide6.QtGui import (QColor, QCursor, QFont, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QPixmapCache)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QFileDialog,
                               QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QPushButton, QScrollArea,
                               QStackedWidget, QVBoxLayout, QWidget)

import core
import store
import theme
from widgets import (ASSETS, Card, CatgirlBackdrop, ClickableRail, ConfirmDialog, Divider,
                     DoneDialog, SoftButton, StatPill, Switch, TagLabel, Toast,
                     ensure_meow_file, shadow, spawn_meow, svg_icon)

APP_NAME = "猫娘工具箱"
APP_DIR = store.ROOT          # 可写数据目录（exe 旁边）
RES_DIR = store.resource()    # 只读资源目录（打包后在临时解压目录里）


# ============================================================ 恢复工作线程

class RestoreWorker(QThread):
    event = Signal(str, dict)
    finished_ok = Signal(dict)

    def __init__(self, windows, want_active_tab=True, want_focus=True, close_first=False):
        super().__init__()
        self.windows = windows
        self.want_active_tab = want_active_tab
        self.want_focus = want_focus
        self.close_first = close_first

    def run(self):
        # Win32 COM 必须在各自线程初始化，否则 Shell.Windows() 会拿不到窗口
        _com_init()
        try:
            core.wait_escape_release(1.2)
            if self.close_first:
                self.event.emit("info", {"message": "正在关闭当前文件夹…"})
                closed, left = core.close_all_explorer_windows()
                msg = "已关闭 %d 个窗口" % closed
                if left:
                    msg += "（%d 个没关掉，可能弹了确认框）" % left
                self.event.emit("info", {"message": msg + "，开始恢复…"})
            res = core.restore(
                self.windows,
                on_event=lambda kind, payload: self.event.emit(kind, payload),
                want_active_tab=self.want_active_tab,
                want_focus=self.want_focus,
            )
        finally:
            _com_uninit()
        self.finished_ok.emit(res)


class ScanWorker(QThread):
    done = Signal(object)
    phase = Signal(int, int)      # 兜底读取阶段：(已完成, 总数)

    def __init__(self, with_active=True):
        super().__init__()
        self.with_active = with_active

    def run(self):
        _com_init()
        try:
            res = core.list_windows(
                with_active=self.with_active,
                on_deep=lambda done, total: self.phase.emit(done, total),
            )
        finally:
            _com_uninit()
        self.done.emit(res)


def _com_init():
    try:
        import pythoncom
        pythoncom.CoInitialize()
        return True
    except Exception:
        return False


def _com_uninit():
    try:
        import pythoncom
        pythoncom.CoUninitialize()
    except Exception:
        pass


# ============================================================ 浮层提示（右下角）

class CornerBanner(QWidget):
    """屏幕右下角的警告条（不阻塞操作，但会置顶提示）。

    三行结构：标题 / 固定警告语 / 实时进度。
    警告语始终显示「不要移动鼠标」，进度另占一行，两者不会互相覆盖。
    """

    WARN_TEXT = "请勿移动鼠标，也不要进行其他操作"

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint |
                            Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._title = ""
        self._body = ""
        self._progress = 0.0
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._pulse_t = QTimer(self)
        self._pulse_t.timeout.connect(self._pulse_tick)
        self.setFixedSize(theme.px(402), theme.px(142))

    def _tick(self):
        sx = QApplication.primaryScreen().availableGeometry()
        self.move(sx.right() - self.width() - theme.px(18),
                  sx.bottom() - self.height() - theme.px(18))

    def _pulse_tick(self):
        self._pulse = (self._pulse + 0.06) % 2.0
        self.update()

    def show_banner(self, title, body, progress=0.0):
        self._title = title
        self._body = body
        self._progress = progress
        self._tick()
        self.show()
        self.raise_()
        self._timer.start(400)
        self._pulse_t.start(45)

    def set_progress(self, value, body=None):
        self._progress = max(0.0, min(1.0, value))
        if body is not None:
            self._body = body
        self.update()

    def hide_banner(self):
        self._timer.stop()
        self._pulse_t.stop()
        self.hide()

    def on_scale_changed(self):
        self.setFixedSize(theme.px(402), theme.px(142))
        self._tick()
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 在「设计稿坐标」里画，整体按缩放系数放大，比例恒定
        p.scale(theme.S, theme.S)
        W = self.width() / theme.S
        H = self.height() / theme.S
        r = QRectF(1, 1, W - 2, H - 2)

        # 阴影
        for i, alpha in enumerate((16, 12, 8)):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(40, 80, 115, alpha))
            p.drawRoundedRect(r.adjusted(-i, -i + 2, i, i + 2), 16 + i, 16 + i)

        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0, QColor(255, 255, 255, 250))
        grad.setColorAt(1, QColor(238, 248, 255, 250))
        p.setBrush(grad)
        p.setPen(QPen(QColor(160, 205, 236), 1))
        p.drawRoundedRect(r, 16, 16)

        # 左侧脉动圆点
        import math
        rr = 5 + 1.6 * math.sin(self._pulse * math.pi)
        cx, cy = 30, 40
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(74, 159, 216, 70))
        p.drawEllipse(QRectF(cx - rr - 4, cy - rr - 4, (rr + 4) * 2, (rr + 4) * 2))
        p.setBrush(QColor(74, 159, 216))
        p.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2, rr * 2))

        # 标题
        f = QFont(theme.FONT_UI, 10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRect(52, 20, int(W) - 76, 22), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        # 固定警告语（带浅粉底，强调「不要动鼠标」）
        fw = QFont(theme.FONT_UI, 8)
        fw.setBold(True)
        p.setFont(fw)
        wtxt = "⚠  " + self.WARN_TEXT
        tw = p.fontMetrics().horizontalAdvance(wtxt) + 16
        pill = QRectF(50, 47, min(tw, W - 72), 21)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(253, 232, 238))
        p.drawRoundedRect(pill, 7, 7)
        p.setPen(QPen(QColor(224, 122, 133, 120), 1))
        p.drawRoundedRect(pill, 7, 7)
        p.setPen(QColor(190, 82, 96))
        p.drawText(pill.adjusted(8, 0, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, wtxt)

        # 实时进度
        f2 = QFont(theme.FONT_UI, 8)
        p.setFont(f2)
        p.setPen(QColor(theme.TEXT_MUTED))
        p.drawText(QRect(52, 74, int(W) - 76, 20),
                   Qt.AlignLeft | Qt.AlignVCenter, self._body)

        # 进度条
        bar = QRectF(20, H - 26, W - 40, 7)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(213, 233, 247))
        p.drawRoundedRect(bar, 3.5, 3.5)
        if self._progress > 0:
            fill = QRectF(bar.left(), bar.top(), max(7.0, bar.width() * self._progress), bar.height())
            g2 = QLinearGradient(fill.topLeft(), fill.topRight())
            g2.setColorAt(0, QColor(theme.PRIMARY_HOVER))
            g2.setColorAt(1, QColor(theme.PRIMARY))
            p.setBrush(g2)
            p.drawRoundedRect(fill, 3.5, 3.5)


# ============================================================ 文件夹行（自绘）

class FolderRow(QWidget):
    def __init__(self, name, path, exists=True, parent=None):
        super().__init__(parent)
        self.setFixedHeight(theme.px(48))
        self._name = name
        self._path = path
        self._exists = exists
        self._hover = False
        self.setToolTip(path)

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.scale(theme.S, theme.S)
        W = self.width() / theme.S
        H = self.height() / theme.S
        r = QRectF(0.5, 0.5, W - 1, H - 1)

        if self._hover:
            p.setBrush(QColor(255, 255, 255, 235))
            p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
            p.drawRoundedRect(r, 9, 9)

        # 文件夹图标（放大到 26x21，视觉重量与两行文字更匹配）
        iw, ih = 26.0, 21.0
        ix = 11.0
        iy = (H - ih) / 2.0
        p.setPen(Qt.NoPen)
        c = QColor(theme.PRIMARY) if self._exists else QColor("#c3d4e0")

        # 后面那片（标签）
        back = QPainterPath()
        back.addRoundedRect(QRectF(ix, iy, iw * 0.52, ih * 0.72), 2.6, 2.6)
        p.setBrush(QColor(c.red(), c.green(), c.blue(), 235))
        p.drawPath(back)

        # 前面主体
        p.setBrush(c)
        p.drawRoundedRect(QRectF(ix, iy + ih * 0.24, iw, ih * 0.76), 3.4, 3.4)

        # 顶部一道浅色高光，让图标有点体积感
        if self._exists:
            p.setBrush(QColor(255, 255, 255, 58))
            p.drawRoundedRect(QRectF(ix + 2.2, iy + ih * 0.30, iw - 4.4, ih * 0.22), 2, 2)

        # 名称
        tx = int(ix + iw + 11)
        f = QFont(theme.FONT_UI, 9)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT if self._exists else theme.TEXT_FAINT))
        name_rect = QRect(tx, 6, int(W) - tx - 12, 19)
        fm = p.fontMetrics()
        p.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self._name, Qt.ElideRight, name_rect.width()))

        # 路径
        f.setPointSizeF(7.6)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT_FAINT if self._exists else "#c9d8e4"))
        path_rect = QRect(tx, 25, int(W) - tx - 12, 16)
        fm = p.fontMetrics()
        suffix = "" if self._exists else "   (目录不存在)"
        p.drawText(path_rect, Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self._path + suffix, Qt.ElideMiddle, path_rect.width()))


class WindowGroup(QWidget):
    """一个资源管理器窗口 = 一组标签页，可折叠查看。"""

    def __init__(self, idx, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        tabs = [t for t in win["tabs"] if t.get("is_folder", True)]
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(theme.px(2), theme.px(4), theme.px(4), theme.px(2))
        hl.setSpacing(theme.px(8))

        self.toggle = QPushButton("▾")
        self.toggle.setObjectName("TinyIcon")
        self.toggle.setFixedWidth(theme.px(22))
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.clicked.connect(self._toggle)
        hl.addWidget(self.toggle)

        title = QLabel("窗口 %d" % idx)
        title.setObjectName("H2")
        hl.addWidget(title)

        cnt = TagLabel("%d 个标签页" % len(tabs),
                       "info" if len(tabs) > 1 else "muted")
        hl.addWidget(cnt)

        # 活动标签提示
        act = win.get("active_tab", -1)
        if 0 <= act < len(tabs):
            a = TagLabel("当前显示：" + (tabs[act]["name"] or "?"), "accent")
            hl.addWidget(a)

        hl.addStretch(1)
        lay.addWidget(head)

        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(theme.px(28), 0, 0, theme.px(4))
        bl.setSpacing(0)
        for t in tabs:
            bl.addWidget(FolderRow(t.get("name") or t["path"], t["path"], t.get("exists", True)))
        lay.addWidget(self.body)
        self.body.setVisible(len(tabs) > 0)

    def _toggle(self):
        v = not self.body.isVisible()
        self.body.setVisible(v)
        self.toggle.setText("▾" if v else "▸")


# ============================================================ 无边框窗口的拖动 / 拉伸

class RootCanvas(QWidget):
    """根容器。

    除了承载界面，还负责「拖四周那圈窄空白带移动窗口」——
    等比放大后标题栏可能被顶到屏幕外，那时只能靠这圈空白把窗口拖回来。
    """

    BAND = 22                    # 边带宽度（设计稿单位）

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self._win = win
        self._grab = None
        # QWidget 的子类默认不画样式表背景，不打开这个属性窗口就会「变成透明」
        self.setAttribute(Qt.WA_StyledBackground, True)

    def _in_band(self, pos):
        b = theme.px(self.BAND)
        return (pos.x() <= b or pos.y() <= b
                or pos.x() >= self.width() - b or pos.y() >= self.height() - b)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._in_band(e.position().toPoint()):
            self._grab = e.globalPosition().toPoint() - self._win.pos()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._grab is not None and (e.buttons() & Qt.LeftButton):
            self._win.move(e.globalPosition().toPoint() - self._grab)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._grab = None
        super().mouseReleaseEvent(e)


class TitleBar(QWidget):
    """标题栏：拖它移动窗口（无边框窗口没有系统标题栏），双击最大化/还原。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self._win = win
        self._grab = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._grab = e.globalPosition().toPoint() - self._win.pos()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._grab is not None and (e.buttons() & Qt.LeftButton):
            # 最大化状态下拖动 → 先还原，再跟着鼠标走（和系统窗口一致）
            if self._win.isMaximized():
                self._win.restore_from_max()
                self._grab = QPoint(self._win.width() // 2, theme.px(24))
            self._win.move(e.globalPosition().toPoint() - self._grab)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._grab = None
        self._win.on_title_release()
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._win.toggle_max()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)


def _grip_cursor(edges):
    l = bool(edges & Qt.LeftEdge)
    r = bool(edges & Qt.RightEdge)
    t = bool(edges & Qt.TopEdge)
    b = bool(edges & Qt.BottomEdge)
    if (l or r) and (t or b):
        return Qt.SizeFDiagCursor if ((l and t) or (r and b)) else Qt.SizeBDiagCursor
    if l or r:
        return Qt.SizeHorCursor
    return Qt.SizeVerCursor


class EdgeGrip(QWidget):
    """贴在窗口四边/四角的透明手柄：只负责拖动改大小。"""

    def __init__(self, win, edges):
        super().__init__(win)
        self._win = win
        self._edges = edges
        self._press = None
        self._start_size = None
        self.setCursor(_grip_cursor(edges))
        self.setAttribute(Qt.WA_NoSystemBackground, True)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = QCursor.pos()
            self._start_size = self._win.size()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None:
            self._win.resize_from_grip(self._edges, self._start_size,
                                       QCursor.pos() - self._press)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        was = self._press is not None
        self._press = None
        self._start_size = None
        super().mouseReleaseEvent(e)
        if was:
            self._win.on_resize_committed()


# ============================================================ 主窗口

class MainWindow(QMainWindow):
    RAIL_W = 186          # 左侧竖向导航栏宽度（设计稿尺寸）

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(store.resource("app.ico")))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._drag_pos = None
        self._busy = False
        self._worker = None
        self._scan = None
        self._last_restored = None
        self._selected_preset = None
        self._normal_geo = None        # 最大化前的窗口位置/大小

        # 先确定缩放系数（后续所有控件的尺寸都按它换算），再建界面
        s = self._compute_scale()
        theme.set_scale(s)
        self._banner = CornerBanner()
        self._apply_scale(s, build=True)
        self._fit_to_screen()

        self._make_grips()
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.timeout.connect(self.on_resize_committed)

        # 后台闲置时定期把用不到的页还给系统（任务管理器里的内存会降下来）
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(45000)
        self._idle_timer.timeout.connect(self._on_idle_tick)
        self._idle_timer.start()

        QTimer.singleShot(120, self._cat.fade_in)
        QTimer.singleShot(80, self._refresh_recorded_ui)
        QTimer.singleShot(180, self._refresh_presets_ui)

        # 拖到另一块屏（DPI / 分辨率不同）时重新适配
        QTimer.singleShot(0, self._hook_screen_signal)

    # -------------------------------------------------- 缩放 / 适配屏幕

    @staticmethod
    def _compute_scale(screen=None):
        """按目标屏幕的可用区域算出整体缩放比例（设计稿 1120×780）。

        4K(150%) 这类大屏系数会封顶在 1.0，视觉和原来一模一样；
        2.5K 或缩放比例更高的屏则整体等比缩小，保证一屏放得下、相对位置不变。
        """
        forced = os.environ.get("FOLDER_RESTORE_SCALE")   # 调试用：强制缩放系数
        if forced:
            try:
                return max(0.62, min(1.0, float(forced)))
            except ValueError:
                pass
        scr = screen or QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if scr is None:
            return 1.0
        a = scr.availableGeometry()
        s = min((a.width() - 48) / theme.BASE_W, (a.height() - 64) / theme.BASE_H)
        return max(0.62, min(1.0, s))

    def _apply_scale(self, s, build=False):
        """应用缩放系数。build=True 时重头搭一遍界面。"""
        theme.set_scale(s)
        app = QApplication.instance()
        if app is not None:
            f = QFont(theme.FONT_UI)
            f.setPointSizeF(theme.pt(theme.BASE_PT))
            app.setFont(f)
            app.setStyleSheet(theme.qss())

        self.setMinimumSize(int(round(theme.BASE_W * 0.62)),
                            int(round(theme.BASE_H * 0.62)))
        if getattr(self, "_banner", None) is not None:
            self._banner.on_scale_changed()

        if build:
            self._rebuild_ui()
            return

        # 不重建时至少更新几个尺寸相关的属性
        self._refresh_recorded_ui()
        self._refresh_presets_ui(keep_id=self._selected_preset)
        self.rescale_children()

    def _rebuild_ui(self):
        keep_tool = self.tool_stack.currentIndex() if hasattr(self, "tool_stack") else 0
        keep_tabs = {}
        for t in getattr(self, "_tools", []):
            keep_tabs[t["key"]] = t["stack"].currentIndex()
        keep_pid = self._selected_preset

        old = self.takeCentralWidget()
        root = RootCanvas(self)
        root.setObjectName("Root")
        self.setCentralWidget(root)
        if old is not None:
            old.setParent(None)
            old.deleteLater()

        self._root = root
        self._cat = CatgirlBackdrop(root)
        self._build_ui()

        self._toast = Toast(root)
        self._cat.lower()
        for t in self._tools:
            self._go_tab(t["key"], keep_tabs.get(t["key"], 0))
        self._go_tool(keep_tool)
        self._refresh_recorded_ui()
        self._refresh_presets_ui(keep_id=keep_pid)
        # 新建的猫娘是透明起步的，重建后要直接显示，不要再淡入一次
        self._cat.show_now()
        # 关键：setCentralWidget 会把新的中央控件盖到最上层，手柄必须重新 raise，
        # 否则重排之后窗口边缘就再也拖不动了
        self._layout_grips()

    def rescale_children(self):
        """缩放系数变了但没重建时，刷新自绘控件的尺寸。"""
        self._cat.setGeometry(0, 0, theme.px(self.RAIL_W + 96), self._root.height())
        for w in self.findChildren(QWidget):
            f = getattr(w, "on_scale_changed", None)
            if callable(f):
                f()
        self.update()

    def _fit_to_screen(self):
        """窗口尺寸跟着屏幕走：放得下就用设计尺寸，放不下就等比缩到刚好。"""
        w, h = theme.px(theme.BASE_W), theme.px(theme.BASE_H)
        self.resize(w, h)
        scr = self.screen() or QApplication.primaryScreen()
        if scr is not None:
            a = scr.availableGeometry()
            self.move(a.center().x() - self.width() // 2,
                      a.center().y() - self.height() // 2)

    # -------------------------------------------------- 拉窗口 / 等比缩放

    def _make_grips(self):
        self._grips = []
        for edges in (Qt.LeftEdge, Qt.RightEdge, Qt.TopEdge, Qt.BottomEdge,
                      Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.TopEdge,
                      Qt.LeftEdge | Qt.BottomEdge, Qt.RightEdge | Qt.BottomEdge):
            g = EdgeGrip(self, edges)
            g.show()
            g.raise_()
            self._grips.append(g)
        self._layout_grips()

    def _layout_grips(self):
        if not getattr(self, "_grips", None):
            return
        W, H = self.width(), self.height()
        EDGE, CORNER = 5, 14
        for g in self._grips:
            e = g._edges
            l = bool(e & Qt.LeftEdge)
            r = bool(e & Qt.RightEdge)
            t = bool(e & Qt.TopEdge)
            b = bool(e & Qt.BottomEdge)
            if (l or r) and (t or b):
                w = h = CORNER
                x = 0 if l else W - CORNER
                y = 0 if t else H - CORNER
            elif l or r:
                w, h = EDGE, max(1, H - 2 * CORNER)
                x = 0 if l else W - EDGE
                y = CORNER
            else:
                w, h = max(1, W - 2 * CORNER), EDGE
                x = CORNER
                y = 0 if t else H - EDGE
            g.setGeometry(x, y, max(1, w), max(1, h))
            g.raise_()

    def resize_from_grip(self, edges, start_size, delta):
        """拖边/角 → 等比尺寸；拖动时先让布局自适应（跟手），松手后再整体缩放。"""
        sw, sh = start_size.width(), start_size.height()
        if edges & Qt.LeftEdge:
            sw -= delta.x()
        if edges & Qt.RightEdge:
            sw += delta.x()
        if edges & Qt.TopEdge:
            sh -= delta.y()
        if edges & Qt.BottomEdge:
            sh += delta.y()

        horiz = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        vert = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
        k = theme.S
        if horiz and vert:
            k = (sw / float(theme.BASE_W) + sh / float(theme.BASE_H)) / 2.0
        elif horiz:
            k = sw / float(theme.BASE_W)
        else:
            k = sh / float(theme.BASE_H)

        k = max(0.62, min(1.6, k))
        w = int(round(theme.BASE_W * k))
        h = int(round(theme.BASE_H * k))
        if abs(w - self.width()) < 1 and abs(h - self.height()) < 1:
            return
        geo = self.geometry()
        x = geo.x() + (geo.width() - w) if edges & Qt.LeftEdge else geo.x()
        y = geo.y() + (geo.height() - h) if edges & Qt.TopEdge else geo.y()
        self.setGeometry(x, y, w, h)

    def on_title_release(self):
        pass

    # -------------------------------------------------- 最大化 / 还原

    def toggle_max(self):
        if self.isMaximized():
            self.restore_from_max()
        else:
            self._normal_geo = self.geometry()
            self.showMaximized()
            self._set_grips_visible(False)
            self._set_max_icon(True)

    def restore_from_max(self):
        geo = self._normal_geo
        self.showNormal()
        if geo is not None:
            self.setGeometry(geo)
        self._set_grips_visible(True)
        self._set_max_icon(False)
        self._layout_grips()

    def _set_max_icon(self, is_max):
        """最大化/还原按钮的图标与提示跟着状态换。"""
        if not hasattr(self, "btn_max"):
            return
        name = "还原.svg" if is_max else "最大化.svg"
        self.btn_max.setIcon(svg_icon(name, theme.px(17), tint="#7d9cb5",
                                      hover_tint=theme.PRIMARY_PRESS,
                                      disabled_tint="#c6d8e6"))

    def _set_grips_visible(self, vis):
        for g in getattr(self, "_grips", []):
            g.setVisible(vis)
            if vis:
                g.raise_()

    def on_resize_committed(self):
        """松手后：内容按新尺寸重新等比排一遍（字体/间距/猫娘一起缩）。"""
        if self.width() <= 0:
            return
        target = max(0.62, min(1.6, self.width() / float(theme.BASE_W)))
        if abs(target - theme.S) < 0.006:
            return
        self.setUpdatesEnabled(False)
        try:
            self._apply_scale(target, build=True)
        finally:
            self.setUpdatesEnabled(True)
        # 重建后布局要等一次事件循环才算出真实几何，定位延后一拍
        QTimer.singleShot(0, self._relayout_root)

    def _layout_root_children(self):
        """设计稿内部的定位：猫娘贴左下、Esc 提示贴右下。"""
        lay = self._root.layout()
        if lay is not None:
            lay.activate()
        if hasattr(self, "_cat"):
            self._cat.setGeometry(0, 0, theme.px(self.RAIL_W + 96), self._root.height())
        if hasattr(self, "lbl_esc"):
            s = self.lbl_esc.sizeHint()
            self.lbl_esc.setGeometry(
                max(0, self._root.width() - s.width() - theme.px(26)),
                max(0, self._root.height() - s.height() - theme.px(12)),
                s.width(), s.height())
            self.lbl_esc.raise_()

    def _relayout_root(self):
        self._layout_root_children()
        self._layout_grips()
        if hasattr(self, "_toast"):
            self._toast._reposition()
        self._sync_rail_hot_zone()

    def _hook_screen_signal(self):
        h = self.windowHandle()
        if h is not None:
            h.screenChanged.connect(self._on_screen_changed)
        # 显示器插拔 / 分辨率变化：窗口可能被留在已经不存在的坐标上。
        # screenChanged 只在「窗口换屏」时触发，拔掉外接屏时窗口并不会自己换屏，
        # 所以必须单独听这几个信号（这是「外接屏一拔窗口就消失」的根因）。
        gui = QApplication.instance()
        if gui is not None:
            for sig in ("screenRemoved", "screenAdded", "primaryScreenChanged"):
                s = getattr(gui, sig, None)
                if s is not None:
                    try:
                        s.connect(self._on_screens_changed)
                    except Exception:
                        pass
        self._hooked_screens = set()
        self._hook_each_screen()
        # 启动时也自检一次：万一上次是「拔了屏才关的」，配置里的坐标已经不可用
        QTimer.singleShot(200, self._ensure_on_screen)

    def _hook_each_screen(self):
        """给每块屏挂上 availableGeometryChanged（任务栏位置/分辨率变化都会发）。"""
        done = getattr(self, "_hooked_screens", set())
        for scr in QApplication.screens():
            key = id(scr)
            if key in done:
                continue
            try:
                scr.availableGeometryChanged.connect(self._on_screens_changed)
                done.add(key)
            except Exception:
                pass
        self._hooked_screens = done

    def _on_screens_changed(self, *_):
        self._hook_each_screen()
        # 信号可能在屏幕列表变更的瞬间发出，等一轮事件循环再判断
        QTimer.singleShot(120, self._ensure_on_screen)

    def _visible_ratio(self):
        """窗口有多少面积还落在「真的存在」的屏幕可用区里（0~1）。"""
        g = self.frameGeometry()
        area = max(1, g.width() * g.height())
        hit = 0
        for scr in QApplication.screens():
            inter = scr.availableGeometry().intersected(g)
            if not inter.isNull():
                hit += inter.width() * inter.height()
        return min(1.0, hit / float(area))

    def _ensure_on_screen(self):
        """窗口要是跑到屏幕外了（拔掉外接屏最常见），把它拉回主屏居中。

        判断标准用「可见面积占比」而不是「中心点在不在某块屏里」：
        窗口只露一条边的时候人一样没法用，得当成不可见处理。
        """
        if not self.isVisible():
            return
        if self._visible_ratio() >= 0.45:
            self._rescue_children()
            return
        scr = QApplication.primaryScreen()
        if scr is None:
            return      # 一块屏都没有（切换显示模式的瞬间），先不动

        # 最大化/全屏状态下坐标是系统给的，直接 showNormal 才有意义
        if self.isMaximized() or self.isFullScreen():
            self._normal_geo = None
            self.showNormal()
            self._set_max_icon(False)

        a = scr.availableGeometry()
        # 先按新屏重算缩放：外接屏通常是 4K、笔记本屏 2.5K，缩放系数不一样，
        # 不重算的话拉回来会大得装不下。
        # 翻译/恢复正在跑的时候**不重建界面** —— 重建会丢掉正在等回调的页面，
        # 后台线程的结果就打到已删除对象上了。
        s = self._compute_scale(scr)
        if abs(s - theme.S) >= 0.02 and not self._busy and not self._worker:
            keep = self._selected_preset
            self._apply_scale(s, build=True)
            self._selected_preset = keep

        w = min(self.width(), a.width() - theme.px(8))
        h = min(self.height(), a.height() - theme.px(8))
        self.resize(w, h)
        self.move(a.center().x() - w // 2, a.center().y() - h // 2)
        self._layout_grips()
        self._rescue_children()
        self.raise_()
        self.activateWindow()

    def _rescue_children(self):
        """把跑到屏幕外的子窗口（设置 / 大窗口预览 / 历史…）也拉回主屏。

        这些窗口各有各的 parent，但拔屏时它们和主窗口一样会被留在不存在的
        坐标上，用户会以为「面板打不开了」。

        ⚠️ 不能用 `QApplication.topLevelWidgets()`：它只返回**没有 parent** 的
        窗口，而这些对话框的 parent 都是主窗口，压根不在那个列表里。
        必须从 QObject 子树里找，再用 `isWindow()` 筛出独立窗口。
        """
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        a = scr.availableGeometry()
        seen = set()
        for win in self.findChildren(QWidget):
            if win is self or not win.isWindow() or not win.isVisible():
                continue
            if id(win) in seen:
                continue
            seen.add(id(win))
            g = win.frameGeometry()
            area = max(1, g.width() * g.height())
            hit = 0
            for s2 in QApplication.screens():
                inter = s2.availableGeometry().intersected(g)
                if not inter.isNull():
                    hit += inter.width() * inter.height()
            if hit / float(area) >= 0.3:
                continue
            w = min(win.width(), a.width() - theme.px(8))
            h = min(win.height(), a.height() - theme.px(8))
            win.resize(w, h)
            win.move(a.center().x() - w // 2, a.center().y() - h // 2)

    def _on_screen_changed(self, *_):
        """换到另一块屏：重算缩放系数，变化明显就重建界面。"""
        s = self._compute_scale()
        if abs(s - theme.S) < 0.02:
            self._fit_to_screen()
            return
        keep = self._selected_preset
        self._apply_scale(s, build=True)
        self._selected_preset = keep
        self._fit_to_screen()

    # -------------------------------------------------- UI 结构

    def _build_ui(self):
        outer = QVBoxLayout(self._root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_titlebar())

        content = QWidget()
        cl = QHBoxLayout(content)
        cl.setContentsMargins(0, theme.px(2), theme.px(22), theme.px(34))  # 底部留出 Esc 提示
        cl.setSpacing(theme.px(18))

        # 左侧竖向导航 = 工具列表（猫娘画在这个窄栏的下半部分）
        cl.addWidget(self._build_sidebar(), 0)

        # 右侧：每个工具一页；工具内部再分标签页
        self.tool_stack = QStackedWidget()
        self.tool_stack.setObjectName("Stack")
        self._tools = []
        self._build_folder_tool()
        self._build_wordcount_tool()
        self._build_jsonmd_tool()
        self._build_mdbrowser_tool()
        self._build_translate_tool()
        cl.addWidget(self.tool_stack, 1)

        outer.addWidget(content, 1)

        # 右下角的 Esc 提示：纯文字、红字、无边框
        self.lbl_esc = QLabel("按 Esc 紧急中止", self._root)
        self.lbl_esc.setStyleSheet(
            f"color:#d0626f; font-size:{theme.px(11.5)}px; font-weight:600;"
            "background:transparent; border:none; padding:0px;"
        )
        self.lbl_esc.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.lbl_esc.adjustSize()

    def _build_titlebar(self):
        bar = TitleBar(self)
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(theme.px(64))
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(theme.px(24), theme.px(10), theme.px(14), theme.px(6))
        lay.setSpacing(theme.px(10))

        # 透明背景的猫娘头像（不再用带浅蓝底的方形图标）
        icon = QLabel()
        icon.setStyleSheet("background:transparent;")
        pm = QPixmap(os.path.join(ASSETS, "title_icon.png"))
        if pm.isNull():
            pm = QPixmap(os.path.join(ASSETS, "catgirl_face.png"))
        if not pm.isNull():
            # 按 2 倍渲染再设 DPR，避免高 DPI 下被放大糊掉
            pm = pm.scaled(theme.px(38) * 2, theme.px(38) * 2,
                           Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pm.setDevicePixelRatio(2.0)
            icon.setPixmap(pm)
        lay.addWidget(icon)

        col = QVBoxLayout()
        col.setSpacing(0)
        t = QLabel(APP_NAME)
        t.setObjectName("AppTitle")
        col.addWidget(t)
        s = QLabel("文件夹恢复 · 字数统计 · JSON 转 MD · MD 浏览器 · 翻译")
        s.setObjectName("AppSubtitle")
        col.addWidget(s)
        lay.addLayout(col)

        lay.addStretch(1)

        def _win_btn(icon_name, slot, hover_tint=theme.PRIMARY_PRESS):
            # 不设 setToolTip：图标已经够直白，标题栏上再弹一大块提示框反而碍眼
            b = QPushButton()
            b.setObjectName("WinBtn")
            b.setFixedSize(theme.px(34), theme.px(30))
            b.setCursor(Qt.PointingHandCursor)
            b.setIcon(svg_icon(icon_name, theme.px(17), tint="#7d9cb5",
                               hover_tint=hover_tint, disabled_tint="#c6d8e6"))
            b.setIconSize(QSize(theme.px(17), theme.px(17)))
            b.clicked.connect(slot)
            return b

        self.btn_set = _win_btn("设置.svg", self.do_open_settings)
        self.btn_min = _win_btn("最小化.svg", self.showMinimized)
        self.btn_max = _win_btn("最大化.svg", self.toggle_max)
        self.btn_close = _win_btn("关闭.svg", self.close, hover_tint="#ffffff")
        self.btn_close.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:8px;}"
            "QPushButton:hover{background:#f0a0aa;}"
            "QPushButton:pressed{background:#d9747f;}"
        )

        lay.addWidget(self.btn_set)
        lay.addWidget(self.btn_min)
        lay.addWidget(self.btn_max)
        lay.addWidget(self.btn_close)
        return bar

    def _build_sidebar(self):
        # 这一栏本身可以点：下半部分是猫娘立绘，点她就在附近飘一句喵语
        w = ClickableRail()
        w.setFixedWidth(theme.px(self.RAIL_W))
        w.clicked.connect(self._on_catgirl_clicked)
        self.rail = w

        lay = QVBoxLayout(w)
        lay.setContentsMargins(theme.px(14), theme.px(30), theme.px(12), theme.px(8))
        lay.setSpacing(theme.px(10))

        # 竖向工具列表
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_btns = []
        for i, (key, name, hint) in enumerate(self._tool_defs()):
            b = QPushButton(name)
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(theme.px(42))
            b.clicked.connect(lambda _=False, idx=i: self._go_tool(idx))
            self.nav_group.addButton(b, i)
            self.nav_btns.append(b)
            lay.addWidget(b)
        self.nav_btns[0].setChecked(True)

        lay.addSpacing(theme.px(8))

        tip = QLabel(self._tool_defs()[0][2])
        tip.setStyleSheet(
            f"color:#37607f; font-size:{theme.px(12.5)}px; font-weight:700;"
            "background:transparent; padding-left:3px; line-height:150%;"
        )
        self._rail_tip = tip
        lay.addWidget(tip, 0, Qt.AlignLeft)

        # 剩余空间留给背景猫娘（她在下半部分）
        lay.addStretch(1)
        QTimer.singleShot(0, self._sync_rail_hot_zone)
        return w

    def _sync_rail_hot_zone(self):
        """把「可点区域」定在导航与文案下方，也就是猫娘身上。"""
        if not hasattr(self, "rail"):
            return
        y = theme.px(30)
        for b in self.nav_btns:
            y = max(y, b.geometry().bottom())
        for c in self.rail.findChildren(QLabel):
            y = max(y, c.geometry().bottom())
        self.rail.set_hot_top(y + theme.px(10))

    def _on_catgirl_clicked(self, pos):
        """pos 是左侧栏内的坐标；只有真点到人物本体时才飘字。"""
        origin = self.rail.mapTo(self._root, QPoint(0, 0))
        p = origin + pos
        if not self._cat.hit_test(self._cat.mapFrom(self._root, p)):
            return
        # 活动范围：左侧栏 + 一点余量，避免飘字压到右边的卡片上
        bounds = QRect(theme.px(2), origin.y(),
                       self.rail.width() + theme.px(14), self.rail.height())
        spawn_meow(self._root, p, bounds=bounds)

    # -------------------------------------------------- 工具 / 标签页

    def _tool_defs(self):
        """左侧工具顺序：(key, 名称, 左侧提示语)。"""
        return [
            ("folder", "文件夹恢复", "记录一次\n随时叫回来"),
            ("wordcount", "字数统计", "粘文字\n即时统计"),
            ("jsonmd", "JSON转MD", "粘 JSON\n转成 MD"),
            ("mdbrowser", "MD浏览器", "粘 Markdown\n直接看版"),
            ("translate", "翻译", "中英互译\n多风格多领域"),
        ]

    def _make_tool(self, key, name, hint, tabs, settings_factory=None):
        """造一个工具页：顶部胶囊标签页 + 内容栈。tabs = [(标签名, 控件), ...]"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QHBoxLayout()
        bar.setContentsMargins(theme.px(2), theme.px(6), 0, theme.px(8))
        bar.setSpacing(theme.px(10))
        group = QButtonGroup(self)
        group.setExclusive(True)
        stack = QStackedWidget()
        stack.setObjectName("Stack")
        btns = []
        for i, (title, w) in enumerate(tabs):
            b = QPushButton(title)
            b.setObjectName("ToolTab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(theme.px(38))
            b.clicked.connect(lambda _=False, idx=i, k=key: self._go_tab(k, idx))
            group.addButton(b, i)
            btns.append(b)
            bar.addWidget(b)
            stack.addWidget(w)
        bar.addStretch(1)
        lay.addLayout(bar)
        lay.addWidget(stack, 1)

        # 只有一个标签页的工具就不显示标签栏了
        for b in btns:
            b.setVisible(len(tabs) > 1)
        if len(tabs) > 1:
            btns[0].setChecked(True)

        self.tool_stack.addWidget(page)
        self._tools.append({"key": key, "name": name, "hint": hint, "page": page,
                            "stack": stack, "btns": btns, "settings": settings_factory})
        return stack

    def _build_folder_tool(self):
        tabs = [("文件夹恢复", self._build_restore_tab()),
                ("预设", self._build_presets_tab())]
        self._folder_stack = self._make_tool("folder", "文件夹恢复",
                                             "记录一次\n随时叫回来", tabs)

    def _build_wordcount_tool(self):
        from wordcount import WordCountPage, WordCountSettings
        self._wc_page = WordCountPage()
        factory = (lambda: WordCountSettings(on_change=self._wc_page.apply_settings))
        self._make_tool("wordcount", "字数统计", "粘文字\n即时统计",
                        [("字数统计", self._wc_page)], factory)

    def _build_jsonmd_tool(self):
        from jsonmd import JsonMdPage, JsonMdSettings
        self._jsonmd_page = JsonMdPage(on_wordcount=self._send_to_wordcount,
                                       on_translate=self._send_to_translate)
        factory = (lambda: JsonMdSettings(on_change=self._jsonmd_page.apply_settings))
        self._make_tool("jsonmd", "JSON转MD", "粘 JSON\n转成 MD",
                        [("JSON转MD", self._jsonmd_page)], factory)

    def _build_mdbrowser_tool(self):
        from mdbrowser import MdBrowserPage
        self._mdb_page = MdBrowserPage()
        # 纯前端渲染，没有设置项
        self._make_tool("mdbrowser", "MD浏览器", "粘 Markdown\n直接看版",
                        [("MD浏览器", self._mdb_page)], None)

    def _build_translate_tool(self):
        from translate import TranslatePage, TranslationSettings
        self._tr_page = TranslatePage(on_toast=self._toast_msg)
        factory = (lambda: TranslationSettings(on_change=self._tr_page.apply_settings))
        self._make_tool("translate", "翻译", "中英互译\n多风格多领域",
                        [("翻译", self._tr_page)], factory)
        # 「统计字数」按钮要跳到字数统计工具，需要主窗口配合
        self._tr_page._on_wordcount = self._send_to_wordcount

    def _send_to_wordcount(self, text):
        """把 JSON 转出来的 Markdown 塞进字数统计，并跳过去。"""
        self._wc_page.set_text(text)
        for i, t in enumerate(self._tools):
            if t["key"] == "wordcount":
                self._go_tool(i)
                break
        self._toast_msg("已送到字数统计", "success")

    def _send_to_translate(self, text):
        """把 Markdown 放进翻译输入框并跳过去。只是搬运，**不会自动翻译**。"""
        self._tr_page.set_text(text)
        for i, t in enumerate(self._tools):
            if t["key"] == "translate":
                self._go_tool(i)
                break
        self._toast_msg("已放进翻译输入框，确认语种后点「翻译」", "success")

    def _tool(self, key):
        for t in getattr(self, "_tools", []):
            if t["key"] == key:
                return t
        return None

    def _go_tool(self, idx):
        if not self._tools:
            return
        idx = max(0, min(idx, len(self._tools) - 1))
        self.tool_stack.setCurrentIndex(idx)
        for i, b in enumerate(self.nav_btns):
            b.setChecked(i == idx)
        if hasattr(self, "_rail_tip"):
            self._rail_tip.setText(self._tools[idx]["hint"])
        QTimer.singleShot(0, self._sync_rail_hot_zone)

    def _go_tab(self, key, idx):
        t = self._tool(key)
        if not t:
            return
        t["stack"].setCurrentIndex(idx)
        for i, b in enumerate(t["btns"]):
            b.setChecked(i == idx)

    # 兼容旧调用
    def _go_page(self, idx):
        self._go_tool(idx)

    def _set_nav_enabled(self, on):
        for b in self.nav_btns:
            b.setEnabled(on)
        for t in getattr(self, "_tools", []):
            for b in t["btns"]:
                b.setEnabled(on)

    def do_open_settings(self):
        if self._busy:
            return
        SettingsDialog(self, self._tools).exec()

    # -------------------------------------------------- 标签页 1：文件夹恢复

    def _build_restore_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, theme.px(12), 0, 0)
        outer.setSpacing(0)

        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.NoFrame)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(theme.px(4), theme.px(2), theme.px(10), theme.px(8))
        lay.setSpacing(theme.px(14))

        # 顶部操作卡
        card = Card(pad=theme.px(20))
        cl = card.body()
        cl.setSpacing(theme.px(12))

        top = QHBoxLayout()
        top.setSpacing(theme.px(12))

        self.btn_record = SoftButton("记录当前打开的文件夹", kind="primary")
        # 记录.svg 本体是深灰 + 绿放大镜的双色图标，蓝底上统一成白色更清晰
        self.btn_record.setIcon(svg_icon("记录.svg", theme.px(19), tint="#ffffff",
                                         hover_tint="#ffffff", disabled_tint="#e8f3fb"))
        self.btn_record.setIconSize(QSize(theme.px(19), theme.px(19)))
        self.btn_record.setMinimumHeight(theme.px(44))
        self.btn_record.clicked.connect(self.do_record)
        top.addWidget(self.btn_record)

        self.btn_restore = SoftButton("恢复已记录的文件夹", kind="ghost")
        self.btn_restore.setIcon(svg_icon("恢复.svg", theme.px(19), tint=theme.PRIMARY_PRESS,
                                          hover_tint=theme.PRIMARY, disabled_tint="#b9cfdf"))
        self.btn_restore.setIconSize(QSize(theme.px(19), theme.px(19)))
        self.btn_restore.setMinimumHeight(theme.px(44))
        self.btn_restore.clicked.connect(self.do_restore_recorded)
        top.addWidget(self.btn_restore)

        top.addStretch(1)

        self.lbl_recorded_at = QLabel("尚未记录")
        self.lbl_recorded_at.setObjectName("Hint")
        top.addWidget(self.lbl_recorded_at)

        cl.addLayout(top)

        cl.addWidget(Divider())

        # 统计
        stats = QHBoxLayout()
        stats.setSpacing(theme.px(26))
        self.st_windows = StatPill("0", "窗口")
        self.st_tabs = StatPill("0", "标签页")
        stats.addWidget(self.st_windows)
        stats.addWidget(self.st_tabs)
        stats.addStretch(1)
        self.lbl_status = QLabel("点「记录」把现在的窗口结构存下来")
        self.lbl_status.setObjectName("Body")
        stats.addWidget(self.lbl_status, 0, Qt.AlignBottom)
        cl.addLayout(stats)

        lay.addWidget(card)

        # 记录内容预览
        prev = Card(pad=theme.px(16))
        pl = prev.body()
        pl.setSpacing(theme.px(8))

        head = QHBoxLayout()
        h = QLabel("已记录的内容")
        h.setObjectName("H2")
        head.addWidget(h)
        head.addStretch(1)
        self.btn_clear = QPushButton("清除记录")
        self.btn_clear.setObjectName("Danger")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.clicked.connect(self.do_clear_recorded)
        head.addWidget(self.btn_clear)
        pl.addLayout(head)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recorded_host = QWidget()
        self.recorded_lay = QVBoxLayout(self.recorded_host)
        self.recorded_lay.setContentsMargins(0, 0, 4, 0)
        self.recorded_lay.setSpacing(2)
        self.recorded_lay.addStretch(1)
        area.setWidget(self.recorded_host)
        pl.addWidget(area, 1)

        lay.addWidget(prev, 1)
        scroller.setWidget(inner)
        outer.addWidget(scroller)
        return page

    # -------------------------------------------------- 标签页 2：预设

    def _build_presets_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, theme.px(14), 0, 0)
        lay.setSpacing(theme.px(14))

        # 操作条
        bar = Card(pad=theme.px(16))
        bl = bar.body()
        row = QHBoxLayout()
        row.setSpacing(theme.px(10))

        self.btn_new_preset = SoftButton("＋ 把当前窗口存为预设", kind="primary")
        self.btn_new_preset.clicked.connect(self.do_new_preset)
        row.addWidget(self.btn_new_preset)

        self.btn_manual = SoftButton("手动添加预设", kind="ghost")
        self.btn_manual.clicked.connect(self.do_manual_preset)
        row.addWidget(self.btn_manual)

        row.addStretch(1)

        self.lbl_preset_count = QLabel("0 个预设")
        self.lbl_preset_count.setObjectName("Hint")
        row.addWidget(self.lbl_preset_count)

        bl.addLayout(row)
        lay.addWidget(bar)

        # 两栏：列表 + 详情
        split = QHBoxLayout()
        split.setSpacing(theme.px(14))

        left = Card(pad=theme.px(12))
        ll = left.body()
        ll.setSpacing(theme.px(6))
        lh = QLabel("我的预设")
        lh.setObjectName("H2")
        ll.addWidget(lh)

        self.preset_list = QListWidget()
        self.preset_list.setSpacing(2)
        self.preset_list.setCursor(Qt.PointingHandCursor)
        self.preset_list.currentItemChanged.connect(self.on_preset_selected)
        ll.addWidget(self.preset_list, 1)
        left.setFixedWidth(theme.px(280))
        split.addWidget(left)

        right = Card(pad=theme.px(16))
        rl = right.body()
        rl.setSpacing(theme.px(8))

        rh = QHBoxLayout()
        self.preset_title = QLabel("选择一个预设")
        self.preset_title.setObjectName("H1")
        rh.addWidget(self.preset_title)
        rh.addStretch(1)
        rl.addLayout(rh)

        self.preset_meta = QLabel("左侧列表里点一个预设，这里会显示它包含的窗口和标签页。")
        self.preset_meta.setObjectName("Body")
        self.preset_meta.setWordWrap(True)
        rl.addWidget(self.preset_meta)

        rl.addWidget(Divider())

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.detail_host = QWidget()
        self.detail_lay = QVBoxLayout(self.detail_host)
        self.detail_lay.setContentsMargins(0, 0, 4, 0)
        self.detail_lay.setSpacing(2)
        self.detail_lay.addStretch(1)
        area.setWidget(self.detail_host)
        rl.addWidget(area, 1)

        rl.addWidget(Divider())

        # 替换模式：应用这个预设前，先关掉当前所有资源管理器窗口
        swrow = QHBoxLayout()
        swrow.setSpacing(theme.px(10))
        self.sw_replace = Switch()
        self.sw_replace.setEnabled(False)
        self.sw_replace.toggled.connect(self.on_replace_toggled)
        swrow.addWidget(self.sw_replace, 0, Qt.AlignVCenter)

        swcol = QVBoxLayout()
        swcol.setSpacing(theme.px(1))
        sw_title = QLabel("替换：先关闭现有文件夹")
        sw_title.setStyleSheet(
            f"color:{theme.TEXT}; font-size:{theme.px(12.5)}px; font-weight:700;"
            "background:transparent;"
        )
        sw_hint = QLabel("开启后，恢复这个预设前会先关掉当前打开的所有文件夹")
        sw_hint.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;"
        )
        swcol.addWidget(sw_title)
        swcol.addWidget(sw_hint)
        swrow.addLayout(swcol, 1)
        rl.addLayout(swrow)

        rl.addSpacing(theme.px(2))

        btnrow = QHBoxLayout()
        btnrow.setSpacing(theme.px(10))
        self.btn_apply = SoftButton("恢复这个预设", kind="primary")
        self.btn_apply.clicked.connect(self.do_restore_preset)
        self.btn_apply.setEnabled(False)
        self.btn_apply.setMinimumHeight(theme.px(42))
        btnrow.addWidget(self.btn_apply, 1)

        rl.addLayout(btnrow)

        btnrow2 = QHBoxLayout()
        btnrow2.setSpacing(theme.px(10))
        self.btn_overwrite = QPushButton("用当前窗口覆盖")
        self.btn_overwrite.setCursor(Qt.PointingHandCursor)
        self.btn_overwrite.clicked.connect(self.do_overwrite_preset)
        self.btn_overwrite.setEnabled(False)
        self.btn_overwrite.setMinimumHeight(theme.px(36))
        btnrow2.addWidget(self.btn_overwrite)

        self.btn_edit = QPushButton("编辑文件夹")
        self.btn_edit.setCursor(Qt.PointingHandCursor)
        self.btn_edit.setToolTip("手动增删这个预设里的文件夹")
        self.btn_edit.clicked.connect(self.do_edit_preset)
        self.btn_edit.setEnabled(False)
        self.btn_edit.setMinimumHeight(theme.px(36))
        btnrow2.addWidget(self.btn_edit)

        self.btn_rename = QPushButton("重命名")
        self.btn_rename.setCursor(Qt.PointingHandCursor)
        self.btn_rename.clicked.connect(self.do_rename_preset)
        self.btn_rename.setEnabled(False)
        self.btn_rename.setMinimumHeight(theme.px(36))
        btnrow2.addWidget(self.btn_rename)

        btnrow2.addStretch(1)

        self.btn_del = QPushButton("删除预设")
        self.btn_del.setObjectName("Danger")
        self.btn_del.setCursor(Qt.PointingHandCursor)
        self.btn_del.clicked.connect(self.do_delete_preset)
        self.btn_del.setEnabled(False)
        self.btn_del.setMinimumHeight(theme.px(36))
        btnrow2.addWidget(self.btn_del)

        rl.addLayout(btnrow2)
        split.addWidget(right, 1)

        lay.addLayout(split, 1)
        return page

    # -------------------------------------------------- 内存

    def _trim_memory(self):
        """把暂时用不到的页还给系统。

        Windows 下进程的工作集不会自己缩回去（用过的页一直留着）；
        调 EmptyWorkingSet 之后任务管理器里的内存会明显下降，被换出的页下次访问会自动换回来。
        """
        try:
            gc.collect()
        except Exception:
            pass
        try:
            import ctypes
            from ctypes import wintypes
            psapi = ctypes.windll.psapi
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.EmptyWorkingSet(wintypes.HANDLE(k32.GetCurrentProcess()))
        except Exception:
            pass

    def _on_minimized(self):
        """最小化：猫娘大图用不到了，先放掉，再归还工作集。"""
        if self.isMinimized():
            try:
                self._cat.release_pixmaps()
            except Exception:
                pass
            self._trim_memory()

    def _on_idle_tick(self):
        """窗口在后台、没有任务在跑时，顺手把工作集收一收。"""
        if self._busy or self.isMinimized():
            return
        if not self.isActiveWindow():
            self._trim_memory()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.WindowStateChange:
            if self.isMinimized():
                QTimer.singleShot(900, self._on_minimized)
            else:
                try:
                    self._cat.reload()
                except Exception:
                    pass
                self._layout_root_children()

    # -------------------------------------------------- 窗口拖动 / 尺寸

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_grips()
        # 猫娘只占左侧导航栏一带，贴左下角，不和竖向导航抢位置
        self._layout_root_children()
        if hasattr(self, "_toast"):
            self._toast._reposition()

    # -------------------------------------------------- 状态刷新

    def _toast_msg(self, text, kind="info"):
        self._toast.pop(text, kind)

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        for b in (self.btn_record, self.btn_restore, self.btn_new_preset,
                  self.btn_manual, self.btn_apply, self.btn_overwrite):
            b.setEnabled(not busy)
        if busy:
            if msg:
                self.lbl_status.setText(msg)
        else:
            self.btn_apply.setEnabled(self._selected_preset is not None)
            self.btn_overwrite.setEnabled(self._selected_preset is not None)
            self._restore_status()

    def _restore_status(self):
        """把底部状态文字还原成「当前记录」的真实状态。

        以前读窗口扫完只把 busy 关掉、不改这句文字，于是它会一直停在
        「正在读取窗口…」——预设页触发的那次扫描尤其明显（扫完没人去更新它）。
        """
        data = store.load_recorded()
        if data:
            self.lbl_status.setText("记录于 " + data["captured_at"])
        else:
            self.lbl_status.setText("点「记录」把现在的窗口结构存下来")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self._busy:
            self._toast_msg("已请求中止…", "warn")
        super().keyPressEvent(e)

    # -------------------------------------------------- 记录

    def do_record(self):
        if self._busy:
            return
        self._start_scan(for_record=True)

    def _start_scan(self, for_record=False, then=None):
        self._set_busy(True, "正在读取窗口…")
        self._scan = ScanWorker(with_active=True)
        self._scan.done.connect(lambda ws: self._on_scan(ws, for_record, then))
        self._scan.phase.connect(self._on_scan_phase)
        self._scan.start()

    def _on_scan_phase(self, done, total):
        """有些窗口系统读不到，要临时翻一遍它们的标签 —— 这一步会动真实窗口，必须提醒。"""
        if done <= 0:
            self.lbl_status.setText("正在读取 %d 个窗口（会自动翻标签）…" % total)
            self._banner.show_banner(
                "正在读取文件夹",
                "会自动翻一下这些窗口的标签，期间请不要动鼠标",
                0.12,
            )
        else:
            self._banner.set_progress(0.12 + 0.8 * (done / float(max(1, total))),
                                      "正在读取 %d / %d 个窗口" % (done, total))

    def _on_scan(self, windows, for_record, then):
        self._banner.hide_banner()     # 兜底读取阶段可能弹过提示条
        self._set_busy(False)      # 这里会把状态文字还原成真实记录状态
        # 读取「Shell 枚举不到的窗口」时抢过焦点，读完全把焦点还回来
        if for_record:
            self.raise_()
            self.activateWindow()
        if for_record:
            if not windows:
                self._toast_msg("现在没有打开的资源管理器窗口", "warn")
                return
            store.save_recorded(windows)
            n_tabs = sum(len(w["tabs"]) for w in windows)
            self._refresh_recorded_ui()

            # 桌面上的窗口数兜底：有的窗口系统不给枚举，别悄悄少记
            missing = core.unread_windows(set(w["hwnd"] for w in windows))
            if missing:
                n_orphan = sum(1 for m in missing if m[2])
                names = "、".join((t or "未命名窗口").replace(" - 文件资源管理器", "")[:14]
                                  for _h, t, _o in missing[:3])
                if len(missing) > 3:
                    names += " 等 %d 个" % len(missing)
                self._toast_msg("已记录 %d 个窗口，另有 %d 个没读到"
                                % (len(windows), len(missing)), "warn")
                self.lbl_status.setText(
                    "记录于 %s　·　没读到：%s%s"
                    % (store.load_recorded()["captured_at"], names,
                       "（旧窗口，关掉重开就能记）" if n_orphan else ""))
            else:
                self._toast_msg("已记录 %d 个窗口 / %d 个标签页" % (len(windows), n_tabs), "success")
                self.lbl_status.setText("记录于 " + store.load_recorded()["captured_at"])
        if then:
            then(windows)

    def _refresh_recorded_ui(self):
        # 清空旧内容
        while self.recorded_lay.count() > 1:
            it = self.recorded_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        data = store.load_recorded()
        if not data:
            self.lbl_recorded_at.setText("尚未记录")
            self.st_windows.set(0)
            self.st_tabs.set(0)
            self.btn_clear.setEnabled(False)
            empty = QLabel("还没有记录。点上面的「记录当前打开的文件夹」，\n"
                           "就会把此刻所有资源管理器窗口和标签页存下来。")
            empty.setObjectName("Body")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"color:#a8c0d2; font-size:{theme.px(12.5)}px;"
                                f"padding:{theme.px(40)}px 0;")
            self.recorded_lay.insertWidget(0, empty)
            return

        self.btn_clear.setEnabled(True)
        self.lbl_recorded_at.setText("记录于 " + data["captured_at"])
        ws = data["windows"]
        self.st_windows.set(len(ws), "窗口")
        self.st_tabs.set(sum(len(w["tabs"]) for w in ws), "标签页")
        for i, w in enumerate(ws, 1):
            self.recorded_lay.insertWidget(self.recorded_lay.count() - 1, WindowGroup(i, w))

    def do_clear_recorded(self):
        if not ConfirmDialog.ask(self, "清除记录",
                                 "确定要清除已记录的窗口结构吗？",
                                 ok_text="清除", danger=True,
                                 detail="预设不受影响。此操作不可撤销。"):
            return
        store.clear_recorded()
        self._refresh_recorded_ui()
        self._toast_msg("记录已清除", "info")

    # -------------------------------------------------- 恢复

    def do_restore_recorded(self):
        data = store.load_recorded()
        if not data:
            self._toast_msg("还没有记录，先点左边的「记录」", "warn")
            return
        self._run_restore(data["windows"], "已记录的文件夹")

    def _run_restore(self, windows, label, close_first=False):
        if self._busy:
            return
        total = sum(len([t for t in w["tabs"] if os.path.isdir(t["path"])]) for w in windows)
        if total == 0:
            self._toast_msg("没有可恢复的文件夹（目录可能都已不存在）", "warn")
            return

        self._set_busy(True, "正在恢复…")
        self._set_nav_enabled(False)

        self._banner.show_banner(
            "正在恢复「%s」" % label,
            "正在关闭现有文件夹…" if close_first else "准备中…",
            0.03,
        )
        self._banner_ok = 0
        self._banner_total = max(1, total)

        self._worker = RestoreWorker(windows, close_first=close_first)
        self._worker.event.connect(self._on_restore_event)
        self._worker.finished_ok.connect(self._on_restore_done)
        self._worker.start()

    def _on_restore_event(self, kind, payload):
        if kind in ("window", "tab"):
            self._banner_ok += 1
            done = self._banner_ok
            self._banner.set_progress(done / self._banner_total,
                                      "已恢复 %d / %d（Esc 可紧急中止）" % (done, self._banner_total))
        elif kind == "info":
            msg = payload.get("message", "")
            if msg:
                self._banner.set_progress(self._banner_ok / self._banner_total, msg)
        elif kind == "error":
            self._toast_msg("打开失败：" + payload.get("path", ""), "error")

    def _on_restore_done(self, res):
        self._banner.hide_banner()
        self._set_busy(False)
        self._set_nav_enabled(True)

        ok, fail, aborted = res["ok"], res["fail"], res["aborted"]
        skipped = res.get("skipped", [])

        if aborted:
            self._toast_msg("已中止：完成 %d 个，剩余未处理" % ok, "warn")
            DoneDialog(self, "已中止", "已按 Esc 紧急中止",
                       ["已完成 %d 个标签页，剩余的没有再继续打开。" % ok],
                       kind="warn").exec()
            return

        parts = ["完成 %d 个标签页" % ok]
        if fail:
            parts.append("失败 %d" % fail)
        if skipped:
            parts.append("%d 个目录已不存在" % len(skipped))
        msg = "，".join(parts)

        self._toast_msg("恢复完毕：" + msg, "success" if not fail else "warn")

        lines = ["已打开 %d 个标签页。" % ok]
        if fail:
            lines.append("有 %d 个未能打开，可稍后再试一次。" % fail)
        if skipped:
            listing = "\n".join(skipped[:6]) + ("\n…等 %d 个" % len(skipped)
                                                if len(skipped) > 6 else "")
            lines.append("以下 %d 个目录已不存在，已跳过：\n%s" % (len(skipped), listing))
        DoneDialog(self, "恢复完毕", "恢复完毕" if not fail else "恢复完成（部分失败）",
                   lines, kind="success" if not fail else "warn").exec()

    # -------------------------------------------------- 预设

    def _refresh_presets_ui(self, keep_id=None):
        if keep_id is None and self._selected_preset is not None:
            keep_id = self._selected_preset
        data = store.load_presets()
        presets = data["presets"]

        self.preset_list.blockSignals(True)
        self.preset_list.clear()
        for p in presets:
            n_win = len(p["windows"])
            n_tab = sum(len(w["tabs"]) for w in p["windows"])
            item = QListWidgetItem()
            item.setData(Qt.UserRole, p["id"])
            item.setSizeHint(QSize(theme.px(240), theme.px(76)))
            item.setText("")  # 用自绘 delegate
            self.preset_list.addItem(item)
            # 自绘卡片
            card = self._preset_card(p["name"], "%d 个窗口 · %d 个标签页" % (n_win, n_tab),
                                     p.get("created_at", ""))
            self.preset_list.setItemWidget(item, card)

        self.lbl_preset_count.setText("%d 个预设" % len(presets))

        target_row = 0
        if keep_id is not None:
            for i, p in enumerate(presets):
                if p["id"] == keep_id:
                    target_row = i
                    break
        self.preset_list.blockSignals(False)

        self.preset_list.setCurrentRow(target_row)
        self.on_preset_selected(self.preset_list.currentItem(), None)

    def _preset_card(self, name, meta, created):
        """左侧预设卡片：单条富文本，避免多 QLabel 行高不一致导致文字重叠。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(theme.px(12), theme.px(7), theme.px(12), theme.px(7))
        lay.setSpacing(0)
        when = (created or "").replace(" ", " · ")
        lab = QLabel(
            '<div style="line-height:118%">'
            f'<span style="color:{theme.TEXT};font-size:{theme.px(13)}px;'
            f'font-weight:600">{name}</span><br>'
            f'<span style="color:{theme.TEXT_MUTED};font-size:{theme.px(10.5)}px">{meta}</span><br>'
            f'<span style="color:{theme.TEXT_FAINT};font-size:{theme.px(9.5)}px">{when}</span>'
            '</div>'
        )
        lab.setTextFormat(Qt.RichText)
        lab.setStyleSheet("background:transparent;")
        lay.addWidget(lab)
        return w

    def on_preset_selected(self, cur, prev):
        # 清空详情
        while self.detail_lay.count() > 1:
            it = self.detail_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if cur is None:
            self._selected_preset = None
            self.preset_title.setText("还没有预设")
            self.preset_meta.setText("点上方「把当前窗口存为预设」，或「手动添加预设」。")
            self.btn_apply.setEnabled(False)
            self.btn_overwrite.setEnabled(False)
            self.btn_edit.setEnabled(False)
            self.btn_rename.setEnabled(False)
            self.btn_del.setEnabled(False)
            self.sw_replace.setChecked(False)
            self.sw_replace.setEnabled(False)
            return

        pid = cur.data(Qt.UserRole)
        p = store.get_preset(pid)
        if not p:
            return
        self._selected_preset = pid
        n_win = len(p["windows"])
        n_tab = sum(len(w["tabs"]) for w in p["windows"])
        self.preset_title.setText(p["name"])
        self.preset_meta.setText("创建于 %s　·　%d 个窗口　·　%d 个标签页"
                                 % (p.get("created_at", "-"), n_win, n_tab))

        for i, w in enumerate(p["windows"], 1):
            self.detail_lay.insertWidget(self.detail_lay.count() - 1, WindowGroup(i, w))

        self.btn_apply.setEnabled(not self._busy)
        self.btn_overwrite.setEnabled(not self._busy)
        self.btn_edit.setEnabled(not self._busy)
        self.btn_rename.setEnabled(True)
        self.btn_del.setEnabled(True)
        self.sw_replace.setChecked(bool(p.get("replace", False)))
        self.sw_replace.setEnabled(not self._busy)

    def on_replace_toggled(self, on):
        if self._selected_preset is None:
            return
        store.set_preset_replace(self._selected_preset, on)
        if on:
            self._toast_msg("已开启替换：恢复前会先关闭现有文件夹", "info")
        else:
            self._toast_msg("已关闭替换：直接打开预设里的文件夹", "info")

    def do_new_preset(self):
        if self._busy:
            return
        self._start_scan(for_record=False,
                         then=lambda ws: self._make_preset_from_windows(ws))

    def _make_preset_from_windows(self, windows):
        if not windows:
            self._toast_msg("现在没有打开的资源管理器窗口", "warn")
            return
        name = self._ask_name("新建预设", "给这个预设起个名字：",
                              default="预设 %d" % (len(store.load_presets()["presets"]) + 1))
        if not name:
            return
        pid = store.add_preset(name, windows)
        self._refresh_presets_ui(keep_id=pid)
        n_tab = sum(len(w["tabs"]) for w in windows)
        self._toast_msg("已保存预设「%s」：%d 个窗口 / %d 个标签页"
                        % (name, len(windows), n_tab), "success")

    def do_manual_preset(self):
        dlg = ManualPresetDialog(self)
        if dlg.exec() and dlg.result_windows:
            pid = store.add_preset(dlg.preset_name,
                                   [{"active_tab": 0, "tabs": [{"path": p, "name": os.path.basename(p)}
                                                               for p in dlg.result_windows]}])
            self._refresh_presets_ui(keep_id=pid)
            self._toast_msg("已创建预设「%s」" % dlg.preset_name, "success")

    def _ask_name(self, title, label, default=""):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, title, label, QLineEdit.Normal, default)
        return name.strip() if ok else ""

    def do_edit_preset(self):
        """手动增删预设里的文件夹（保持原来的窗口分组）。"""
        if self._selected_preset is None or self._busy:
            return
        p = store.get_preset(self._selected_preset)
        if not p:
            return
        dlg = EditPresetDialog(self, p)
        if not dlg.exec():
            return
        name, windows = dlg.result_name, dlg.result_windows
        store.update_preset(self._selected_preset, name=name, windows=windows)
        self._refresh_presets_ui(keep_id=self._selected_preset)
        n_win = len(windows)
        n_tab = sum(len(w["tabs"]) for w in windows)
        self._toast_msg("已保存「%s」：%d 个窗口 / %d 个标签页"
                        % (name, n_win, n_tab), "success")

    def do_rename_preset(self):
        if self._selected_preset is None:
            return
        p = store.get_preset(self._selected_preset)
        name = self._ask_name("重命名预设", "新的名字：", p["name"])
        if not name:
            return
        store.update_preset(self._selected_preset, name=name)
        self._refresh_presets_ui(keep_id=self._selected_preset)
        self._toast_msg("已重命名为「%s」" % name, "success")

    def do_overwrite_preset(self):
        if self._selected_preset is None or self._busy:
            return
        p = store.get_preset(self._selected_preset)
        if not ConfirmDialog.ask(self, "覆盖预设",
                                 "用当前打开的窗口结构覆盖预设「%s」？" % p["name"],
                                 ok_text="覆盖", detail="原来的内容会被替换。"):
            return
        self._start_scan(for_record=False,
                         then=lambda ws: self._do_overwrite(ws))

    def _do_overwrite(self, windows):
        if not windows:
            self._toast_msg("现在没有打开的资源管理器窗口", "warn")
            return
        store.update_preset(self._selected_preset, windows=windows)
        self._refresh_presets_ui(keep_id=self._selected_preset)
        self._toast_msg("预设已更新", "success")

    def do_delete_preset(self):
        if self._selected_preset is None:
            return
        p = store.get_preset(self._selected_preset)
        if not ConfirmDialog.ask(self, "删除预设", "确定删除预设「%s」？" % p["name"],
                                 ok_text="删除", danger=True,
                                 detail="此操作不可撤销。"):
            return
        store.delete_preset(self._selected_preset)
        self._selected_preset = None
        self._refresh_presets_ui()
        self._toast_msg("已删除预设", "info")

    def do_restore_preset(self):
        if self._selected_preset is None:
            return
        p = store.get_preset(self._selected_preset)
        self._run_restore(p["windows"], p["name"],
                          close_first=bool(p.get("replace", False)))

    def closeEvent(self, e):
        self._banner.hide_banner()
        super().closeEvent(e)


# ============================================================ 手动添加预设对话框


class SettingsDialog(QDialog):
    """设置窗口：左边是「有设置项的工具」，右边是该工具的设置面板。

    没有设置项的工具不会出现在左边（比如目前的「文件夹恢复」就什么都不用配）。
    """

    def __init__(self, parent, tools):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(theme.px(720), theme.px(520))
        self._drag = None

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 46, QColor(30, 70, 105, 85), 8)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(22), theme.px(16), theme.px(22), theme.px(16))
        lay.setSpacing(theme.px(10))

        head = QHBoxLayout()
        head.setSpacing(theme.px(8))
        t = QLabel("设置")
        t.setObjectName("H1")
        head.addWidget(t)
        head.addStretch(1)
        x = QPushButton("✕")
        x.setObjectName("WinBtn")
        x.setFixedSize(theme.px(30), theme.px(28))
        x.setCursor(Qt.PointingHandCursor)
        x.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:8px;"
            "color:#7d9cb5;font-size:13px;}"
            "QPushButton:hover{background:#f0a0aa;color:#ffffff;}"
        )
        x.clicked.connect(self.accept)
        head.addWidget(x)
        lay.addLayout(head)

        lay.addWidget(Divider())

        split = QHBoxLayout()
        split.setSpacing(theme.px(16))

        # 左：有设置项的工具
        self.list = QListWidget()
        self.list.setFixedWidth(theme.px(190))
        self.list.setSpacing(4)
        self.list.setStyleSheet(
            f"QListWidget{{background:transparent;border:none;}}"
            f"QListWidget::item{{color:{theme.TEXT}; font-size:{theme.px(13)}px;"
            f"padding-left:{theme.px(12)}px;}}"
            f"QListWidget::item:selected{{color:{theme.PRIMARY_PRESS}; font-weight:700;}}"
        )
        self.stack = QStackedWidget()

        panels = []
        for tl in tools:
            factory = tl.get("settings")
            if not factory:
                continue
            item = QListWidgetItem(tl["name"])
            item.setSizeHint(QSize(theme.px(170), theme.px(38)))
            self.list.addItem(item)
            holder = QWidget()
            hl = QVBoxLayout(holder)
            hl.setContentsMargins(theme.px(14), theme.px(10), theme.px(10), theme.px(10))
            hl.addWidget(factory())
            self.stack.addWidget(holder)
            panels.append(tl["name"])

        self.list.currentRowChanged.connect(self.stack.setCurrentIndex)
        if panels:
            self.list.setCurrentRow(0)
        else:
            empty = QLabel("暂时没有可配置的项。")
            empty.setObjectName("Body")
            self.stack.addWidget(empty)

        split.addWidget(self.list, 0)
        split.addWidget(self.stack, 1)
        lay.addLayout(split, 1)

        foot = QHBoxLayout()
        note = QLabel("改动即时生效，保存在 data/settings.json")
        note.setStyleSheet(
            f"color:{theme.TEXT_FAINT}; font-size:{theme.px(10.5)}px; background:transparent;"
        )
        foot.addWidget(note)
        foot.addStretch(1)
        ok = SoftButton("完成", kind="primary")
        ok.setMinimumHeight(theme.px(38))
        ok.setMinimumWidth(theme.px(104))
        ok.clicked.connect(self.accept)
        foot.addWidget(ok)
        lay.addLayout(foot)

    # --- 无边框拖动 ---
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
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


class EditPresetDialog(QDialog):
    """编辑已有预设：手动增删文件夹，保持原来的「哪个窗口放哪些标签」分组。

    删除的粒度是「文件夹（标签页）」；某个窗口里的标签被删光，保存时这个窗口就不再有。
    """

    def __init__(self, parent, preset):
        super().__init__(parent)
        self.setWindowTitle("编辑预设")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(theme.px(700), theme.px(580))
        self._drag = None

        # 深拷贝一份，取消就什么都不改
        self._groups = [[dict(t) for t in w.get("tabs", [])] for w in preset.get("windows", [])]
        self._active = max(0, len(self._groups) - 1)
        self.result_name = preset.get("name", "")
        self.result_windows = []

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 46, QColor(30, 70, 105, 85), 8)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(24), theme.px(18), theme.px(24), theme.px(18))
        lay.setSpacing(theme.px(12))

        head = QHBoxLayout()
        head.setSpacing(theme.px(8))
        t = QLabel("编辑预设")
        t.setObjectName("H1")
        head.addWidget(t)
        head.addStretch(1)
        x = QPushButton("✕")
        x.setObjectName("WinBtn")
        x.setFixedSize(theme.px(30), theme.px(28))
        x.setCursor(Qt.PointingHandCursor)
        x.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:8px;"
            "color:#7d9cb5;font-size:13px;}"
            "QPushButton:hover{background:#f0a0aa;color:#ffffff;}"
        )
        x.clicked.connect(self.reject)
        head.addWidget(x)
        lay.addLayout(head)

        s = QLabel("选中某个文件夹后点「移除选中」；添加时默认放进最后选中的那个窗口。"
                   "窗口里的文件夹被删光，这个窗口就不再保留。")
        s.setObjectName("Body")
        s.setWordWrap(True)
        lay.addWidget(s)

        rope = QHBoxLayout()
        rope.setSpacing(theme.px(10))
        nl = QLabel("预设名称")
        nl.setObjectName("H2")
        rope.addWidget(nl)
        self.name_edit = QLineEdit(preset.get("name", ""))
        rope.addWidget(self.name_edit, 1)
        lay.addLayout(rope)

        lay.addWidget(Divider())

        self.list = QListWidget()
        self.list.setSpacing(3)
        self.list.itemSelectionChanged.connect(self._on_select)
        lay.addWidget(self.list, 1)

        add = QHBoxLayout()
        add.setSpacing(theme.px(8))
        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("PathEdit")
        self.path_edit.setPlaceholderText("输入或粘贴文件夹路径，回车添加…")
        self.path_edit.returnPressed.connect(self._add_from_edit)
        add.addWidget(self.path_edit, 1)

        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setCursor(Qt.PointingHandCursor)
        self.btn_browse.clicked.connect(self._browse)
        add.addWidget(self.btn_browse)

        self.btn_add = QPushButton("添加")
        self.btn_add.setObjectName("Ghost")
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.clicked.connect(self._add_from_edit)
        add.addWidget(self.btn_add)

        self.btn_newgrp = QPushButton("新建窗口")
        self.btn_newgrp.setCursor(Qt.PointingHandCursor)
        self.btn_newgrp.setToolTip("再加一个空窗口，之后添加的文件夹会放进去")
        self.btn_newgrp.clicked.connect(self._new_group)
        add.addWidget(self.btn_newgrp)
        lay.addLayout(add)

        foot = QHBoxLayout()
        foot.setSpacing(theme.px(10))
        self.btn_rm = QPushButton("移除选中")
        self.btn_rm.setObjectName("Danger")
        self.btn_rm.setCursor(Qt.PointingHandCursor)
        self.btn_rm.clicked.connect(self._remove)
        foot.addWidget(self.btn_rm)
        foot.addStretch(1)

        cancel = QPushButton("取消")
        cancel.setMinimumHeight(theme.px(40))
        cancel.setMinimumWidth(theme.px(88))
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        foot.addWidget(cancel)

        okb = SoftButton("保存", kind="primary")
        okb.setMinimumHeight(theme.px(40))
        okb.setMinimumWidth(theme.px(120))
        okb.clicked.connect(self._accept)
        foot.addWidget(okb)
        lay.addLayout(foot)

        self._rebuild_list()

    # --- 列表 ---
    def _rebuild_list(self, keep=()):
        self.list.blockSignals(True)
        self.list.clear()
        for gi, tabs in enumerate(self._groups):
            hit = QListWidgetItem()
            hit.setFlags(Qt.NoItemFlags)
            hit.setSizeHint(QSize(theme.px(200), theme.px(32)))
            lab = QLabel("窗口 %d　·　%d 个标签%s"
                         % (gi + 1, len(tabs), "（空，等你添加）" if not tabs else ""))
            lab.setStyleSheet(
                f"color:{theme.TEXT_MUTED}; font-size:{theme.px(11)}px; font-weight:700;"
                f"padding-left:{theme.px(4)}px; background:transparent;"
            )
            self.list.addItem(hit)
            self.list.setItemWidget(hit, lab)

            for ti, tab in enumerate(tabs):
                p = tab.get("path", "")
                item = QListWidgetItem()
                item.setData(Qt.UserRole, (gi, ti))
                item.setSizeHint(QSize(theme.px(200), theme.px(58)))
                row = FolderRow(os.path.basename(p.rstrip("\\/")) or p or "（空路径）",
                                p, os.path.isdir(p))
                self.list.addItem(item)
                self.list.setItemWidget(item, row)

        self.list.blockSignals(False)
        if keep:
            self.list.setCurrentRow(keep[0])

    def _on_select(self):
        cur = self.list.currentItem()
        if cur is None:
            return
        data = cur.data(Qt.UserRole)
        if data:
            self._active = data[0]

    def _add_from_edit(self):
        txt = self.path_edit.text().strip().strip('"')
        if not txt:
            return
        self._add_path(txt)
        self.path_edit.clear()

    def _add_path(self, p):
        if not self._groups:
            self._groups.append([])
            self._active = 0
        self._active = max(0, min(self._active, len(self._groups) - 1))
        name = os.path.basename(p.rstrip("\\/")) or p
        self._groups[self._active].append({"path": p, "name": name})
        self._rebuild_list(keep=(self.list.count() - 1,))

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self._add_path(d)

    def _new_group(self):
        self._groups.append([])
        self._active = len(self._groups) - 1
        self._rebuild_list(keep=(self.list.count() - 1,))

    def _remove(self):
        targets = []
        for it in self.list.selectedItems():
            data = it.data(Qt.UserRole)
            if data:
                targets.append(data)
        for gi, ti in sorted(targets, reverse=True):
            if 0 <= gi < len(self._groups) and 0 <= ti < len(self._groups[gi]):
                del self._groups[gi][ti]
        self._rebuild_list()

    def _accept(self):
        name = self.name_edit.text().strip() or self.result_name or "未命名预设"
        windows = [
            {"active_tab": 0,
             "tabs": [{"path": t["path"], "name": t.get("name", "")} for t in g]}
            for g in self._groups if g
        ]
        if not windows:
            DoneDialog(self, "还没有文件夹", "预设里至少要有一个文件夹",
                       ["把窗口里的文件夹都删光了。请先添加至少一个文件夹再保存。"],
                       kind="warn").exec()
            return
        self.result_name = name
        self.result_windows = windows
        self.accept()

    # --- 无边框窗口拖动 ---
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
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


class ManualPresetDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("手动添加预设")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(theme.px(660), theme.px(520))
        self.result_windows = []
        self.preset_name = ""
        self._drag = None

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        shell.addWidget(card)
        shadow(card, 46, QColor(30, 70, 105, 85), 8)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(theme.px(24), theme.px(18), theme.px(24), theme.px(18))
        lay.setSpacing(theme.px(12))

        # 自绘标题行（与主窗口一致的拖拽区）
        head = QHBoxLayout()
        head.setSpacing(theme.px(8))
        t = QLabel("手动添加预设")
        t.setObjectName("H1")
        head.addWidget(t)
        head.addStretch(1)
        x = QPushButton("✕")
        x.setObjectName("WinBtn")
        x.setFixedSize(theme.px(30), theme.px(28))
        x.setCursor(Qt.PointingHandCursor)
        x.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:8px;"
            "color:#7d9cb5;font-size:13px;}"
            "QPushButton:hover{background:#f0a0aa;color:#ffffff;}"
        )
        x.clicked.connect(self.reject)
        head.addWidget(x)
        lay.addLayout(head)

        s = QLabel("输入文件夹路径后回车添加，不存在的目录会标灰但仍可加入。")
        s.setObjectName("Body")
        lay.addWidget(s)

        rope = QHBoxLayout()
        rope.setSpacing(theme.px(10))
        nl = QLabel("预设名称")
        nl.setObjectName("H2")
        rope.addWidget(nl)
        self.name_edit = QLineEdit("我的预设")
        rope.addWidget(self.name_edit, 1)
        lay.addLayout(rope)

        lay.addWidget(Divider())

        self.list = QListWidget()
        self.list.setSpacing(3)
        lay.addWidget(self.list, 1)

        add = QHBoxLayout()
        add.setSpacing(theme.px(8))
        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("PathEdit")
        self.path_edit.setPlaceholderText("输入或粘贴文件夹路径，回车添加…")
        self.path_edit.returnPressed.connect(self._add_from_edit)
        add.addWidget(self.path_edit, 1)

        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setCursor(Qt.PointingHandCursor)
        self.btn_browse.clicked.connect(self._browse)
        add.addWidget(self.btn_browse)

        self.btn_add = QPushButton("添加")
        self.btn_add.setObjectName("Ghost")
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.clicked.connect(self._add_from_edit)
        add.addWidget(self.btn_add)
        lay.addLayout(add)

        foot = QHBoxLayout()
        foot.setSpacing(theme.px(10))
        self.btn_rm = QPushButton("移除选中")
        self.btn_rm.setObjectName("Danger")
        self.btn_rm.setCursor(Qt.PointingHandCursor)
        self.btn_rm.clicked.connect(self._remove)
        foot.addWidget(self.btn_rm)
        foot.addStretch(1)

        cancel = QPushButton("取消")
        cancel.setMinimumHeight(theme.px(40))
        cancel.setMinimumWidth(theme.px(88))
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        foot.addWidget(cancel)

        okb = SoftButton("创建预设", kind="primary")
        okb.setMinimumHeight(theme.px(40))
        okb.setMinimumWidth(theme.px(120))
        okb.clicked.connect(self._accept)
        foot.addWidget(okb)
        lay.addLayout(foot)

    # --- 拖动 ---
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
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

    def _add_from_edit(self):
        txt = self.path_edit.text().strip().strip('"')
        if not txt:
            return
        self._add_path(txt)
        self.path_edit.clear()

    def _add_path(self, p):
        exists = os.path.isdir(p)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, p)
        item.setSizeHint(QSize(theme.px(200), theme.px(58)))   # 48 的行高 + 列表 item 的上下 margin
        row = FolderRow(os.path.basename(p.rstrip("\\/")) or p, p, exists)
        self.list.addItem(item)
        self.list.setItemWidget(item, row)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self._add_path(d)

    def _remove(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def _accept(self):
        paths = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            p = it.data(Qt.UserRole)
            if p and p not in paths:
                paths.append(p)
        if not paths:
            DoneDialog(self, "还没有文件夹", "还没有添加文件夹",
                       ["请至少添加一个文件夹路径，再创建预设。"],
                       kind="warn").exec()
            return
        name = self.name_edit.text().strip() or "未命名预设"
        self.preset_name = name
        self.result_windows = paths
        self.accept()


# ============================================================ 入口

# ------------------------------------------------------------ 单实例

MUTEX_NAME = "Local\\CatgirlToolbox_v1"
_instance_mutex = None      # 必须一直持有，否则锁会被系统回收


def _find_running_window():
    """找已经在跑的那份程序的窗口（按标题精确匹配）。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        try:
            if u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                u.GetWindowTextW(hwnd, buf, 256)
                if buf.value.strip() == APP_NAME:
                    found.append(hwnd)
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(_cb, 0)
    except Exception:
        pass
    return found[0] if found else None


def _activate_existing():
    """把已有实例的窗口还原并提到前台。"""
    hwnd = _find_running_window()
    if not hwnd:
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        u.ShowWindow(hwnd, 9)                 # SW_RESTORE：最小化/最大化都还原
        ok = u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
        if not ok:
            # 前台锁被别的进程占着时的兜底（和点任务栏同一个效果）
            u.SwitchToThisWindow(hwnd, True)
        return True
    except Exception:
        return False


def acquire_single_instance():
    """拿到单实例锁返回 True；已经有实例在跑 → 激活它并返回 False。

    PyInstaller onefile 的父进程只是引导器，这里判断的是真正的应用进程。
    想同时开多份（比如调试）可以设环境变量 FR_ALLOW_MULTI=1。
    """
    global _instance_mutex
    if os.environ.get("FR_ALLOW_MULTI") == "1":
        return True
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()
        if err == 183:                       # ERROR_ALREADY_EXISTS
            if h:
                k32.CloseHandle(wintypes.HANDLE(h))
            _activate_existing()
            return False
        _instance_mutex = h
        return True
    except Exception:
        return True


def main():
    if not acquire_single_instance():
        # 已经有一份在跑了：把它的窗口提到前台，这份直接退出（不建窗口、不占内存）
        sys.exit(0)
    store.ensure_dir()          # 首启动就把 data/ 建好，避免用户找不到数据在哪
    ensure_meow_file()          # 顺便放出可自定义的「喵语.txt」
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setFont(QFont(theme.FONT_UI, 9))
    app.setStyleSheet(theme.qss())
    QPixmapCache.setCacheLimit(2048)      # KB；默认 10MB，这里压到 2MB

    icon = QIcon(store.resource("app.ico"))
    app.setWindowIcon(icon)

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
