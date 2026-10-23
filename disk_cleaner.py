"""硬盘清理工具 —— 桌面应用界面。

启动方式:
    双击 `启动清理工具.bat`
    或在命令行执行: python disk_cleaner.py

界面: 深色侧边栏导航 + 浅色主工作区, 全部控件均为自绘, 不依赖第三方库。
功能: 临时文件与系统缓存清理 / 回收站清空 / 大文件扫描 / 重复文件查找。
"""

from __future__ import annotations

import csv
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cleaner_core as core

FONT = "Microsoft YaHei UI"
UNIT_TABLE = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def _resource_path(name: str) -> str:
    """取随程序分发的资源路径; 兼容源码运行与 PyInstaller 打包运行。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


# --------------------------------------------------------------- 配色 / 尺寸

C = {
    "sidebar": "#141b27",
    "sidebar_hi": "#1e2839",
    "sidebar_text": "#93a1b5",
    "sidebar_text_hi": "#ffffff",
    "bg": "#f4f6fb",
    "card": "#ffffff",
    "card_line": "#e6eaf2",
    "text": "#1b2430",
    "muted": "#7b8798",
    "faint": "#aab4c2",
    "accent": "#3b82f6",
    "accent_hi": "#2f6fe0",
    "accent_soft": "#e8f0fe",
    "green": "#16a34a",
    "green_soft": "#e7f6ec",
    "amber": "#e08c1a",
    "amber_soft": "#fdf2e0",
    "red": "#dc2626",
    "track": "#e7ebf3",
}

SIDEBAR_W = 224


def parse_size(text: str) -> int:
    """把 '100 MB' 这样的字符串转成字节数。"""
    number, unit = text.split()
    return int(float(number) * UNIT_TABLE[unit.upper()])


def parse_display_size(text: str) -> float:
    """把 '1,024.0 MB' 这类展示文本还原成字节数; 无法解析时返回 -1。"""
    try:
        number, unit = text.split()
        return float(number.replace(",", "")) * UNIT_TABLE[unit.upper()]
    except (ValueError, KeyError):
        return -1.0


# Excel 会把以这些字符开头的单元格当作公式执行(CSV 公式注入)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: str) -> str:
    """给可能被 Excel 解释为公式的文本加前导单引号。"""
    stripped = value.lstrip("\t\r\n ")
    if stripped and stripped[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


# --------------------------------------------------------------- 自绘基础件

def _round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
    """在画布上勾勒一个圆角矩形, 返回图元 id。"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class RoundButton(tk.Canvas):
    """圆角按钮, 支持实心/描边两种样式与悬停反馈。"""

    def __init__(self, parent, text, command, *, width=132, height=38, radius=10,
                 fill=C["accent"], hover=C["accent_hi"], text_color="#ffffff",
                 outline=None, hover_outline=None, font=(FONT, 10), bg=None,
                 padx=0, enabled=True):
        bg = bg or parent.cget("bg")
        super().__init__(parent, width=width, height=height, bd=0, highlightthickness=0,
                         bg=bg, cursor="hand2" if enabled else "arrow")
        self._command = command
        self._fill = fill
        self._hover = hover
        self._outline = outline
        self._hover_outline = hover_outline or outline
        self._text_color = text_color
        self._radius = radius
        self._font = font
        self._enabled = enabled
        self._hovering = False
        self._text = text
        self._cw, self._h = width, height
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        if not self._enabled:
            fill, outline, color = C["track"], None, C["faint"]
        else:
            hovering = self._hovering
            fill = self._hover if hovering else self._fill
            outline = self._hover_outline if hovering else self._outline
            color = self._text_color
        _round_rect(self, 1, 1, self._cw - 1, self._h - 1, self._radius, fill=fill, outline="")
        if outline:
            _round_rect(self, 1, 1, self._cw - 1, self._h - 1, self._radius,
                        fill="", outline=outline, width=1)
        self.create_text(self._cw / 2, self._h / 2, text=self._text, fill=color, font=self._font)

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _press(self, _event=None) -> None:
        if self._enabled and self._command:
            self._command()

    def _on_enter(self, _event=None) -> None:
        if self._enabled:
            self._hovering = True
            self._draw()

    def _on_leave(self, _event=None) -> None:
        if self._enabled:
            self._hovering = False
            self._draw()

    # 事件绑定在 __init__ 之后, 见 _attach
    def _attach(self) -> None:
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._press)


def Button(parent, text, command, **kwargs) -> RoundButton:
    """对外统一的按钮工厂(自动补上事件绑定)。"""
    btn = RoundButton(parent, text, command, **kwargs)
    btn._attach()
    return btn


class NavItem(tk.Canvas):
    """侧边栏导航项: 选中时高亮圆角底 + 左侧强调条。"""

    def __init__(self, parent, text, command, *, width=SIDEBAR_W - 28, height=44, radius=10):
        super().__init__(parent, width=width, height=height, bd=0, highlightthickness=0,
                         bg=C["sidebar"], cursor="hand2")
        self._text = text
        self._command = command
        self._cw, self._h, self._r = width, height, radius
        self._active = False
        self._hovering = False
        self._draw()
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self._command())

    def _set_hover(self, value: bool) -> None:
        self._hovering = value
        self._draw()

    def set_active(self, value: bool) -> None:
        self._active = value
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        if self._active:
            _round_rect(self, 0, 0, self._cw, self._h, self._r, fill=C["sidebar_hi"], outline="")
            _round_rect(self, 0, 8, 4, self._h - 8, 2, fill=C["accent"], outline="")
            color = C["sidebar_text_hi"]
        elif self._hovering:
            _round_rect(self, 0, 0, self._cw, self._h, self._r, fill=C["sidebar_hi"], outline="")
            color = C["sidebar_text_hi"]
        else:
            color = C["sidebar_text"]
        self.create_text(18, self._h / 2, text=self._text, fill=color,
                         font=(FONT, 10, "bold" if self._active else "normal"), anchor="w")


class Switch(tk.Canvas):
    """布尔开关, 绑定到 BooleanVar。"""

    def __init__(self, parent, variable: tk.BooleanVar, *, width=46, height=26, bg=None, command=None):
        bg = bg or parent.cget("bg")
        super().__init__(parent, width=width, height=height, bd=0, highlightthickness=0,
                         bg=bg, cursor="hand2")
        self.var = variable
        self._cw, self._h = width, height
        self._command = command
        self._draw()
        self.bind("<Button-1>", self._toggle)
        self.var.trace_add("write", lambda *_a: self._draw())

    def _toggle(self, _event=None) -> None:
        self.var.set(not self.var.get())
        if self._command:
            self._command()

    def _draw(self) -> None:
        self.delete("all")
        on = self.var.get()
        pad = 3
        track = C["accent"] if on else C["track"]
        _round_rect(self, 0, 0, self._cw, self._h, self._h / 2, fill=track, outline="")
        d = self._h - pad * 2
        x1 = self._cw - d - pad if on else pad
        knob = "#ffffff" if on else "#ffffff"
        _round_rect(self, x1, pad, x1 + d, pad + d, d / 2, fill=knob, outline=C["card_line"])


