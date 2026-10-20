"""硬盘清理工具 —— 桌面应用界面。

启动方式:
    双击 `启动清理工具.bat`
    或在命令行执行: python disk_cleaner.py

界面: 深色侧边栏导航 + 浅色主工作区, 全部控件均为自绘, 不依赖第三方库。
功能: 临时文件与系统缓存清理 / 回收站清空 / 大文件扫描 / 重复文件查找。
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

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
    "「程序文件 / 数据文件 / 虚拟机磁盘」= 属于系统或已安装程序的文件; "
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
        tk.Label(footer, text="v1.0", bg=C["sidebar"], fg="#4c5a70",
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

    def _compute_reclaimable(self) -> None:
        def work(cancel, report):
            targets = core.default_targets()
            return core.scan_targets(targets, cancel=cancel, progress=report)

        def done(result: core.ScanResult) -> None:
            self.overview_hint.configure(
                text=f"临时文件与缓存约占用 {core.human_size(result.total_bytes)} "
                     f"({result.file_count} 个文件), 清理后可释放这部分空间。")

        self.run_task(work, done)

    # ------------------------------------------------------ 通用小工具

    def _make_tree(self, parent: tk.Widget, columns: list[tuple[str, str, int, str]]) -> ttk.Treeview:
        frame = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                         highlightbackground=C["card_line"])
        frame.pack(fill="both", expand=True, pady=(12, 0))
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                            selectmode="extended")
        for key, title, width, anchor in columns:
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
        tree.bind("<<TreeviewSelect>>", lambda _e: None)
        return tree

    @staticmethod
    def _row_status(index: int, path: str, locked: set[str],
                    with_risk: bool = True) -> tuple[str, tuple[str, ...]]:
        """返回 (删除提示, 标签元组)。

        优先级: 占用中 > 风险类别 > 未占用。占用行标红、风险行标琥珀色。
        """
        tags = ["odd"] if index % 2 else []
        if path in locked:
            return "占用中", tuple(tags + ["inuse"])
        if with_risk:
            kind = core.deletion_risk(path)
            if kind:
                return kind, tuple(tags + ["risk"])
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
        tk.Label(card.body, text="清理位置", bg=C["card"], fg=C["text"],
                 font=(FONT, 12, "bold")).pack(anchor="w")
        tk.Label(card.body, text="勾选需要清理的位置; 临时文件按系统惯例直接删除, 不会进入回收站。"
                                "列表中的「占用中」表示文件正被程序使用(含已加载的程序模块), 清理时会被保留、不删除。",
                 bg=C["card"], fg=C["muted"], font=(FONT, 9)).pack(anchor="w", pady=(2, 10))

        self.temp_targets = core.default_targets()
        self.temp_vars: dict[str, tk.BooleanVar] = {}
        for target in self.temp_targets:
            var = tk.BooleanVar(value=target.enabled)
            self.temp_vars[target.key] = var
            row = tk.Frame(card.body, bg=C["card"])
            row.pack(fill="x", pady=3)
            Switch(row, var, bg=C["card"]).pack(side="left")
            text = tk.Frame(row, bg=C["card"])
            text.pack(side="left", padx=(10, 0))
            tk.Label(text, text=target.label, bg=C["card"], fg=C["text"],
                     font=(FONT, 10, "bold")).pack(anchor="w")
            tk.Label(text, text=target.note, bg=C["card"], fg=C["muted"],
                     font=(FONT, 9)).pack(anchor="w")
        if not self.temp_targets:
            tk.Label(card.body, text="未找到可清理的默认位置。", bg=C["card"],
                     fg=C["muted"]).pack(anchor="w")

        actions = tk.Frame(parent, bg=C["bg"])
        actions.pack(fill="x", pady=(14, 0))
        Button(actions, "扫描", self.scan_temp, fill=C["accent"], width=110).pack(side="left")
        Button(actions, "清理选中项", self.clean_temp, fill="#eef1f6", hover="#e3e8f0",
               text_color=C["text"], width=130).pack(side="left", padx=8)

        self.temp_summary = self._summary_label(parent)
        self.temp_tree = self._make_tree(
            parent,
            [("state", "删除提示", 90, "center"), ("size", "大小", 110, "e"),
             ("name", "文件", 290, "w"), ("path", "所在目录", 480, "w")],
        )

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
        if not messagebox.askyesno(
            "确认清理",
            "将直接删除选中位置中的临时文件(不进入回收站)。\n\n"
            "正被程序使用的文件会被自动保留、不会删除, 因此不会影响正在运行的程序。\n"
            "确定继续吗?",
        ):
            return

        def work(cancel, report):
            return core.clean_targets(targets, cancel=cancel, progress=report)

        def done(result: core.CleanResult) -> None:
            text = f"清理完成: 删除 {result.deleted} 个文件, 释放 {core.human_size(result.freed_bytes)}。"
            if result.blocked:
                text += f" 为安全起见保留 {result.blocked} 个正在被程序使用的文件(未尝试删除)。"
            if result.skipped:
                text += f" 另有 {result.skipped} 个文件因权限不足未删除。"
            self.temp_summary.configure(text=text)
            self.set_status("清理完成")
            self.temp_tree.delete(*self.temp_tree.get_children())

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
               hover="#e3e8f0", text_color=C["text"], width=190).pack(side="left", padx=8)

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
            protected = sum(1 for path, _size in files
                            if path in locked or core.deletion_risk(path))
            extra = (f" 其中 {protected} 个受保护(正被使用或属系统/程序文件), 不会被删除。"
                     if protected else " 未发现受保护的文件, 这批都可以删除。")
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
        total = sum(os.path.getsize(p) for p in allowed if os.path.exists(p))
        blocked_note = (f"\n其中 {len(blocked)} 个受保护({self._blocked_summary(blocked)}), 已排除、不会删除。"
                        if blocked else "")
        if not messagebox.askyesno(
            "确认删除",
            f"将把 {len(allowed)} 个文件(共 {core.human_size(total)})移入回收站。"
            f"{blocked_note}\n\n之后仍可从回收站还原。确定继续吗?",
        ):
            return
        self._move_to_recycle(allowed, "大文件", self._prune_large_tree)

    def _prune_large_tree(self, failed: list[str]) -> None:
        failed_set = set(failed)
        for iid, path in list(self.large_paths.items()):
            if path not in failed_set and self.large_tree.exists(iid):
                self.large_tree.delete(iid)
                del self.large_paths[iid]

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
               text_color=C["text"], width=130).pack(side="left", padx=8)
        Button(actions, "删除选中项", self.delete_selected_dups, fill="#eef1f6", hover="#e3e8f0",
               text_color=C["text"], width=120).pack(side="left")

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
        )

    def scan_dups(self) -> None:
        root = self.dup_drive.get()
        min_size = parse_size(self.dup_size.get())

        def work(cancel, report):
            groups = core.find_duplicates(root, min_size=min_size, cancel=cancel, progress=report)
            report("正在检查文件占用情况…", 0, 0)
            locked = core.check_in_use([path for group in groups for path in group.files])
            return groups, locked

        def done(result: tuple[list[core.DupGroup], set[str]]) -> None:
            groups, locked = result
            self.dup_groups = groups
            self.dup_tree.delete(*self.dup_tree.get_children())
            self.dup_paths.clear()
            wasted = 0
            for group_index, group in enumerate(groups):
                parent_iid = self.dup_tree.insert(
                    "", "end", tags=("group",), open=False,
                    values=("", core.human_size(group.size), len(group.files),
                            f"重复 {len(group.files)} 份", ""),
                )
                for file_index, path in enumerate(group.files):
                    iid = f"{group_index}-{file_index}"
                    self.dup_paths[iid] = path
                    state, tags = self._row_status(file_index, path, locked)
                    self.dup_tree.insert(parent_iid, "end", iid=iid, tags=tags, values=(
                        state, core.human_size(group.size), "", os.path.basename(path),
                        os.path.dirname(path)))
                wasted += group.wasted_bytes
            if groups:
                summary = (f"发现 {len(groups)} 组重复文件, 每组只保留一份可释放约 "
                           f"{core.human_size(wasted)}。")
                protected = sum(1 for group in groups for path in group.files
                                if path in locked or core.deletion_risk(path))
                if protected:
                    summary += f" 其中 {protected} 个受保护, 不会被删除。"
            else:
                summary = "未发现重复文件。"
            self.dup_summary.configure(text=summary)
            self.set_status("查找完成")

        self.run_task(work, done)

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
        total = sum(os.path.getsize(p) for p in allowed if os.path.exists(p))
        blocked_note = (f"\n其中 {len(blocked)} 个受保护({self._blocked_summary(blocked)}), 已排除、不会删除。"
                        if blocked else "")
        if not messagebox.askyesno(
            "确认删除",
            f"将把 {len(allowed)} 个重复文件(共 {core.human_size(total)})移入回收站, 每组保留一份。"
            f"{blocked_note}\n\n确定继续吗?",
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
        total = sum(os.path.getsize(p) for p in allowed if os.path.exists(p))
        blocked_note = (f"\n其中 {len(blocked)} 个受保护({self._blocked_summary(blocked)}), 已排除、不会删除。"
                        if blocked else "")
        if not messagebox.askyesno(
            "确认删除",
            f"将把选中的 {len(allowed)} 个文件(共 {core.human_size(total)})移入回收站。"
            f"{blocked_note}\n\n确定继续吗?",
        ):
            return
        self._move_to_recycle(allowed, "重复文件", self._prune_dup_tree)

    def _prune_dup_tree(self, failed: list[str]) -> None:
        failed_set = set(failed)
        removed: set[str] = set()
        for iid, path in list(self.dup_paths.items()):
            if path not in failed_set:
                removed.add(path)
                if self.dup_tree.exists(iid):
                    self.dup_tree.delete(iid)
                del self.dup_paths[iid]
        for group in self.dup_groups:
            group.files = [p for p in group.files if p not in removed]
        self.dup_groups = [g for g in self.dup_groups if len(g.files) > 1]
        self.dup_summary.configure(text=f"剩余 {len(self.dup_groups)} 组重复文件。")

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
