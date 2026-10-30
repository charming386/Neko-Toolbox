# -*- coding: utf-8 -*-
"""
core.py —— 资源管理器窗口/标签页的扫描与恢复

数据结构（data/recorded.json）:
{
  "captured_at": "2026-10-19 18:30:00",
  "windows": [
    {"active_tab": 2, "tabs": [{"path": "...", "name": "..."}]}
  ]
}

恢复策略:
  1. explorer.exe /n,<path>   起一个独立窗口（第一个标签页）
  2. 用 UI Automation 点标签栏的「+」按钮新增标签，
     再对新增标签对应的 Shell 窗口对象 Navigate2(path) 导航。
     —— 不模拟键盘鼠标、不抢焦点；标签严格按点击顺序从左到右排列。
  3. 兜底：Ctrl+T 再 Navigate2；仍失败则地址栏粘贴路径。
全程响应 Esc 中止。

性能要点（实测）:
  * LocationURL（约 5ms）比 Document.Folder.Self.Path（约 19ms）快 3 倍，优先用它
  * 「+」按钮的 UIA 元素定位一次（约 120ms）后缓存复用，
    否则每次 invoke 都要重新搜索，单次多花 200ms 以上
  * 轮询只看 Windows.Count（约 2ms），不要每次都读全部路径
"""
import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes
from urllib.parse import unquote

import win32api
import win32com.client
import win32con
import win32gui
import win32process

VK_ESCAPE = 0x1B
NAV_OPEN_IN_BACKGROUND_TAB = 0x1000

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.GetWindowThreadProcessId.restype = wintypes.DWORD
_u32.SetForegroundWindow.argtypes = [wintypes.HWND]
_u32.BringWindowToTop.argtypes = [wintypes.HWND]
_u32.IsIconic.argtypes = [wintypes.HWND]
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetAsyncKeyState.restype = ctypes.c_short


class Aborted(Exception):
    """用户按下 Esc 中止。"""


def escape_pressed():
    return bool(_u32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


# Esc 监听线程：恢复过程里主循环几十毫秒~一秒钟才检查一次，
# 用户快速点一下 Esc（<100ms）会被漏掉，所以用独立线程每 8ms 采样一次，
# 采到就置位 _abort_flag，主循环只需读标志。
_abort_flag = threading.Event()
_esc_thread = None
_esc_lock = threading.Lock()


def _esc_loop():
    while not _abort_flag.is_set():
        if escape_pressed():
            _abort_flag.set()
            return
        time.sleep(0.008)


def start_esc_monitor():
    """开始监听 Esc（幂等）。"""
    global _esc_thread
    with _esc_lock:
        _abort_flag.clear()
        if _esc_thread is not None and _esc_thread.is_alive():
            return
        _esc_thread = threading.Thread(target=_esc_loop, daemon=True)
        _esc_thread.start()


def stop_esc_monitor():
    _abort_flag.clear()


def wait_escape_release(timeout=1.5):
    """开始前等 Esc 松开，避免用户按键残留立刻中止。"""
    end = time.time() + timeout
    while time.time() < end and escape_pressed():
        time.sleep(0.03)


def check_abort():
    if _abort_flag.is_set() or escape_pressed():
        raise Aborted()


# ------------------------------------------------------------ COM（每线程一个）

_tls = threading.local()


def _shell():
    """取得当前线程复用的 Shell.Application 对象。"""
    obj = getattr(_tls, "shell", None)
    if obj is None:
        obj = win32com.client.Dispatch("Shell.Application")
        _tls.shell = obj
    return obj


def com_init():
    try:
        import pythoncom
        pythoncom.CoInitialize()
        return True
    except Exception:
        return False


def com_uninit():
    _tls.shell = None
    try:
        import pythoncom
        pythoncom.CoUninitialize()
    except Exception:
        pass


def url_to_path(u):
    """file:///D:/a/b -> D:\\a\\b；虚拟文件夹（::{...}）原样返回。"""
    if not u:
        return ""
    if u.startswith("file:///"):
        p = unquote(u[8:])
        if len(p) > 1 and p[1] == ":":
            return p.replace("/", "\\")
        return "\\\\" + p.replace("/", "\\")
    return u


# ------------------------------------------------------------ 扫描

def _scan_raw():
    """[(hwnd, shell_window_obj, path)]，顺序即标签页顺序。"""
    out = []
    try:
        ws = _shell().Windows()
        for i in range(ws.Count):
            w = ws.Item(i)
            try:
                hwnd = int(w.HWND)
            except Exception:
                continue
            try:
                u = w.LocationURL
            except Exception:
                u = ""
            path = url_to_path(u)
            if not path:
                try:
                    path = w.Document.Folder.Self.Path
                except Exception:
                    path = ""
            out.append((hwnd, w, path))
    except Exception:
        pass
    return out


def _shell_windows():
    """只保留资源管理器窗口（排除 IE 等）的原始扫描结果。"""
    rows = []
    for hwnd, w, path in _scan_raw():
        try:
            name = w.Name or ""
        except Exception:
            name = ""
        if name and "Explorer" not in name and "资源管理器" not in name:
            continue
        rows.append((hwnd, w, path))
    return rows


EXPLORER_CLASSES = ("CabinetWClass", "ExploreWClass")


# 已经确认「Shell 枚举不到」的窗口（Win11 有时把窗口放进独立的 explorer 进程）。
# 这些窗口不会随时间出现，缓存起来可以避免每次记录都白等几秒。
_UNREADABLE = set()


def top_explorer_hwnds():
    """桌面上所有资源管理器顶层窗口的 hwnd（按类名数，不受 Shell 枚举延迟影响）。"""
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) in EXPLORER_CLASSES:
                out.append(hwnd)
        except Exception:
            pass
        return True

    try:
        _u32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return out