class ProgressPill(tk.Canvas):
    """细长圆角进度条; 支持不确定态(滑块循环)。"""

    def __init__(self, parent, *, width=200, height=6, bg=None):
        bg = bg or parent.cget("bg")
        super().__init__(parent, width=width, height=height, bd=0, highlightthickness=0, bg=bg)
        self._cw, self._h = width, height
        self._running = False
        self._pos = 0.0
        self._tick_id = None
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        _round_rect(self, 0, 0, self._cw, self._h, self._h / 2, fill=C["track"], outline="")
        if self._running:
            seg = self._cw * 0.32
            x = self._pos * (self._cw + seg) - seg
            x1, x2 = max(0, x), min(self._cw, x + seg)
            if x2 > x1:
                _round_rect(self, x1, 0, x2, self._h, self._h / 2, fill=C["accent"], outline="")

    def _tick(self) -> None:
        if not self._running:
            return
        self._pos = (self._pos + 0.02) % 1.0
        self._draw()
        self._tick_id = self.after(24, self._tick)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._tick_id:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self._draw()


class Card(tk.Frame):
    """白底细边卡片容器; 内容放入 card.body。"""

    def __init__(self, parent, *, padding=(18, 16), fill=None, bg=None):
        bg = bg or parent.cget("bg")
        fill = fill or C["card"]
        super().__init__(parent, bg=fill, highlightthickness=1,
                         highlightbackground=C["card_line"], highlightcolor=C["card_line"])
        self.body = tk.Frame(self, bg=fill)
        self.body.pack(fill="both", expand=True, padx=padding[0], pady=padding[1])


