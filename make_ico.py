# -*- coding: utf-8 -*-
"""make_ico.py —— 用 assets/app_icon.svg 生成 app.ico（多尺寸）和 app_icon.png（512 圆形版）。

改了图标（app_icon.svg）之后跑一次这个，再重新打包 exe：

    python make_ico.py
    pyinstaller build.spec --distpath release --workpath build2

为什么要自己抠圆形：
    QtSvg 只支持 SVG Tiny 1.2，**不认 clip-path**。这个 SVG 是靠
    `<g clip-path="url(#roundClip)">` 裁成圆形的，直接用 QSvgRenderer 渲染会丢掉裁切、
    四角被背景矩形填满，看起来就是个方块。所以这里按 SVG 里 roundClip 的半径
    （512 视图下 r=252）自己加一条圆形裁剪路径。
"""
import os
import struct
import sys

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPainterPath
from PySide6.QtSvg import QSvgRenderer

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "assets", "app_icon.svg")
ICO = os.path.join(HERE, "app.ico")
PNG = os.path.join(HERE, "assets", "app_icon.png")   # 仓库里留一张圆形源图，方便预览

# Windows 资源管理器/任务栏常用的几档；256 用于大图标视图
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
RADIUS_RATIO = 252.0 / 512.0      # 与 SVG 里 <circle cx=256 cy=256 r=252> 一致
PNG_SIZE = 512                    # app_icon.png 输出尺寸


def render_png(svg, size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    rad = size * RADIUS_RATIO
    path = QPainterPath()
    path.addEllipse(QRectF((size - 2 * rad) / 2.0, (size - 2 * rad) / 2.0, 2 * rad, 2 * rad))
    p.setClipPath(path)
    svg.render(p, QRectF(0, 0, size, size))
    p.end()

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


_APP = None          # 保引用：QGuiApplication 被 GC 掉的话 QPainter 会崩


def main():
    global _APP
    _APP = QGuiApplication(sys.argv)         # QPainter / QSvgRenderer 需要一个 GUI 应用实例
    svg = QSvgRenderer(SVG)
    if not svg.isValid():
        raise SystemExit("读取失败：%s" % SVG)

    blobs = [(s, render_png(svg, s)) for s in SIZES]

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = b""
    data = b""
    for size, blob in blobs:
        w = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob

    with open(ICO, "wb") as f:
        f.write(header + entries + data)
    print("已生成 %s（%d 个尺寸，%d 字节）" % (ICO, len(blobs), os.path.getsize(ICO)))

    # 顺带刷新 assets/app_icon.png：同样是圆形 + 带渐变底
    with open(PNG, "wb") as f:
        f.write(render_png(svg, PNG_SIZE))
    print("已生成 %s（%dx%d）" % (PNG, PNG_SIZE, PNG_SIZE))


if __name__ == "__main__":
    main()