def count_explorer_windows(exclude=()):
    """桌面上实际存在的资源管理器窗口数（可排除已知枚举不到的）。"""
    return len([h for h in top_explorer_hwnds() if h not in exclude])


def _snapshot():
    """一次枚举，按 hwnd 分组，保持标签页顺序。"""
    snap, order = {}, []
    for hwnd, _w, path in _shell_windows():
        if hwnd not in snap:
            snap[hwnd] = []
            order.append(hwnd)
        snap[hwnd].append(path)
    return snap, order


def scan_windows(settle=True, max_wait=2.6, step=0.2, min_wait=0.8):
    """枚举所有资源管理器窗口；新窗口在 Shell 里登记有延迟，所以要「等到齐」。

    实测：刚用 explorer.exe 打开的窗口，要约 1.5~2s 才出现在 Shell.Windows() 里，
    在此之前枚举只能看到早就存在的窗口 —— 这正是「明明开了好几个却只记录一个」的原因。

    所以这里：先枚举一次；已经和桌面上的窗口数对得上就立刻返回（常见情况零等待），
    对不上就每 step 秒重来一次，直到够数 / 连续两次不再增加 / 超过 max_wait。
    返回 ({hwnd: [path, ...]}, [hwnd, ...])。
    """
    snap, order = _snapshot()
    if not settle:
        return snap, order

    tops = top_explorer_hwnds()
    _UNREADABLE.intersection_update(tops)          # 窗口关掉了就别再记着
    want = len([h for h in tops if h not in _UNREADABLE])
    if len(snap) >= want:
        _remember_unreadable(tops, snap)
        return snap, order

    t0 = time.time()
    last = len(snap)
    stable = 0
    while time.time() - t0 < max_wait:
        time.sleep(step)
        cur, cur_order = _snapshot()
        for hwnd in cur_order:
            if hwnd not in snap:
                order.append(hwnd)
            snap[hwnd] = cur[hwnd]        # 用最新快照覆盖，标签页顺序保持最新
        if len(snap) >= want:
            break
        if len(snap) == last:
            stable += 1
            if stable >= 2 and (time.time() - t0) >= min_wait:
                break
        else:
            stable = 0
            last = len(snap)
    _remember_unreadable(tops, snap)
    return snap, order


def _remember_unreadable(tops, snap):
    """这一轮仍然枚举不到的窗口，记下来，下次不再等它。"""
    for h in tops:
        if h not in snap:
            _UNREADABLE.add(h)


