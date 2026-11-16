# -*- coding: utf-8 -*-
"""生成 Windows 样式的 X 光标主题（箭头、手形、文本 I 形），供自动录屏的虚拟屏使用。

光标按像素图绘制，写成 Xcursor 文件格式，不依赖额外软件；带一层浅阴影，接近 Windows 默认指针。
虚拟屏里的程序通过 XCURSOR_PATH、XCURSOR_THEME 加载这套主题，ffmpeg 录屏时直接录下系统指针。
"""
import struct
from pathlib import Path

# 主题名用 default：Chromium 不读 XCURSOR_THEME，没有 GTK 指针主题设置时按 default 在 XCURSOR_PATH 里找
THEME = 'default'
SIZE = 32
# B 黑色描边，W 白色填充，空格透明
ARROW = [
    'B',
    'BB',
    'BWB',
    'BWWB',
    'BWWWB',
    'BWWWWB',
    'BWWWWWB',
    'BWWWWWWB',
    'BWWWWWWWB',
    'BWWWWWWWWB',
    'BWWWWWWWWWB',
    'BWWWWWWBBBBB',
    'BWWWBWWB',
    'BWWBBWWB',
    'BWB  BWWB',
    'BB   BWWB',
    'B     BWWB',
    '      BWWB',
    '       BB',
]
HAND = [
    '     BB',
    '    BWWB',
    '    BWWB',
    '    BWWB',
    '    BWWB',
    '    BWWBBB',
    '    BWWBWWBBB',
    '    BWWBWWBWWBB',
    '    BWWBWWBWWBWB',
    'BBB BWWBWWBWWBWB',
    'BWWBBWWWWWWWWWWB',
    'BWWWBWWWWWWWWWWB',
    ' BWWWWWWWWWWWWWB',
    '  BWWWWWWWWWWWWB',
    '  BWWWWWWWWWWWWB',
    '   BWWWWWWWWWWWB',
    '   BWWWWWWWWWWB',
    '    BWWWWWWWWWB',
    '    BWWWWWWWWWB',
    '     BWWWWWWWWB',
    '     BWWWWWWWWB',
    '     BBBBBBBBBB',
]
IBEAM = [
    ' WWW WWW',
    ' WBBWBBW',
    '  WWBWW',
    '   WBW',
    '   WBW',
    '   WBW',
    '   WBW',
    '   WBW',
    '   WBW',
    '   WBW',
    '   WBW',
    '   WBW',
    '   WBW',
    '  WWBWW',
    ' WBBWBBW',
    ' WWW WWW',
]

# 光标名到图形：覆盖 X11 传统名、CSS 名与 Chromium 会查询的哈希名
SHAPES = {
    'arrow': (ARROW, (0, 0), [
        'left_ptr', 'default', 'arrow', 'top_left_arrow', 'X_cursor', 'progress', 'left_ptr_watch', 'wait',
        'watch', 'context-menu', 'help', 'question_arrow', 'copy', 'alias', 'cell', 'crosshair', 'not-allowed',
        'no-drop', 'col-resize', 'row-resize', 'ew-resize', 'ns-resize', 'nesw-resize', 'nwse-resize', 'move',
        'all-scroll', 'fleur', 'sb_h_double_arrow', 'sb_v_double_arrow', 'size_hor', 'size_ver', 'size_all',
        'n-resize', 's-resize', 'e-resize', 'w-resize', 'ne-resize', 'nw-resize', 'se-resize', 'sw-resize',
        'zoom-in', 'zoom-out']),
    'hand': (HAND, (5, 0), [
        'hand', 'hand1', 'hand2', 'pointer', 'pointing_hand', 'grab', 'grabbing', 'openhand', 'closedhand',
        '9d800788f1b08800ae810202380a0822', 'e29285e634086352946a0e7090d73106']),
    'ibeam': (IBEAM, (4, 8), ['xterm', 'text', 'ibeam', 'vertical-text']),
}

BLACK, WHITE, SHADOW = 0xFF000000, 0xFFFFFFFF, 0x40000000


def render(rows: list[str], size: int = SIZE, shadow: bool = True) -> list[int]:
    """像素图转成 size×size 的预乘 ARGB 像素；阴影向右下偏 1 像素。"""
    px = [0] * (size * size)
    shape = {(x, y): c for y, row in enumerate(rows) for x, c in enumerate(row) if c in 'BW'}
    if shadow:
        for (x, y) in shape:
            sx, sy = x + 1, y + 1
            if sx < size and sy < size and (sx, sy) not in shape:
                px[sy * size + sx] = SHADOW
    for (x, y), c in shape.items():
        if x < size and y < size:
            px[y * size + x] = BLACK if c == 'B' else WHITE
    return px


def xcursor(rows: list[str], hot: tuple[int, int], size: int = SIZE) -> bytes:
    """单张图的 Xcursor 文件：文件头、目录一项、图像块（小端）。"""
    pixels = render(rows, size)
    image_type = 0xFFFD0002
    head = struct.pack('<4sIII', b'Xcur', 16, 0x10000, 1)
    toc = struct.pack('<III', image_type, size, 16 + 12)
    chunk = struct.pack('<IIIIIIIII', 36, image_type, size, 1, size, size, hot[0], hot[1], 0)
    return head + toc + chunk + struct.pack(f'<{size * size}I', *pixels)


def ensure_theme(root: Path) -> Path:
    """在 root 下写出光标主题（每次覆盖写，图形改了也能生效），返回给 XCURSOR_PATH 用的目录。"""
    d = root / THEME / 'cursors'
    d.mkdir(parents=True, exist_ok=True)
    for rows, hot, names in SHAPES.values():
        data = xcursor(rows, hot)
        for n in names:
            (d / n).write_bytes(data)
    (root / THEME / 'index.theme').write_text(
        f'[Icon Theme]\nName={THEME}\nComment=Windows 样式指针（自动录屏用）\n', encoding='utf-8')
    return root