class ScrollArea(tk.Frame):
    """固定最大高度的纵向滚动容器, 内容放入 .inner。"""

    def __init__(self, parent, *, max_height=240, bg=None):
        bg = bg or parent.cget("bg")
        super().__init__(parent, bg=bg)
        self._max_height = max_height
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0, height=max_height)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._sync_region)
        self.canvas.bind("<Configure>", self._sync_width)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _sync_region(self, _event=None) -> None:
        bbox = self.canvas.bbox("all")
        if bbox:
            self.canvas.configure(scrollregion=bbox)

    def _sync_width(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def fit(self, rows: int, row_height: int = 44) -> None:
        """按内容行数调整可见高度; 超过上限时固定高度并滚动。"""
        self.canvas.configure(height=max(60, min(self._max_height, rows * row_height)))
        self._sync_region()

    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")


class Bar(tk.Canvas):
    """静态度量条(用于磁盘占用展示)。"""

    def __init__(self, parent, *, width=300, height=10, bg=None, color=None):
        bg = bg or parent.cget("bg")
        super().__init__(parent, width=width, height=height, bd=0, highlightthickness=0, bg=bg)
        self._cw, self._h = width, height
        self._color = color or C["accent"]
        self._ratio = 0.0
        self._draw()

    def set_color(self, color: str) -> None:
        self._color = color
        self._draw()

    def set_ratio(self, ratio: float) -> None:
        self._ratio = max(0.0, min(1.0, ratio))
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        _round_rect(self, 0, 0, self._cw, self._h, self._h / 2, fill=C["track"], outline="")
        if self._ratio > 0:
            fill_w = max(self._h, self._cw * self._ratio)
            _round_rect(self, 0, 0, fill_w, self._h, self._h / 2, fill=self._color, outline="")


class Badge(tk.Canvas):
    """状态徽标(圆角药丸)。"""

    def __init__(self, parent, text, *, fg, bg, dot=None, width=None, height=26, font=(FONT, 9)):
        super().__init__(parent, width=width or 10, height=height, bd=0, highlightthickness=0,
                         bg=parent.cget("bg"))
        self._text = text
        self._fg = fg
        self._bg = bg
        self._dot = dot
        self._font = font
        self._h = height
        self.set(text, fg, bg, dot)

    def set(self, text, fg, bg, dot=None) -> None:
        self._text, self._fg, self._bg, self._dot = text, fg, bg, dot
        self.delete("all")
        item = self.create_text(0, -100, text=text, font=self._font, anchor="nw")
        x1, y1, x2, _y2 = self.bbox(item)
        tw = x2 - x1
        pad = 12
        dot_space = 14 if dot else 0
        total = tw + pad * 2 + dot_space
        self.configure(width=total, height=self._h)
        _round_rect(self, 0, 0, total, self._h, self._h / 2, fill=bg, outline="")
        tx = pad + dot_space
        if dot:
            self.create_oval(pad - 2, self._h / 2 - 3, pad + 4, self._h / 2 + 3, fill=dot, outline="")
        self.create_text(tx, self._h / 2, text=text, font=self._font, fill=fg, anchor="w")


# --------------------------------------------------------------- 主应用

SECTION_META = {
    "overview": ("概览", "一眼看清磁盘占用, 找到可以释放的空间"),
    "temp": ("临时文件与缓存", "勾选清理位置, 先扫描再清理, 释放系统垃圾"),
    "recycle": ("回收站", "回收站中的文件仍占用磁盘空间, 可一键清空"),
    "large": ("大文件", "按体积找出占地方的大文件, 删除时进入回收站"),
    "dup": ("重复文件", "比对文件内容, 找出完全相同的多份副本"),
}

LEGEND_TEXT = (
    "受保护的项不会被删除: 「占用中」= 正被程序使用(含已加载的程序模块); "
    "「程序文件 / 数据文件 / 虚拟机磁盘」= 属于系统、已安装程序或重要数据。"
    "「安装包 / 日志文件 / 备份文件 / 镜像 / 邮件缓存」= 可以删除, 但建议先确认; "
    "「未占用」= 可以删除。"
)


class CleanerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ui_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.drives = core.list_drives()
        self._busy = False
        self.nav_items: dict[str, NavItem] = {}
        self.sections: dict[str, tk.Frame] = {}

        self.large_paths: dict[str, str] = {}
        self.dup_paths: dict[str, str] = {}
        self.dup_groups: list[core.DupGroup] = []
        self.dup_locked: set[str] = set()
        self.custom_dirs: list[str] = core.load_custom_dirs(include_missing=True)
        self._reclaim_cache: tuple[float, core.ScanResult] | None = None

        root.title("硬盘清理工具")
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        width = min(1180, screen_w - 60)
        height = min(780, screen_h - 90)
        left = max(0, (screen_w - width) // 2)
        top = max(0, (screen_h - height) // 3)
        root.geometry(f"{width}x{height}+{left}+{top}")
        root.minsize(min(1020, width), min(640, height))
        root.configure(bg=C["bg"])

        self._init_style()

        shell = tk.Frame(root, bg=C["bg"])
        shell.pack(fill="both", expand=True)
        self._build_sidebar(shell)
        self._build_main(shell)

        self.root.after(100, self._drain_queue)
        self.switch_section("overview")

    # ------------------------------------------------------------ 样式

    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("clam", "vista", "winnative"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure(".", font=(FONT, 10))
        style.configure("Treeview", font=(FONT, 10), rowheight=28, background=C["card"],
                        fieldbackground=C["card"], borderwidth=0, relief="flat")
        style.configure("Treeview.Heading", font=(FONT, 10, "bold"), background="#f0f3f9",
                        foreground=C["text"], relief="flat", borderwidth=0, padding=(8, 6))
        style.map("Treeview.Heading", background=[("active", "#e6ebf5")])
        style.map("Treeview", background=[("selected", C["accent_soft"])],
                  foreground=[("selected", C["text"])])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("TCombobox", padding=(8, 6), fieldbackground=C["card"],
                        background=C["card"], bordercolor=C["card_line"], arrowcolor=C["muted"])
        style.map("TCombobox", fieldbackground=[("readonly", C["card"])])

    # ------------------------------------------------------------ 侧边栏

    def _build_sidebar(self, parent: tk.Frame) -> None:
        side = tk.Frame(parent, bg=C["sidebar"], width=SIDEBAR_W)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        brand = tk.Frame(side, bg=C["sidebar"])
        brand.pack(fill="x", padx=18, pady=(22, 22))
        mark = tk.Canvas(brand, width=36, height=36, bg=C["sidebar"], highlightthickness=0)
        mark.pack(side="left")
        _round_rect(mark, 0, 0, 36, 36, 10, fill=C["accent"], outline="")
        mark.create_text(18, 18, text="清", fill="#ffffff", font=(FONT, 13, "bold"))
        title_box = tk.Frame(brand, bg=C["sidebar"])
        title_box.pack(side="left", padx=(10, 0))
        tk.Label(title_box, text="硬盘清理", bg=C["sidebar"], fg="#ffffff",
                 font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(title_box, text="Disk Cleaner", bg=C["sidebar"], fg=C["sidebar_text"],
                 font=(FONT, 8)).pack(anchor="w")

        tk.Label(side, text="功能", bg=C["sidebar"], fg="#5f6f88",
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=22, pady=(4, 6))

        for key in ("overview", "temp", "recycle", "large", "dup"):
            item = NavItem(side, SECTION_META[key][0], lambda k=key: self.switch_section(k))
            item.pack(padx=14, pady=3)
            self.nav_items[key] = item

        footer = tk.Frame(side, bg=C["sidebar"])
        footer.pack(side="bottom", fill="x", padx=18, pady=16)
        self.admin_badge = Badge(footer, "检测中", fg=C["sidebar_text"], bg=C["sidebar_hi"],
                                 dot=C["faint"], font=(FONT, 9))
        self.admin_badge.pack(anchor="w")
        tk.Label(footer, text="v1.1", bg=C["sidebar"], fg="#4c5a70",
                 font=(FONT, 8)).pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------ 主区域

    def _build_main(self, parent: tk.Frame) -> None:
        main = tk.Frame(parent, bg=C["bg"])
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=C["bg"])
        header.pack(fill="x", padx=26, pady=(24, 6))
        head_left = tk.Frame(header, bg=C["bg"])
        head_left.pack(side="left")
        self.title_label = tk.Label(head_left, text="", bg=C["bg"], fg=C["text"],
                                    font=(FONT, 19, "bold"))
        self.title_label.pack(anchor="w")
        self.subtitle_label = tk.Label(head_left, text="", bg=C["bg"], fg=C["muted"],
                                       font=(FONT, 10))
        self.subtitle_label.pack(anchor="w", pady=(2, 0))

        self.content = tk.Frame(main, bg=C["bg"])
        self.content.pack(fill="both", expand=True, padx=26, pady=(10, 12))

        self._build_statusbar(main)

        for key, builder in (
            ("overview", self._build_overview_tab),
            ("temp", self._build_temp_tab),
            ("recycle", self._build_recycle_tab),
            ("large", self._build_large_tab),
            ("dup", self._build_dup_tab),
        ):
            frame = tk.Frame(self.content, bg=C["bg"])
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            builder(frame)
            self.sections[key] = frame

        self._refresh_admin_badge()

    def _build_statusbar(self, main: tk.Frame) -> None:
        bar = tk.Frame(main, bg=C["card"], highlightthickness=1,
                       highlightbackground=C["card_line"])
        bar.pack(fill="x", side="bottom")
        inner = tk.Frame(bar, bg=C["card"])
        inner.pack(fill="x", padx=26, pady=10)
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(inner, textvariable=self.status_var, bg=C["card"], fg=C["muted"],
                 font=(FONT, 9)).pack(side="left")
        self.cancel_button = Button(inner, "取消", self.request_cancel, width=76, height=30,
                                    fill="#eef1f6", hover="#e3e8f0", text_color=C["text"],
                                    font=(FONT, 9), bg=C["card"], enabled=False)
        self.cancel_button.pack(side="right")
        self.progress = ProgressPill(inner, width=180, height=6, bg=C["card"])
        self.progress.pack(side="right", padx=14)

    def _refresh_admin_badge(self) -> None:
        if core.is_admin():
            self.admin_badge.set("管理员模式", fg="#8ee6a8", bg="#1c3a2a", dot="#22c55e")
        else:
            self.admin_badge.set("普通权限", fg="#f0c07a", bg="#3a2e18", dot="#f59e0b")

    def switch_section(self, key: str) -> None:
        title, subtitle = SECTION_META[key]
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=subtitle)
        for name, item in self.nav_items.items():
            item.set_active(name == key)
        self.sections[key].tkraise()
        if key == "overview":
            self.refresh_overview()

    # ------------------------------------------------------------ 概览页

    def _build_overview_tab(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x")
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, minsize=300)

        drive_card = Card(row, padding=(20, 16))
        drive_card.grid(row=0, column=0, sticky="new")
        tk.Label(drive_card.body, text="磁盘占用", bg=C["card"], fg=C["text"],
                 font=(FONT, 12, "bold")).pack(anchor="w")
        self.drive_box = tk.Frame(drive_card.body, bg=C["card"])
        self.drive_box.pack(fill="x", pady=(12, 0))

        side_col = tk.Frame(row, bg=C["bg"])
        side_col.grid(row=0, column=1, sticky="new", padx=(16, 0))

        recycle_card = Card(side_col, padding=(20, 16))
        recycle_card.pack(fill="x")
        tk.Label(recycle_card.body, text="回收站", bg=C["card"], fg=C["muted"],
                 font=(FONT, 10)).pack(anchor="w")
        self.ov_recycle = tk.Label(recycle_card.body, text="—", bg=C["card"], fg=C["text"],
                                   font=(FONT, 20, "bold"))
        self.ov_recycle.pack(anchor="w", pady=(4, 2))
        self.ov_recycle_sub = tk.Label(recycle_card.body, text="正在读取…", bg=C["card"],
                                       fg=C["muted"], font=(FONT, 9))
        self.ov_recycle_sub.pack(anchor="w")

        quick_card = Card(side_col, padding=(20, 16), bg=C["bg"])
        quick_card.pack(fill="x", pady=(16, 0))
        tk.Label(quick_card.body, text="快捷操作", bg=C["card"], fg=C["text"],
                 font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, 10))
        Button(quick_card.body, "扫描临时文件", lambda: self.switch_section("temp"),
               fill=C["accent"], width=200, bg=C["card"]).pack(anchor="w", pady=3)
        Button(quick_card.body, "查找大文件", lambda: self.switch_section("large"),
               fill="#eef1f6", hover="#e3e8f0", text_color=C["text"], width=200,
               bg=C["card"]).pack(anchor="w", pady=3)
        Button(quick_card.body, "查找重复文件", lambda: self.switch_section("dup"),
               fill="#eef1f6", hover="#e3e8f0", text_color=C["text"], width=200,
               bg=C["card"]).pack(anchor="w", pady=3)

        self.overview_hint = tk.Label(parent, text="", bg=C["bg"], fg=C["muted"],
                                      font=(FONT, 9), justify="left", wraplength=820)
        self.overview_hint.pack(anchor="w", pady=(14, 0))

    def refresh_overview(self) -> None:
        for child in self.drive_box.winfo_children():
            child.destroy()
        for drive in self.drives:
            try:
                usage = shutil.disk_usage(drive)
            except OSError:
                continue
            used_ratio = usage.used / usage.total if usage.total else 0
            if used_ratio > 0.9:
                color = C["red"]
            elif used_ratio > 0.75:
                color = C["amber"]
            else:
                color = C["accent"]
            line = tk.Frame(self.drive_box, bg=C["card"])
            line.pack(fill="x", pady=(0, 12))
            top = tk.Frame(line, bg=C["card"])
            top.pack(fill="x")
            tk.Label(top, text=drive.rstrip("\\"), bg=C["card"], fg=C["text"],
                     font=(FONT, 11, "bold")).pack(side="left")
            tk.Label(top, text=f"剩余 {core.human_size(usage.free)} / 共 {core.human_size(usage.total)}",
                     bg=C["card"], fg=C["muted"], font=(FONT, 9)).pack(side="right")
            bar = Bar(line, width=520, height=10, bg=C["card"], color=color)
            bar.pack(fill="x", pady=(6, 0))
            bar.bind("<Configure>", lambda e, b=bar, r=used_ratio: self._resize_bar(b, e, r))
        self.refresh_recycle_bin(overview=True)
        self._compute_reclaimable()

    def _resize_bar(self, bar: Bar, event, ratio: float) -> None:
        bar._cw = event.width
        bar.set_ratio(ratio)

    def _compute_reclaimable(self, force: bool = False) -> None:
        """统计临时文件占用量; 一分钟内复用上次结果, 避免每次切页都全盘重扫。"""
        if not force and self._reclaim_cache is not None:
            age = time.monotonic() - self._reclaim_cache[0]
            if age < 60:
                self._show_reclaimable(self._reclaim_cache[1])
                return
        if self._busy:
            # 已有任务在跑(例如正在扫描大文件), 稍后再统计, 避免两个任务互相干扰
            self.root.after(800, lambda: self._compute_reclaimable(force))
            return

        def work(cancel, report):
            targets = core.default_targets()
            return core.scan_targets(targets, cancel=cancel, progress=report)

        def done(result: core.ScanResult) -> None:
            self._reclaim_cache = (time.monotonic(), result)
            self._show_reclaimable(result)

        self.run_task(work, done)

    def _show_reclaimable(self, result: core.ScanResult) -> None:
        self.overview_hint.configure(
            text=f"临时文件与缓存约占用 {core.human_size(result.total_bytes)} "
                 f"({result.file_count} 个文件), 清理后可释放这部分空间。")

    # ------------------------------------------------------ 通用小工具

    def _make_tree(self, parent: tk.Widget, columns: list[tuple[str, str, int, str]],
                   *, sortable: bool = True) -> ttk.Treeview:
        frame = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                         highlightbackground=C["card_line"])
        frame.pack(fill="both", expand=True, pady=(12, 0))
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                            selectmode="extended")
        for key, title, width, anchor in columns:
            if sortable:
                tree.heading(key, text=title,
                             command=lambda c=key, t=tree: self._sort_tree(t, c))
            else:
                tree.heading(key, text=title)
            tree.column(key, width=width, anchor=anchor, stretch=(anchor == "w"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scroll.pack(side="right", fill="y", pady=6)
        tree.tag_configure("odd", background="#fafbfe")
        tree.tag_configure("group", background=C["accent_soft"], font=(FONT, 10, "bold"))
        tree.tag_configure("inuse", foreground=C["red"])
        tree.tag_configure("risk", foreground=C["amber"])
        tree.tag_configure("notice", foreground="#0e7490")
        tree._sort_state = {}
        return tree

    @staticmethod
    def _sort_tree(tree: ttk.Treeview, column: str) -> None:
        """点击列头排序: 首次升序, 再次点击改为降序。"""
        state = getattr(tree, "_sort_state", {})
        reverse = state.get(column, False)
        state[column] = not reverse
        tree._sort_state = state

        def sort_key(iid: str):
            value = tree.set(iid, column)
            if column == "size":
                return parse_display_size(value)
            if column == "count":
                try:
                    return int(value)
                except ValueError:
                    return 0
            return value.lower()

        children = list(tree.get_children(""))
        children.sort(key=sort_key, reverse=reverse)
        for index, iid in enumerate(children):
            tree.move(iid, "", index)

    def _export_tree(self, tree: ttk.Treeview, name: str) -> None:
        """把表格当前内容导出为 CSV(UTF-8 带 BOM, Excel 可直接打开)。"""
        children = tree.get_children("")
        if not children:
            messagebox.showinfo("提示", "当前没有可导出的数据, 请先扫描。")
            return
        path = filedialog.asksaveasfilename(
            title="导出为 CSV",
            defaultextension=".csv",
            initialfile=f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        columns = list(tree["columns"])
        headers = [tree.heading(column)["text"] for column in columns]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                for iid in children:
                    writer.writerow([csv_cell(tree.set(iid, column)) for column in columns])
                    for child in tree.get_children(iid):
                        writer.writerow([csv_cell(tree.set(child, column)) for column in columns])
        except OSError as exc:
            messagebox.showerror("导出失败", f"写入文件时出错:\n{exc}")
            return
        self.set_status(f"已导出 {path}")

    @staticmethod
    def _row_status(index: int, path: str, locked: set[str],
                    with_risk: bool = True) -> tuple[str, tuple[str, ...]]:
        """返回 (删除提示, 标签元组)。

        优先级: 占用中 > 禁止删除的风险 > 建议确认 > 未占用。
        占用/风险行标红或琥珀色, 提示行标青色(仍可删除)。
        """
        tags = ["odd"] if index % 2 else []
        if path in locked:
            return "占用中", tuple(tags + ["inuse"])
        if with_risk:
            kind = core.deletion_risk(path)
            if kind:
                return kind, tuple(tags + ["risk"])
            notice = core.deletion_notice(path)
            if notice:
                return notice, tuple(tags + ["notice"])
        return "未占用", tuple(tags)

    def _field_row(self, parent, label, values, variable, width=10) -> None:
        tk.Label(parent, text=label, bg=C["card"], fg=C["muted"], font=(FONT, 9)).pack(side="left")
        combo = ttk.Combobox(parent, textvariable=variable, values=values, width=width,
                             state="readonly", font=(FONT, 10))
        combo.pack(side="left", padx=(6, 18))

    def _summary_label(self, parent) -> tk.Label:
        lbl = tk.Label(parent, text="尚未扫描", bg=C["bg"], fg=C["muted"], font=(FONT, 9),
                       justify="left", wraplength=820)
        lbl.pack(anchor="w", pady=(10, 0))
        return lbl

    # -------------------------------------------------------- 临时文件页

    def _build_temp_tab(self, parent: tk.Frame) -> None:
        card = Card(parent, padding=(20, 16))
        card.pack(fill="x")
        head = tk.Frame(card.body, bg=C["card"])
        head.pack(fill="x")
        tk.Label(head, text="清理位置", bg=C["card"], fg=C["text"],
                 font=(FONT, 12, "bold")).pack(side="left")
        Button(head, "添加目录", self.add_custom_dir, width=96, height=30,
               fill="#eef1f6", hover="#e3e8f0", text_color=C["text"],
               font=(FONT, 9), bg=C["card"]).pack(side="right")
        tk.Label(card.body, text="勾选需要清理的位置; 临时文件按系统惯例直接删除, 不会进入回收站。"
                                "列表中的「占用中」表示文件正被程序使用(含已加载的程序模块), 清理时会被保留、不删除。",
                 bg=C["card"], fg=C["muted"], font=(FONT, 9), justify="left",
                 wraplength=860).pack(anchor="w", pady=(2, 10))

        self.temp_scroll = ScrollArea(card.body, max_height=200, bg=C["card"])
        self.temp_scroll.pack(fill="x")
        self.temp_list = self.temp_scroll.inner
        self.temp_vars: dict[str, tk.BooleanVar] = {}
        self.temp_targets: list[core.CleanTarget] = []
        self._reload_temp_targets()

        actions = tk.Frame(parent, bg=C["bg"])
        actions.pack(fill="x", pady=(14, 0))
        Button(actions, "扫描", self.scan_temp, fill=C["accent"], width=110).pack(side="left")
        Button(actions, "清理选中项", self.clean_temp, fill="#eef1f6", hover="#e3e8f0",
               text_color=C["text"], width=130).pack(side="left", padx=8)
        Button(actions, "导出 CSV", lambda: self._export_tree(self.temp_tree, "临时文件"),
               fill="#eef1f6", hover="#e3e8f0", text_color=C["text"], width=100).pack(side="left")

        self.temp_summary = self._summary_label(parent)
        self.temp_tree = self._make_tree(
            parent,
            [("state", "删除提示", 90, "center"), ("size", "大小", 110, "e"),
             ("name", "文件", 290, "w"), ("path", "所在目录", 480, "w")],
        )

    def _reload_temp_targets(self) -> None:
        """按内置目标 + 自定义目录重建列表, 尽量保留用户已做的勾选。"""
        previous = {key: var.get() for key, var in self.temp_vars.items()}
        for child in self.temp_list.winfo_children():
            child.destroy()
        self.temp_targets = core.default_targets() + core.custom_targets(self.custom_dirs)
        self.temp_vars = {}
        for target in self.temp_targets:
            var = tk.BooleanVar(value=previous.get(target.key, target.enabled))
            self.temp_vars[target.key] = var
            self._render_target_row(target, var)
        if not self.temp_targets:
            tk.Label(self.temp_list, text="未找到可清理的默认位置。", bg=C["card"],
                     fg=C["muted"]).pack(anchor="w")
        else:
            self.temp_scroll.fit(len(self.temp_targets))

    def _render_target_row(self, target: core.CleanTarget, var: tk.BooleanVar) -> None:
        row = tk.Frame(self.temp_list, bg=C["card"])
        row.pack(fill="x", pady=3)
        Switch(row, var, bg=C["card"]).pack(side="left")
        if target.custom:
            remove = tk.Label(row, text="移除", bg=C["card"], fg=C["faint"],
                              font=(FONT, 9), cursor="hand2")
            remove.pack(side="right", padx=(12, 0))
            remove.bind("<Button-1>", lambda _e, p=target.paths[0]: self.remove_custom_dir(p))
        text = tk.Frame(row, bg=C["card"])
        text.pack(side="left", fill="x", expand=True, padx=(10, 0))
        tk.Label(text, text=target.label, bg=C["card"], fg=C["text"],
                 font=(FONT, 10, "bold")).pack(anchor="w")
        tk.Label(text, text=target.note, bg=C["card"], fg=C["muted"], font=(FONT, 9),
                 justify="left", wraplength=720, anchor="w").pack(anchor="w", fill="x")

    def add_custom_dir(self) -> None:
        path = filedialog.askdirectory(title="选择要清理的目录")
        if not path:
            return
        reason = core.is_dangerous_dir(path)
        if reason:
            messagebox.showwarning(
                "无法添加",
                f"{reason}。\n\n请选择更具体的子目录, 例如某个软件的缓存目录。")
            return
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in {os.path.normcase(os.path.abspath(p)) for p in self.custom_dirs}:
            messagebox.showinfo("提示", "该目录已在清理列表中。")
            return
        if not messagebox.askyesno(
            "确认添加",
            f"将把以下目录加入清理列表:\n{path}\n\n"
            "清理时该目录下的所有文件都会被直接删除(子目录本身保留), 不会进入回收站。\n"
            "请确认目录中不含还需要保留的文件。确定添加吗?",
        ):
            return
        self.custom_dirs.append(path)
        if not core.save_custom_dirs(self.custom_dirs):
            messagebox.showwarning("提示", "设置保存失败, 本次添加仅在当前会话有效。")
        self._reload_temp_targets()
        self.set_status(f"已添加清理目录: {path}")

    def remove_custom_dir(self, path: str) -> None:
        normalized = os.path.normcase(os.path.abspath(path))
        self.custom_dirs = [p for p in self.custom_dirs
                            if os.path.normcase(os.path.abspath(p)) != normalized]
        if not core.save_custom_dirs(self.custom_dirs):
            messagebox.showwarning("提示", "设置保存失败, 移除操作仅在当前会话有效。")
        self._reload_temp_targets()
        self.set_status(f"已移除清理目录: {path}")

    def _selected_targets(self) -> list[core.CleanTarget]:
        return [t for t in self.temp_targets if self.temp_vars[t.key].get()]

    def scan_temp(self) -> None:
        targets = self._selected_targets()
        if not targets:
            messagebox.showinfo("提示", "请至少勾选一个清理位置。")
            return

        def work(cancel, report):
            return core.scan_targets(targets, cancel=cancel, progress=report)

        def done(result: core.ScanResult) -> None:
            self.temp_tree.delete(*self.temp_tree.get_children())
            for index, (path, size) in enumerate(result.top_files):
                state, tags = self._row_status(index, path, result.locked_paths, with_risk=False)
                self.temp_tree.insert("", "end", tags=tags, values=(
                    state, core.human_size(size), os.path.basename(path), os.path.dirname(path)))
            if result.locked_count:
                locked = (f"其中 {result.locked_count} 个较大文件正被程序占用"
                          f"({core.human_size(result.locked_bytes)}), 清理时会被自动跳过, 不影响正在运行的程序; "
                          f"预计可释放 {core.human_size(result.deletable_bytes)}。")
            else:
                locked = f"未发现被占用的较大文件, 预计可释放 {core.human_size(result.deletable_bytes)}。"
            self.temp_summary.configure(
                text=f"共 {result.file_count} 个文件, 占用 {core.human_size(result.total_bytes)}; "
                     f"{locked}下表为其中最大的 {len(result.top_files)} 个。")
            self.set_status("扫描完成")

        self.run_task(work, done)

    def clean_temp(self) -> None:
        targets = self._selected_targets()
        if not targets:
            messagebox.showinfo("提示", "请至少勾选一个清理位置。")
            return
        custom = [t for t in targets if t.custom]
        warning = (
            "\n\n注意: 以下自定义目录中的所有文件都会被删除:\n"
            + "\n".join(t.paths[0] for t in custom)
        ) if custom else ""
        if not messagebox.askyesno(
            "确认清理",
            "将直接删除选中位置中的临时文件(不进入回收站)。\n\n"
            "正被程序使用的文件会被自动保留、不会删除, 因此不会影响正在运行的程序。"
            f"{warning}\n\n确定继续吗?",
        ):
            return

        def work(cancel, report):
            return core.clean_targets(targets, cancel=cancel, progress=report)

        def done(result: core.CleanResult) -> None:
            text = f"清理完成: 删除 {result.deleted} 个文件, 释放 {core.human_size(result.freed_bytes)}。"
            if result.blocked:
                text += f" 为安全起见保留 {result.blocked} 个正在被程序使用的文件(未尝试删除)。"
            if result.skipped:
                text += f" 另有 {result.skipped} 个文件未能删除(可能被占用或权限不足)。"
            delta, disks = core.free_space_delta(result.free_before)
            if disks and delta > 0:
                text += f" 磁盘可用空间实际增加 {core.human_size(delta)}。"
            self.temp_summary.configure(text=text)
            self.set_status("清理完成")
            self.temp_tree.delete(*self.temp_tree.get_children())
            self._reclaim_cache = None

        self.run_task(work, done)

    # ---------------------------------------------------------- 回收站页

    def _build_recycle_tab(self, parent: tk.Frame) -> None:
        card = Card(parent, padding=(24, 22))
        card.pack(fill="x")
        tk.Label(card.body, text="回收站状态", bg=C["card"], fg=C["muted"],
                 font=(FONT, 10)).pack(anchor="w")
        self.recycle_var = tk.StringVar(value="正在读取…")
        tk.Label(card.body, textvariable=self.recycle_var, bg=C["card"], fg=C["text"],
                 font=(FONT, 22, "bold")).pack(anchor="w", pady=(6, 0))
        tk.Label(card.body, text="清空后无法恢复, 请确认其中没有还需要还原的文件。",
                 bg=C["card"], fg=C["muted"], font=(FONT, 9)).pack(anchor="w", pady=(8, 0))

        actions = tk.Frame(parent, bg=C["bg"])
        actions.pack(fill="x", pady=(14, 0))
        Button(actions, "刷新", self.refresh_recycle_bin, fill="#eef1f6", hover="#e3e8f0",
               text_color=C["text"], width=110).pack(side="left")
        Button(actions, "清空回收站", self.empty_recycle, fill=C["red"], hover="#c11f1f",
               width=130).pack(side="left", padx=8)

    def refresh_recycle_bin(self, overview: bool = False) -> None:
        def work(cancel, report):
            return core.recycle_bin_info()

        def done(result: tuple[int, int]) -> None:
            size, count = result
            self.recycle_var.set(
                f"{core.human_size(size)}  ·  {count} 个文件/文件夹")
            if hasattr(self, "ov_recycle"):
                self.ov_recycle.configure(text=core.human_size(size))
                self.ov_recycle_sub.configure(text=f"{count} 个文件/文件夹")

        self.run_task(work, done, silent=overview)

    def empty_recycle(self) -> None:
        if not messagebox.askyesno("确认清空", "清空回收站后, 其中所有已删除文件将无法还原。\n\n确定继续吗?"):
            return

        def work(cancel, report):
            return core.empty_recycle_bin()

        def done(result: tuple[bool, str]) -> None:
            ok, message = result
            self.refresh_recycle_bin()
            if ok:
                messagebox.showinfo("回收站", message)
            else:
                messagebox.showwarning("回收站", message)

        self.run_task(work, done)

    # ---------------------------------------------------------- 大文件页

    def _build_large_tab(self, parent: tk.Frame) -> None:
        actions = tk.Frame(parent, bg=C["bg"])
        actions.pack(fill="x")
        self.large_drive = tk.StringVar(value=self.drives[0])
        self.large_size = tk.StringVar(value="100 MB")
        self._field_row(actions, "扫描位置", self.drives, self.large_drive, width=8)
        self._field_row(actions, "最小体积", ["10 MB", "50 MB", "100 MB", "500 MB", "1 GB", "5 GB"],
                        self.large_size, width=8)
        Button(actions, "开始扫描", self.scan_large, fill=C["accent"], width=110).pack(side="left")
        Button(actions, "删除选中(移入回收站)", self.delete_selected_large, fill="#eef1f6",
               hover="#e3e8f0", text_color=C["text"], width=180).pack(side="left", padx=8)
        Button(actions, "导出 CSV", lambda: self._export_tree(self.large_tree, "大文件"),
               fill="#eef1f6", hover="#e3e8f0", text_color=C["text"], width=96).pack(side="left")

        tk.Label(parent, text=LEGEND_TEXT, bg=C["bg"], fg=C["faint"], font=(FONT, 9),
                 wraplength=900, justify="left").pack(anchor="w", pady=(8, 0))

        self.large_summary = self._summary_label(parent)
        self.large_tree = self._make_tree(
            parent,
            [("state", "删除提示", 90, "center"), ("size", "大小", 110, "e"),
             ("name", "文件", 290, "w"), ("path", "所在目录", 480, "w")],
        )

    def scan_large(self) -> None:
        root = self.large_drive.get()
        min_size = parse_size(self.large_size.get())

        def work(cancel, report):
            files, scanned = core.scan_large_files(root, min_size=min_size, cancel=cancel, progress=report)
            report("正在检查文件占用情况…", 0, 0)
            locked = core.check_in_use([path for path, _size in files])
            # 预热风险识别所需的安装目录, 避免界面渲染时再读取注册表/快捷方式/进程模块
            core.installed_program_dirs()
            core.running_program_dirs()
            return files, scanned, locked

        def done(result: tuple[list[tuple[str, int]], int, set[str]]) -> None:
            files, scanned, locked = result
            self.large_tree.delete(*self.large_tree.get_children())
            self.large_paths.clear()
            for index, (path, size) in enumerate(files):
                iid = f"f{index}"
                self.large_paths[iid] = path
                state, tags = self._row_status(index, path, locked)
                self.large_tree.insert("", "end", iid=iid, tags=tags, values=(
                    state, core.human_size(size), os.path.basename(path), os.path.dirname(path)))
            total = sum(size for _path, size in files)
            protected = 0
            notices = 0
            for path, _size in files:
                if path in locked or core.deletion_risk(path):
                    protected += 1
                elif core.deletion_notice(path):
                    notices += 1
            extra = (f" 其中 {protected} 个受保护(正被使用或属系统/程序文件), 不会被删除。"
                     if protected else " 未发现受保护的文件。")
            if notices:
                extra += f" 另有 {notices} 个安装包/日志/备份等文件, 删除前建议确认。"
            self.large_summary.configure(
                text=f"扫描 {scanned} 个文件, 找到 {len(files)} 个大于 {self.large_size.get()} 的文件, "
                     f"合计 {core.human_size(total)}。{extra}")
            self.set_status("扫描完成")

        self.run_task(work, done)

    def delete_selected_large(self) -> None:
        iids = self.large_tree.selection()
        if not iids:
            messagebox.showinfo("提示", "请先在列表中选择要删除的文件。")
            return
        paths = [self.large_paths[iid] for iid in iids if iid in self.large_paths]
        allowed, blocked = self._split_protected(paths)
        if not allowed:
            messagebox.showwarning(
                "已禁止删除",
                f"选中的 {len(paths)} 个文件全部受保护, 一个都不会删除:\n"
                f"{self._blocked_summary(blocked)}\n\n"
                "受保护 = 正在被程序使用, 或属于系统 / 已安装程序的文件。",
            )
            return
        total = core.total_size(allowed)
        blocked_note = (f"\n其中 {len(blocked)} 个受保护({self._blocked_summary(blocked)}), 已排除、不会删除。"
                        if blocked else "")
        notice_note = self._notice_note(allowed)
        if not messagebox.askyesno(
            "确认删除",
            f"将把 {len(allowed)} 个文件(共 {core.human_size(total)})移入回收站。"
            f"{blocked_note}{notice_note}\n\n之后仍可从回收站还原。确定继续吗?",
        ):
            return
        self._move_to_recycle(allowed, "大文件", self._prune_large_tree)

    def _prune_large_tree(self, failed: list[str]) -> None:
        """移除确实已消失的行。

        因受保护而被跳过的文件仍然存在, 必须继续显示, 否则用户会误以为已删除。
        """
        for iid, path in list(self.large_paths.items()):
            if os.path.exists(path):
                continue
            if self.large_tree.exists(iid):
                self.large_tree.delete(iid)
            del self.large_paths[iid]
        remaining = list(self.large_paths.values())
        total = core.total_size(remaining)
        self.large_summary.configure(
            text=f"剩余 {len(remaining)} 个文件, 合计 {core.human_size(total)}。")

    # -------------------------------------------------------- 重复文件页

    def _build_dup_tab(self, parent: tk.Frame) -> None:
        actions = tk.Frame(parent, bg=C["bg"])
        actions.pack(fill="x")
        self.dup_drive = tk.StringVar(value=self.drives[0])
        self.dup_size = tk.StringVar(value="10 MB")
        self._field_row(actions, "扫描位置", self.drives, self.dup_drive, width=8)
        self._field_row(actions, "最小体积", ["1 MB", "10 MB", "50 MB", "100 MB"],
                        self.dup_size, width=8)
        Button(actions, "开始查找", self.scan_dups, fill=C["accent"], width=110).pack(side="left")
        Button(actions, "每组保留一个", self.delete_dup_extras, fill="#eef1f6", hover="#e3e8f0",
               text_color=C["text"], width=120).pack(side="left", padx=8)
        Button(actions, "删除选中项", self.delete_selected_dups, fill="#eef1f6", hover="#e3e8f0",
               text_color=C["text"], width=110).pack(side="left")
        Button(actions, "导出 CSV", lambda: self._export_tree(self.dup_tree, "重复文件"),
               fill="#eef1f6", hover="#e3e8f0", text_color=C["text"], width=92).pack(side="left", padx=8)

        tk.Label(parent, text="按文件内容比对。'每组保留一个'会保留每组中排在最前的一份, 其余移入回收站。",
                 bg=C["bg"], fg=C["faint"], font=(FONT, 9)).pack(anchor="w", pady=(8, 0))
        tk.Label(parent, text=LEGEND_TEXT, bg=C["bg"], fg=C["faint"], font=(FONT, 9),
                 wraplength=900, justify="left").pack(anchor="w", pady=(2, 0))

        self.dup_summary = self._summary_label(parent)
        self.dup_tree = self._make_tree(
            parent,
            [("state", "删除提示", 90, "center"), ("size", "单份大小", 100, "e"),
             ("count", "份数", 60, "e"), ("name", "文件", 280, "w"),
             ("path", "所在目录", 420, "w")],
            sortable=False,
        )

    def scan_dups(self) -> None:
        root = self.dup_drive.get()
        min_size = parse_size(self.dup_size.get())

        def work(cancel, report):
            groups = core.find_duplicates(root, min_size=min_size, cancel=cancel, progress=report)
            report("正在检查文件占用情况…", 0, 0)
            locked = core.check_in_use([path for group in groups for path in group.files])
            core.installed_program_dirs()
            core.running_program_dirs()
            return groups, locked

        def done(result: tuple[list[core.DupGroup], set[str]]) -> None:
            groups, locked = result
            self.dup_groups = groups
            self.dup_locked = locked
            self._render_dup_groups()
            if groups:
                wasted = sum(group.wasted_bytes for group in groups)
                summary = (f"发现 {len(groups)} 组重复文件, 每组只保留一份可释放约 "
                           f"{core.human_size(wasted)}。")
                protected = 0
                notices = 0
                for group in groups:
                    for path in group.files:
                        if path in locked or core.deletion_risk(path):
                            protected += 1
                        elif core.deletion_notice(path):
                            notices += 1
                if protected:
                    summary += f" 其中 {protected} 个受保护, 不会被删除。"
                if notices:
                    summary += f" 另有 {notices} 个文件属于安装包/日志等类型, 删除前建议确认。"
            else:
                summary = "未发现重复文件。"
            self.dup_summary.configure(text=summary)
            self.set_status("查找完成")

        self.run_task(work, done)

    def _render_dup_groups(self) -> None:
        """按 self.dup_groups 重建分组表格。"""
        self.dup_tree.delete(*self.dup_tree.get_children())
        self.dup_paths.clear()
        for group_index, group in enumerate(self.dup_groups):
            parent_iid = self.dup_tree.insert(
                "", "end", tags=("group",), open=False,
                values=("", core.human_size(group.size), len(group.files),
                        f"重复 {len(group.files)} 份", ""),
            )
            for file_index, path in enumerate(group.files):
                iid = f"{group_index}-{file_index}"
                self.dup_paths[iid] = path
                state, tags = self._row_status(file_index, path, self.dup_locked)
                self.dup_tree.insert(parent_iid, "end", iid=iid, tags=tags, values=(
                    state, core.human_size(group.size), "", os.path.basename(path),
                    os.path.dirname(path)))

    def delete_dup_extras(self) -> None:
        if not self.dup_groups:
            messagebox.showinfo("提示", "请先查找重复文件。")
            return
        extras = [path for group in self.dup_groups for path in group.files[1:]]
        if not extras:
            messagebox.showinfo("提示", "没有可删除的重复文件。")
            return
        allowed, blocked = self._split_protected(extras)
        if not allowed:
            messagebox.showwarning(
                "已禁止删除",
                f"待删除的 {len(extras)} 个重复文件全部受保护, 一个都不会删除:\n"
                f"{self._blocked_summary(blocked)}\n\n"
                "受保护 = 正在被程序使用, 或属于系统 / 已安装程序的文件。",
            )
            return
        total = core.total_size(allowed)
        blocked_note = (f"\n其中 {len(blocked)} 个受保护({self._blocked_summary(blocked)}), 已排除、不会删除。"
                        if blocked else "")
        notice_note = self._notice_note(allowed)
        if not messagebox.askyesno(
            "确认删除",
            f"将把 {len(allowed)} 个重复文件(共 {core.human_size(total)})移入回收站, 每组保留一份。"
            f"{blocked_note}{notice_note}\n\n确定继续吗?",
        ):
            return
        self._move_to_recycle(allowed, "重复文件", self._prune_dup_tree)

    def delete_selected_dups(self) -> None:
        iids = self.dup_tree.selection()
        paths = [self.dup_paths[iid] for iid in iids if iid in self.dup_paths]
        if not paths:
            messagebox.showinfo("提示", "请先展开分组并选择要删除的文件。")
            return
        allowed, blocked = self._split_protected(paths)
        if not allowed:
            messagebox.showwarning(
                "已禁止删除",
                f"选中的 {len(paths)} 个文件全部受保护, 一个都不会删除:\n"
                f"{self._blocked_summary(blocked)}\n\n"
                "受保护 = 正在被程序使用, 或属于系统 / 已安装程序的文件。",
            )
            return
        total = core.total_size(allowed)
        blocked_note = (f"\n其中 {len(blocked)} 个受保护({self._blocked_summary(blocked)}), 已排除、不会删除。"
                        if blocked else "")
        notice_note = self._notice_note(allowed)
        if not messagebox.askyesno(
            "确认删除",
            f"将把选中的 {len(allowed)} 个文件(共 {core.human_size(total)})移入回收站。"
            f"{blocked_note}{notice_note}\n\n确定继续吗?",
        ):
            return
        self._move_to_recycle(allowed, "重复文件", self._prune_dup_tree)

    def _prune_dup_tree(self, failed: list[str]) -> None:
        """移除确实已消失的文件; 受保护而被跳过的文件保留, 并同步分组信息。"""
        for group in self.dup_groups:
            group.files = [path for path in group.files if os.path.exists(path)]
        self.dup_groups = [group for group in self.dup_groups if len(group.files) > 1]
        self._render_dup_groups()
        wasted = sum(group.wasted_bytes for group in self.dup_groups)
        if self.dup_groups:
            self.dup_summary.configure(
                text=f"剩余 {len(self.dup_groups)} 组重复文件, 可释放约 "
                     f"{core.human_size(wasted)}。")
        else:
            self.dup_summary.configure(text="已没有重复文件。")

    # ------------------------------------------------------------ 任务调度

    def post(self, fn) -> None:
        self.ui_queue.put(fn)

    def _drain_queue(self) -> None:
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:  # 界面回调异常不应导致整个程序卡死
                print("界面回调异常:", exc)
        self.root.after(100, self._drain_queue)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def run_task(self, work, on_done, silent: bool = False) -> None:
        """在后台线程执行 work(cancel, report), 结果通过 on_done 回到主线程。

        silent=True 时不显示忙碌指示(用于后台的轻量查询)。
        """
        if not silent and self._busy:
            self.set_status("已有任务正在运行, 请等待完成或点击取消后再试。")
            return
        if not silent:
            self.cancel_event.clear()
            self._set_busy(True)

        def report(message: str, *_args) -> None:
            self.post(lambda: self.status_var.set(message))

        def target() -> None:
            try:
                result = work(self.cancel_event.is_set, report)
            except Exception as exc:
                self.post(lambda e=exc: self._show_error(e))
            else:
                self.post(lambda r=result: self._finish(r, on_done))
            finally:
                if not silent:
                    self.post(lambda: self._set_busy(False))

        threading.Thread(target=target, daemon=True).start()

    def _finish(self, result, on_done) -> None:
        try:
            on_done(result)
        except Exception as exc:
            self._show_error(exc)

    def _show_error(self, exc: Exception) -> None:
        self.set_status(f"出错: {exc}")
        messagebox.showerror("错误", f"操作过程中出现问题:\n{exc}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.progress.start()
            self.cancel_button.set_enabled(True)
        else:
            self.progress.stop()
            self.cancel_button.set_enabled(False)

    def request_cancel(self) -> None:
        if self._busy:
            self.cancel_event.set()
            self.set_status("正在取消, 请稍候…")

    @staticmethod
    def _split_protected(paths: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
        """把待删文件分成 (可删除列表, [(受保护路径, 原因)])。"""
        modules = core.loaded_module_paths()
        allowed: list[str] = []
        blocked: list[tuple[str, str]] = []
        for path in paths:
            reason = core.protection_reason(path, modules)
            if reason:
                blocked.append((path, reason))
            else:
                allowed.append(path)
        return allowed, blocked

    @staticmethod
    def _blocked_summary(blocked: list[tuple[str, str]]) -> str:
        """把受保护文件按原因归类成一句话, 例如 "正在被程序使用 2 个、程序文件 3 个"。"""
        counts: dict[str, int] = {}
        for _path, reason in blocked:
            counts[reason] = counts.get(reason, 0) + 1
        return "、".join(f"{reason} {count} 个" for reason, count in sorted(counts.items()))

    @staticmethod
    def _notice_note(paths: list[str]) -> str:
        """把"可删但需确认"的文件归类成一段提示, 无此类文件时返回空串。"""
        counts: dict[str, int] = {}
        for path in paths:
            kind = core.deletion_notice(path)
            if kind:
                counts[kind] = counts.get(kind, 0) + 1
        if not counts:
            return ""
        summary = "、".join(f"{kind} {count} 个" for kind, count in sorted(counts.items()))
        return f"\n其中包含 {summary}, 删除前请确认仍需要保留。"

    def _move_to_recycle(self, paths: list[str], label: str, on_done=None) -> None:
        def work(cancel, report):
            return core.move_to_recycle_bin(paths)

        def done(result: tuple[int, int, list[str]]) -> None:
            moved, failed, failed_paths = result
            message = f"{label}: 已删除 {moved} 项。"
            if failed:
                message += f"\n{failed} 项失败(文件正被占用或权限不足)。"
            self.set_status(message.replace("\n", " "))
            messagebox.showinfo("操作完成", message)
            if on_done is not None:
                on_done(failed_paths)

        self.run_task(work, done)


def main() -> None:
    root = tk.Tk()
    icon = _resource_path("app_icon.ico")
    if os.path.exists(icon):
        try:
            root.iconbitmap(icon)
        except tk.TclError:
            pass
    CleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