def shell_pid():
    """当前 shell（拥有任务栏的那个 explorer.exe）的进程号；取不到返回 0。"""
    try:
        hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
        if hwnd:
            return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        pass
    return 0


def _win_pid(hwnd):
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return 0


def unread_windows(seen_hwnds):
    """桌面上还有哪些资源管理器窗口没被 Shell 枚举到（返回 [(hwnd, title, orphan)]）。

    只有「属于当前 shell 进程」的窗口才会被 Shell.Windows() 列出来；explorer 重启后
    上一次会话遗留的旧窗口（不在当前 shell 进程里）永远读不到 —— 这种标 orphan=True，
    提示用户关掉重开即可，而不是让用户以为程序坏了。
    """
    out = []
    me = shell_pid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        try:
            if (win32gui.IsWindowVisible(hwnd)
                    and win32gui.GetClassName(hwnd) in EXPLORER_CLASSES
                    and hwnd not in seen_hwnds):
                pid = _win_pid(hwnd)
                out.append((hwnd, win32gui.GetWindowText(hwnd), bool(me) and pid != me))
        except Exception:
            pass
        return True

    try:
        _u32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return out


# ------------------------------------------------------------ 兜底：直接读窗口界面

def _clip_get():
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return ""


def _clip_set(text):
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        if text:
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
    except Exception:
        pass


def _looks_like_path(v):
    return bool(v) and (":\\" in v or v.startswith("\\\\"))


def read_window_paths(hwnd, pause=0.22):
    """直接读某个资源管理器窗口：逐个标签 Ctrl+L / Ctrl+C 取地址栏里的完整路径。

    只用于 Shell.Windows() 枚举不到的窗口（独立进程里的窗口、explorer 重启后遗留的旧窗口）。
    读完会把活动标签、最小化状态、剪贴板、前台窗口全部还原。
    返回按标签顺序的 [path, ...]；整窗读不出来时返回 []。
    """
    try:
        from pywinauto import Desktop
    except Exception:
        return []

    was_min = False
    try:
        was_min = bool(win32gui.IsIconic(hwnd))
        if was_min:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.25)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.2)
    except Exception:
        pass

    old_clip = _clip_get()
    prev_fg = win32gui.GetForegroundWindow()
    paths = []
    active = 0
    try:
        dlg = Desktop(backend="uia").window(handle=hwnd)
        tabs = dlg.descendants(control_type="TabItem") or []
        for i, t in enumerate(tabs):
            try:
                if t.is_selected():
                    active = i
                    break
            except Exception:
                pass

        if not tabs:
            # 没有标签栏（老式窗口）：直接读当前地址
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            _chord(win32con.VK_CONTROL, ord("L"))
            time.sleep(pause)
            _chord(win32con.VK_CONTROL, ord("C"))
            time.sleep(pause)
            cur = _clip_get()
            paths = [cur] if _looks_like_path(cur) else []
        else:
            for t in tabs:
                try:
                    t.select()
                except Exception:
                    paths.append("")
                    continue
                time.sleep(pause)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                _chord(win32con.VK_CONTROL, ord("L"))
                time.sleep(pause)
                _chord(win32con.VK_CONTROL, ord("C"))
                time.sleep(pause)
                cur = _clip_get()
                paths.append(cur if _looks_like_path(cur) else "")
                # 退出地址栏编辑态，恢复成普通浏览
                win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
                win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.06)

            # 还原原来选中的标签
            try:
                if tabs and 0 <= active < len(tabs):
                    tabs[active].select()
                    time.sleep(0.12)
            except Exception:
                pass
    except Exception:
        paths = []
    finally:
        _clip_set(old_clip)
        try:
            if was_min:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        except Exception:
            pass
        try:
            if prev_fg and prev_fg != hwnd:
                win32gui.SetForegroundWindow(prev_fg)
        except Exception:
            pass

    return [p for p in paths if p]


def list_windows(with_active=False, settle=True, deep=True, on_deep=None):
    """按窗口分组返回 [{hwnd, title, active_tab, tabs:[{path,name,exists}]}]。

    deep=True 时，Shell 枚举不到的窗口会再走一遍「直接读界面」的兜底（会短暂抢占焦点），
    保证「明明开着却记不到」的情况尽量不出现。

    on_deep(done, total)：兜底阶段开始（done=0）和每读完一个窗口时回调，
    让界面能弹提示条、显示进度（这一步会操作真实窗口，必须提醒用户别乱动）。
    """
    groups, order = scan_windows(settle=settle)
    if deep:
        targets = unread_windows(set(groups))
        if targets and on_deep:
            try:
                on_deep(0, len(targets))
            except Exception:
                pass
        for i, (hwnd, _title, _orphan) in enumerate(targets, 1):
            ps = read_window_paths(hwnd)
            if ps:
                groups[hwnd] = ps
                order.append(hwnd)
            if on_deep:
                try:
                    on_deep(i, len(targets))
                except Exception:
                    pass

    result = []
    for hwnd in order:
        paths = groups[hwnd]
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            title = ""
        tabs = []
        for p in paths:
            real = bool(p) and not p.startswith("::")
            tabs.append({
                "path": p,
                "name": os.path.basename(p.rstrip("\\/")) or p,
                "is_folder": real,
                "exists": real and os.path.isdir(p),
            })
        result.append({
            "hwnd": hwnd,
            "title": title,
            "active_tab": _read_active_tab(hwnd, len(paths)) if with_active else -1,
            "tabs": tabs,
        })
    return result


# ------------------------------------------------------------ 关闭现有窗口

def _alive(hwnd):
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:
        return False


def close_all_explorer_windows(timeout=6.0):
    """关闭当前所有资源管理器窗口（一个窗口里的多个标签页会一起关掉）。

    「替换模式」用的：先把现有的都关掉，再打开预设里的文件夹。

    关窗方式按顺序尝试：
      1. Shell 窗口对象的 Quit() —— 多标签窗口也不会弹「关闭所有标签页？」确认框；
      2. 兜底 PostMessage(WM_CLOSE)。
    然后轮询确认窗口真的消失，返回 (关掉的数量, 还没关掉的数量)。
    """
    rows = _shell_windows()
    targets = []
    for hwnd, w, _p in rows:
        if hwnd in [t[0] for t in targets]:
            continue
        targets.append((hwnd, w))
    if not targets:
        return 0, 0

    for hwnd, w in targets:
        try:
            w.Quit()
        except Exception:
            pass

    end = time.time() + max(0.5, timeout * 0.6)
    left = [h for h, _w in targets if _alive(h)]
    while left and time.time() < end:
        time.sleep(0.1)
        left = [h for h in left if _alive(h)]

    # 还有没关掉的，用 WM_CLOSE 再补一刀
    for hwnd in left:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass

    end = time.time() + max(0.5, timeout * 0.4)
    left = [h for h in left if _alive(h)]
    while left and time.time() < end:
        time.sleep(0.1)
        left = [h for h in left if _alive(h)]

    return len(targets) - len(left), len(left)


def _read_active_tab(hwnd, count):
    if count <= 1:
        return 0 if count == 1 else -1
    try:
        from pywinauto import Desktop
        dlg = Desktop(backend="uia").window(handle=hwnd)
        for i, t in enumerate(dlg.descendants(control_type="TabItem", depth=None)[:count]):
            try:
                if t.is_selected():
                    return i
            except Exception:
                continue
    except Exception:
        pass
    return -1


def count_windows():
    return len(list_windows())


# ------------------------------------------------------------ 窗口操作

def force_foreground(hwnd, retries=5):
    """绕开前台锁把窗口提到最前（只在兜底路径里用）。"""
    SW_RESTORE = 9
    for _ in range(retries):
        if not win32gui.IsWindow(hwnd):
            return False
        if _u32.GetForegroundWindow() == hwnd:
            return True
        if _u32.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.12)
        cur = _u32.GetForegroundWindow()
        fg_tid = _u32.GetWindowThreadProcessId(cur, None)
        my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = False
        try:
            if fg_tid and fg_tid != my_tid:
                attached = bool(_u32.AttachThreadInput(fg_tid, my_tid, True))
            _u32.BringWindowToTop(hwnd)
            _u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                _u32.AttachThreadInput(fg_tid, my_tid, False)
        time.sleep(0.1)
        if _u32.GetForegroundWindow() == hwnd:
            return True
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.06)
        try:
            _u32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.1)
    return _u32.GetForegroundWindow() == hwnd


def open_new_window(path):
    subprocess.Popen(["explorer.exe", "/n," + path])


def wait_new_window(path, known, timeout=8.0):
    """等指定路径的新窗口出现，返回 hwnd（0 = 超时）。"""
    low = path.lower()
    end = time.time() + timeout
    while time.time() < end:
        for hwnd, _w, p in _shell_windows():
            if hwnd not in known and p.lower() == low:
                return hwnd
        time.sleep(0.04)
    return 0


def tabs_in_window(hwnd):
    """该窗口里当前所有标签页路径（按顺序）。"""
    return [p for h, _w, p in _shell_windows() if h == hwnd]


# ------------------------------------------------------------ 新建标签页

class TabMaker:
    """给某个窗口建标签页：缓存「+」按钮，只用计数轮询等新标签出现。

    用法:
        mk = TabMaker(hwnd)
        ok, detail = mk.add(path)
        mk.close()
    """

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self._btn = None
        self._btn_tried = False
        self._uia_fail = 0

    # --- 「+」按钮 ---
    def _find_button(self):
        if self._btn is not None:
            return self._btn
        if self._btn_tried and self._uia_fail > 2:
            return None
        self._btn_tried = True
        try:
            from pywinauto import Desktop
            dlg = Desktop(backend="uia").window(handle=self.hwnd)
            for tb in dlg.descendants(control_type="Tab", depth=None):
                for ch in tb.children():
                    try:
                        if ch.element_info.control_type == "Button":
                            self._btn = ch
                            return ch
                    except Exception:
                        continue
            for b in dlg.descendants(control_type="Button", depth=None):
                try:
                    nm = (b.window_text() or "").lower()
                    if "新标签" in nm or "new tab" in nm:
                        self._btn = b
                        return b
                except Exception:
                    continue
        except Exception:
            self._uia_fail += 1
        return None

    def _count(self):
        try:
            return _shell().Windows().Count
        except Exception:
            return -1

    def _count_in_window(self):
        """该窗口的标签数（只看 hwnd，不读路径）。"""
        n = 0
        try:
            ws = _shell().Windows()
            total = ws.Count
            if total <= 0:
                return 0
            for i in range(total):
                try:
                    if int(ws.Item(i).HWND) == self.hwnd:
                        n += 1
                except Exception:
                    continue
        except Exception:
            pass
        return n

    def _last_object(self):
        try:
            ws = _shell().Windows()
            return ws.Item(ws.Count - 1)
        except Exception:
            return None

    def _wait_new(self, tabs_before, total_before, timeout=3.0):
        """等新标签出现，返回最新标签对象。

        只做两步廉价判断：全局 Windows().Count 是否上涨（约 5ms），
        以及集合末项的 HWND 是否就是本窗口——新建的标签总是追加在末尾。
        tabs_before 给 None 时不做逐项核对兜底（调用方已知本窗口标签数时
        传入可提高鲁棒性，但逐项扫描每项约 17ms，能用就别用）。
        注意 tabs_before 是「本窗口标签数」，total_before 是「全局窗口数」，
        两者量纲不同，必须分开比对。
        """
        end = time.time() + timeout
        while time.time() < end:
            try:
                total = _shell().Windows().Count
            except Exception:
                total = -1
            if total > total_before:
                obj = self._last_object()
                if obj is not None:
                    try:
                        if int(obj.HWND) == self.hwnd:
                            return obj
                    except Exception:
                        pass
                if tabs_before is not None and self._count_in_window() > tabs_before:
                    return obj
            time.sleep(0.005)
        return None

    def add(self, path, wait_settle=True):
        """新建一个标签并导航到 path。返回 (ok, detail)。

        wait_settle=False 时不等待地址栏落定（省下每个标签约 0.35s），
        调用方需要在全部标签建好后自行统一校验（restore 就是这么做的）。
        """
        total_before = self._count()

        obj = None

        btn = self._find_button()
        if btn is not None:
            try:
                btn.invoke()
            except Exception:
                self._btn = None
            else:
                obj = self._wait_new(None, total_before, 2.5)

        if obj is None:
            # 兜底：Ctrl+T（需要短暂置前）
            if force_foreground(self.hwnd, retries=3):
                _chord(win32con.VK_CONTROL, ord("T"))
                obj = self._wait_new(None, total_before, 2.0)

        if obj is None:
            return False, "无法新建标签页"

        try:
            obj.Navigate2(path)
        except Exception:
            try:
                obj.Navigate2(path, 0)
            except Exception as e:
                return False, "导航失败：%s" % e

        if not wait_settle:
            return True, "ok(未等待)"

        # 只等这一个标签的地址变成目标路径
        low = path.lower()
        end = time.time() + 3.0
        while time.time() < end:
            try:
                if url_to_path(obj.LocationURL).lower() == low:
                    return True, "ok"
            except Exception:
                pass
            time.sleep(0.01)

        # 最后手段：地址栏粘贴
        if force_foreground(self.hwnd, retries=3):
            _type_path_via_addressbar(path)
            if low in [p.lower() for p in tabs_in_window(self.hwnd)]:
                return True, "ok(地址栏)"
        return False, "导航后未检测到该路径"

    def select(self, index):
        """UIA 选中第 index 个标签。"""
        if index is None or index < 0:
            return False
        try:
            from pywinauto import Desktop
            dlg = Desktop(backend="uia").window(handle=self.hwnd)
            tabs = dlg.descendants(control_type="TabItem", depth=None)
            if index >= len(tabs):
                return False
            for i, t in enumerate(tabs):
                try:
                    if t.is_selected():
                        if i == index:
                            return True
                        break
                except Exception:
                    pass
            tabs[index].select()
            return True
        except Exception:
            return False

    def close(self):
        self._btn = None


def _chord(*vks):
    for v in vks:
        win32api.keybd_event(v, 0, 0, 0)
    for v in reversed(vks):
        win32api.keybd_event(v, 0, win32con.KEYEVENTF_KEYUP, 0)


def _type_path_via_addressbar(path):
    """Ctrl+L → 全选 → 粘贴 → 回车。中文路径必须走剪贴板。"""
    import win32clipboard

    saved = None
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                saved = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass

    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, path)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return False

    _chord(win32con.VK_CONTROL, ord("L"))
    time.sleep(0.3)
    _chord(win32con.VK_CONTROL, ord("A"))
    time.sleep(0.08)
    _chord(win32con.VK_CONTROL, ord("V"))
    time.sleep(0.3)
    win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
    win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)

    if saved:
        time.sleep(0.15)
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, saved)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass
    return True


# 兼容旧接口
def open_tab(hwnd, path):
    mk = TabMaker(hwnd)
    try:
        return mk.add(path)
    finally:
        mk.close()


def open_tab_in_background(hwnd, path):
    return open_tab(hwnd, path)


# ------------------------------------------------------------ 恢复

def _settle_paths(hwnd, wanted, timeout=4.0):
    """等窗口里出现 wanted 中的全部路径，返回仍缺失的路径列表。

    只轮询一次集合（一次 COM 枚举拿全窗口路径），比逐个标签等更快。
    """
    low_wanted = [p.lower() for p in wanted]
    end = time.time() + timeout
    have = set()
    while time.time() < end:
        got = {p.lower() for p in tabs_in_window(hwnd)}
        have = {p for p in low_wanted if p in got}
        if len(have) == len(low_wanted):
            return []
        time.sleep(0.02)
    return [p for p in wanted if p.lower() not in have]


def restore(windows, on_event=None, want_active_tab=True, want_focus=False):
    """按 windows 列表恢复界面。

    windows: [{"active_tab": int, "tabs": [{"path": ...}]}]
    on_event: 回调 (kind, payload)，kind ∈ {"window","tab","error","info"}
    返回 {"ok": int, "fail": int, "skipped": [path], "aborted": bool}

    性能：新建窗口（explorer.exe /n,）本身要 2~3 秒，所以在处理当前窗口
    标签页的同时就把「下一个窗口」先 spawn 出去，把这 2~3 秒重叠掉。
    """
    def emit(kind, **payload):
        if on_event:
            on_event(kind, payload)

    ok = fail = 0
    skipped = []
    made = {}
    aborted = False

    # 预处理：把每个窗口里仍然存在的目录挑出来
    plan = []
    for win in windows:
        alive = []
        for t in win.get("tabs", []):
            p = t.get("path", "")
            if not p or p.startswith("::"):
                continue
            if os.path.isdir(p):
                alive.append(p)
            else:
                skipped.append(p)
        plan.append((win, alive))

    # 预热：先 spawn 第一个窗口，记下 spawn 前的窗口集合
    primed = {}   # wi -> (first_path, known_set)

    def prime(wi):
        """先把第 wi 个窗口 spawn 出去（不等待）。"""
        if wi in primed or wi >= len(plan) + 1:
            return
        _win, alive = plan[wi - 1]
        if not alive:
            return
        known = {h for h, _w, _p in _shell_windows()}
        open_new_window(alive[0])
        primed[wi] = (alive[0], known)

    try:
        wait_escape_release(1.0)
        start_esc_monitor()
        emit("info", message="正在打开第 1 个窗口…")
        prime(1)

        for wi, (win, alive) in enumerate(plan, 1):
            check_abort()

            if not alive:
                emit("info", window=wi, message="目录都不存在，已跳过此窗口")
                continue

            first, known = primed.get(wi, (None, None))
            if first is None:
                # 补 spawn（前一个窗口为空被跳过等情况）
                known = {h for h, _w, _p in _shell_windows()}
                first = alive[0]
                open_new_window(first)
            primed.pop(wi, None)

            emit("info", window=wi, message="等待第 %d 个窗口出现…" % wi)
            hwnd = wait_new_window(first, known, timeout=8.0)
            if not hwnd:
                for h, _w, p in _shell_windows():
                    if p.lower() == first.lower():
                        hwnd = h
                        break
            if not hwnd:
                fail += 1
                emit("error", window=wi, path=first, message="窗口未能创建")
                continue

            made[wi] = hwnd
            ok += 1
            emit("window", window=wi, path=first, total=len(alive))

            # 立刻预 spawn 下一个窗口，和下面的建标签并行
            prime(wi + 1)

            if len(alive) == 1:
                continue

            mk = TabMaker(hwnd)
            try:
                # 流水线：先全部建出来（不逐个等落定），最后统一校验，
                # 省掉每个标签约 0.35s 的串行等待。
                pending = []
                for p in alive[1:]:
                    check_abort()
                    done, detail = mk.add(p, wait_settle=False)
                    if done:
                        pending.append(p)
                    else:
                        fail += 1
                        emit("error", window=wi, path=p,
                             message=detail or "标签页未能打开")

                if pending:
                    missing = _settle_paths(hwnd, alive, timeout=4.0)
                    ok += len(pending) - len(missing)
                    fail += len(missing)
                    for p in pending:
                        if p in missing:
                            emit("error", window=wi, path=p,
                                 message="导航后未检测到该路径")
                        else:
                            emit("tab", window=wi, path=p)
            finally:
                mk.close()

        if want_active_tab:
            for wi, hwnd in made.items():
                idx = plan[wi - 1][0].get("active_tab", -1)
                if idx is not None and idx >= 0:
                    mk = TabMaker(hwnd)
                    try:
                        mk.select(idx)
                    finally:
                        mk.close()

        if want_focus and made:
            force_foreground(made[max(made)])

    except Aborted:
        aborted = True
        emit("info", message="已按 Esc 中止")
    finally:
        stop_esc_monitor()

    return {"ok": ok, "fail": fail, "skipped": skipped, "aborted": aborted}
