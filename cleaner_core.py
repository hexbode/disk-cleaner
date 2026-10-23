"""硬盘清理工具 —— 核心逻辑(与界面无关, 可单独调用/测试)。

包含四类能力:
  1. 临时文件与系统缓存扫描 / 清理
  2. 回收站容量查询 / 清空
  3. 大文件扫描
  4. 重复文件查找

所有删除操作默认走 Windows 回收站(可还原), 临时文件按系统惯例直接删除。
"""

from __future__ import annotations

import ctypes
import fnmatch
import glob
import hashlib
import heapq
import json
import os
import shutil
import stat
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

IS_WINDOWS = os.name == "nt"

# PyInstaller 单文件打包时, 程序启动会把自身解压到 %TEMP%\MEIxxxxxx 目录。
# 扫描/清理时跳过它, 免得把自己的运行时文件当成垃圾统计或删除。
_FROZEN_ROOT = os.path.abspath(sys._MEIPASS) if getattr(sys, "_MEIPASS", None) else None


def _is_own_runtime(path: str) -> bool:
    """路径是否属于程序自身的运行时解压目录(仅打包运行时存在)。"""
    if _FROZEN_ROOT is None:
        return False
    # 加分隔符比较, 避免把 _MEI1234 误判成 _MEI123 的子路径
    return path == _FROZEN_ROOT or path.startswith(_FROZEN_ROOT + os.sep)


# 取消检查函数: 返回 True 表示应当中止
CancelCheck = Callable[[], bool]
# 进度回调: (描述信息, 当前数量, 总数量[未知时为 0])
ProgressFn = Callable[[str, int, int], None]


# ---------------------------------------------------------------- 基础工具

def human_size(num: float) -> str:
    """把字节数格式化成易读字符串, 例如 1.5 GB。"""
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{int(num)} B" if unit == "B" else f"{num:,.1f} {unit}"
        num /= 1024.0
    return f"{num:,.1f} PB"


def total_size(paths: Iterable[str]) -> int:
    """安全地求和一批文件的大小; 已消失或无权限的文件按 0 计。"""
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
    return total


def is_admin() -> bool:
    """当前进程是否拥有管理员权限。"""
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def list_drives() -> list[str]:
    """列出所有已就绪的本地磁盘盘符, 例如 ['C:\\', 'D:\\']。"""
    if not IS_WINDOWS:
        return ["/"]
    drives = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except OSError:
        return ["C:\\"]
    for i in range(26):
        if mask & (1 << i):
            letter = chr(ord("A") + i) + ":\\"
            if os.path.exists(letter):
                drives.append(letter)
    return drives or ["C:\\"]


def _matches(name: str, patterns: list[str]) -> bool:
    """文件是否匹配给定通配符; 空列表表示匹配所有文件。"""
    if not patterns:
        return True
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, p.lower()) for p in patterns)


# 目录联接(junction)、符号链接等重解析点。遍历时若跟进去, 可能重复扫描甚至死循环,
# 也可能顺着链接删到目录之外, 因此一律不跟随。
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(info: os.stat_result) -> bool:
    """该目录项是否为重解析点(junction / 符号链接)。"""
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _iter_files(root: str, cancel: CancelCheck | None = None) -> Iterator[tuple[str, int]]:
    """递归遍历 root 下的所有普通文件, 产出 (完整路径, 字节大小)。

    用 os.scandir 实现: Windows 的目录项里已经带有文件大小与类型, 无需再对
    每个文件单独调用 stat/getsize, 实测比 os.walk + os.path.getsize 快数倍。
    符号链接与无法访问的目录会被静默跳过。
    """
    if not root or not os.path.isdir(root):
        return
    stack = [root]
    while stack:
        if cancel is not None and cancel():
            return
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if cancel is not None and cancel():
                        return
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if not _is_reparse_point(info) and not _is_own_runtime(entry.path):
                            stack.append(entry.path)
                    elif stat.S_ISREG(info.st_mode):
                        if not _is_own_runtime(entry.path):
                            yield entry.path, info.st_size
        except OSError:
            continue


def _remove_file(path: str) -> bool:
    """删除单个文件, 只读文件先改属性; 成功返回 True。"""
    try:
        os.remove(path)
        return True
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
            return True
        except OSError:
            return False
    except OSError:
        return False


# ------------------------------------------------------------ 文件占用检测

OPEN_EXISTING = 3
GENERIC_READ = 0x80000000
FILE_ATTRIBUTE_NORMAL = 0x0080
ERROR_SHARING_VIOLATION = 32

# 占用检测要对每个文件做一次独占打开, 在杀软实时防护下开销明显。
# 因此只对体积不小于该阈值的文件检测: 小文件即使被占用, 跳过它损失的空间也很有限。
IN_USE_CHECK_MIN_SIZE = 1024 * 1024

if IS_WINDOWS:
    # 注意: 必须用 use_last_error=True 的 WinDLL, 否则 GetLastError 会被
    # ctypes 内部的调用覆盖, 取到错误的错误码。
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = wintypes.HANDLE
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.restype = wintypes.BOOL
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _GetDriveTypeW = _kernel32.GetDriveTypeW
    _GetDriveTypeW.restype = wintypes.UINT
    _GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


# GetDriveTypeW 的返回值
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6


def recycle_bin_supported(path: str) -> bool:
    """该路径所在的卷是否能比较可靠地使用回收站。

    本地固定磁盘通常可以; 网络位置一般没有回收站, 可移动磁盘只有在已经
    存在 $Recycle.Bin 时才认为可用。其他类型(光盘、内存盘)一律视为不支持。
    非 Windows 平台返回 False。
    """
    if not IS_WINDOWS:
        return False
    try:
        drive, _tail = os.path.splitdrive(os.path.abspath(path))
    except (OSError, ValueError):
        return False
    # UNC 路径直接判定为不支持: 既符合实际情况, 也避免探测不存在的服务器时卡住
    if drive.startswith("\\\\"):
        return False
    kind = _GetDriveTypeW(drive + "\\")
    if kind == DRIVE_FIXED:
        return True
    if kind == DRIVE_REMOVABLE:
        return os.path.isdir(os.path.join(drive + "\\", "$Recycle.Bin"))
    return False


def unsupported_recycle_paths(paths: Iterable[str]) -> list[str]:
    """从路径列表中筛出所在卷不支持回收站的项(用于删除前提示)。

    同一个卷只检测一次, 避免批量文件重复查询驱动器类型。
    """
    checked: dict[str, bool] = {}
    result: list[str] = []
    for path in paths:
        try:
            drive = os.path.splitdrive(os.path.abspath(path))[0].lower()
        except (OSError, ValueError):
            drive = ""
        if drive not in checked:
            checked[drive] = recycle_bin_supported(path)
        if not checked[drive]:
            result.append(path)
    return result


def is_file_in_use(path: str) -> bool:
    """文件当前是否被其它进程占用。

    以"申请读取权限 + 不允许任何共享"的方式尝试打开文件: 若系统返回共享冲突,
    说明该文件已被别的程序打开 —— 删除它要么会被系统拒绝, 要么会影响那个
    正在运行的程序。(只有申请数据读取权限才会触发共享冲突, 申请 0 权限不会。)
    非 Windows 平台恒为 False。
    """
    if not IS_WINDOWS:
        return False
    handle = _CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING,
                          FILE_ATTRIBUTE_NORMAL, None)
    error = ctypes.get_last_error()
    if handle and handle != _INVALID_HANDLE:
        _CloseHandle(handle)
        return False
    return error == ERROR_SHARING_VIOLATION


def _norm_path(path: str) -> str:
    """规范化为绝对路径 + 统一大小写, 便于直接比较。"""
    return os.path.normcase(os.path.abspath(path))


# ------------------------------------------------ 已加载模块(映像)检测

# EXE/DLL 被加载时会映射成"映像节", 加载器创建节后就关闭了文件句柄 —— 此时
# 任何"独占打开"探测都撞不到冲突, 会把这些文件误判成"未被占用"。只能靠枚举
# 各进程已加载的模块列表来识别。
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_PATH = 260

_MODULES_TTL = 3.0
_modules_cache: tuple[float, set[str]] | None = None


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * MAX_PATH),
    ]


if IS_WINDOWS:
    _CreateToolhelp32Snapshot = _kernel32.CreateToolhelp32Snapshot
    _CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _Process32FirstW = _kernel32.Process32FirstW
    _Process32FirstW.restype = wintypes.BOOL
    _Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _Process32NextW = _kernel32.Process32NextW
    _Process32NextW.restype = wintypes.BOOL
    _Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _Module32FirstW = _kernel32.Module32FirstW
    _Module32FirstW.restype = wintypes.BOOL
    _Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
    _Module32NextW = _kernel32.Module32NextW
    _Module32NextW.restype = wintypes.BOOL
    _Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]


def _collect_process_modules(pid: int, paths: set[str]) -> None:
    """把某个进程已加载的模块路径并入 paths(无权访问的进程直接跳过)。"""
    snapshot = _CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snapshot or snapshot == _INVALID_HANDLE:
        return
    try:
        entry = _MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(_MODULEENTRY32W)
        has_module = _Module32FirstW(snapshot, ctypes.byref(entry))
        while has_module:
            if entry.szExePath:
                paths.add(_norm_path(entry.szExePath))
            has_module = _Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        _CloseHandle(snapshot)


def loaded_module_paths(force: bool = False) -> set[str]:
    """汇总所有运行进程已加载的模块(EXE/DLL)路径, 规范化后的小写形式。

    枚举一次开销约一秒, 因此在短时间内缓存复用。
    """
    global _modules_cache
    if not IS_WINDOWS:
        return set()
    now = time.monotonic()
    if not force and _modules_cache is not None and now - _modules_cache[0] < _MODULES_TTL:
        return _modules_cache[1]

    paths: set[str] = set()
    snapshot = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot and snapshot != _INVALID_HANDLE:
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            has_process = _Process32FirstW(snapshot, ctypes.byref(entry))
            while has_process:
                if entry.th32ProcessID:
                    _collect_process_modules(entry.th32ProcessID, paths)
                has_process = _Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _CloseHandle(snapshot)

    _modules_cache = (now, paths)
    return paths


def check_in_use(paths: Iterable[str], modules: set[str] | None = None) -> set[str]:
    """从给定路径中筛出当前正被使用的文件。

    两类来源: 被其它进程以文件句柄打开(共享冲突), 或被加载成映像的 EXE/DLL。
    """
    if modules is None:
        modules = loaded_module_paths()
    return {
        path for path in paths
        if _norm_path(path) in modules or is_file_in_use(path)
    }


# ------------------------------------------------------------ 删除风险分类

_RISK_DISK_EXT = {".vdi", ".vhdx", ".vhd", ".vmdk", ".qcow2", ".avhdx"}
_RISK_DATA_EXT = {
    ".db", ".db3", ".sqlite", ".sqlite3", ".mdb", ".accdb",
    ".pst", ".mdf", ".ldf", ".ndf",  # Outlook 邮件库 / SQL Server 数据库
}
_RISK_DATA_SUFFIX = (".db-wal", ".db-shm")
_RISK_PROGRAM_EXT = {".dll", ".sys", ".ocx", ".node", ".asar"}

# 系统关键目录: 删除其中的文件会破坏 Windows 或已安装的程序。
# 只覆盖明确"不可删"的目录; Windows\Temp、SoftwareDistribution\Download、
# Prefetch 等可清理位置不在此列。
_RISK_PROGRAM_DIRS = (
    "\\windowsapps\\",
    "\\program files\\",
    "\\program files (x86)\\",
    "\\windows\\winsxs\\",
    "\\windows\\system32\\",
    "\\windows\\syswow64\\",
    "\\windows\\systemapps\\",
    "\\windows\\servicing\\",
    "\\windows\\installer\\",
    "\\windows\\assembly\\",
    "\\windows\\microsoft.net\\",
    "\\windows\\boot\\",
    "\\windows\\fonts\\",
    "\\windows\\inf\\",
    "\\programdata\\package cache\\",
    "\\programdata\\microsoft\\windows defender\\",
    "\\programdata\\microsoft\\windows\\systemdata\\",
    "\\appdata\\local\\programs\\",
    "\\system volume information\\",
    "\\$recycle.bin\\",
    "\\recovery\\",
    "\\resources\\app\\",
)
_INSTALLER_MARKERS = ("install", "setup", "uninst")

# 可以删除、但值得提醒的类型(例如下载的安装包、日志、备份)。
# 这些只做提示, 不阻止删除。
_NOTICE_EXT = {
    ".msi": "安装包",
    ".msp": "安装包",
    ".msm": "安装包",
    ".log": "日志文件",
    ".bak": "备份文件",
    ".iso": "光盘镜像",
    ".img": "磁盘镜像",
    ".ost": "邮件缓存",
}


# 读取注册表安装信息的缓存: 枚举一次开销约几十毫秒, 短时间内复用。
_INSTALL_DIRS_TTL = 120.0
_install_dirs_cache: tuple[float, tuple[str, ...]] | None = None


def _read_uninstall_locations() -> list[str]:
    """从卸载注册表项读取各程序的安装目录。

    优先使用 InstallLocation; 部分程序只登记了 DisplayIcon, 则取其可执行
    文件所在目录。读取失败或无权限的条目直接跳过。
    """
    try:
        import winreg
    except ImportError:
        return []

    roots = (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    locations: list[str] = []
    for root, subkey in roots:
        try:
            with winreg.OpenKey(root, subkey) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, name) as entry:
                            location = _read_location(winreg, entry)
                    except OSError:
                        continue
                    if location:
                        locations.append(location)
        except OSError:
            continue
    return locations


def _read_location(winreg, entry) -> str | None:
    """从一个卸载项里解析安装目录。"""
    for value_name in ("InstallLocation", "DisplayIcon"):
        try:
            raw = winreg.QueryValueEx(entry, value_name)[0]
        except OSError:
            continue
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = os.path.expandvars(raw.split(",")[0].strip().strip('"'))
        if value_name == "DisplayIcon":
            if not text.lower().endswith(".exe"):
                continue
            text = os.path.dirname(text)
        return text
    return None


# ---------------------------------------------------- 开始菜单快捷方式
# 绿色软件通常不写卸载注册表, 但一般会创建开始菜单快捷方式。解析 .lnk 的
# 目标路径(Shell Link 二进制格式), 就能反推它实际安装在哪个目录。

_START_MENU_SUBDIR = os.path.join("Microsoft", "Windows", "Start Menu", "Programs")


def _start_menu_dirs() -> list[str]:
    """系统和当前用户的开始菜单程序目录。"""
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    app_data = os.environ.get("APPDATA", "")
    candidates = [os.path.join(program_data, _START_MENU_SUBDIR)]
    if app_data:
        candidates.append(os.path.join(app_data, _START_MENU_SUBDIR))
    return _existing_dirs(candidates)


def _read_utf16z(data: bytes, offset: int) -> str | None:
    """读取 UTF-16LE 的 NUL 结尾字符串。"""
    if offset >= len(data):
        return None
    text = data[offset:].decode("utf-16-le", errors="ignore").split("\x00", 1)[0]
    return text or None


def _read_ansiz(data: bytes, offset: int) -> str | None:
    """读取 ANSI(系统代码页)的 NUL 结尾字符串。"""
    if offset >= len(data):
        return None
    raw = data[offset:].split(b"\x00", 1)[0]
    text = raw.decode("mbcs", errors="ignore")
    return text or None


def _parse_link_info(info: bytes) -> str | None:
    """从 LinkInfo 结构中取 LocalBasePath(优先 Unicode 版本)。"""
    if len(info) < 0x1C:
        return None
    header_size = struct.unpack_from("<I", info, 4)[0]
    # LinkInfoHeaderSize >= 0x24 时才有 Unicode 路径字段
    if header_size >= 0x24 and len(info) >= 0x24:
        unicode_offset = struct.unpack_from("<I", info, 0x1C)[0]
        if unicode_offset:
            text = _read_utf16z(info, unicode_offset)
            if text:
                return text
    local_offset = struct.unpack_from("<I", info, 16)[0]
    if local_offset:
        return _read_ansiz(info, local_offset)
    return None


def _read_string_data(data: bytes, offset: int,
                      is_unicode: bool) -> tuple[str | None, int]:
    """读取一段 StringData(长度 WORD + 字符串), 返回 (文本, 新偏移)。"""
    if offset + 2 > len(data):
        return None, offset
    count = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    size = count * 2 if is_unicode else count
    raw = data[offset:offset + size]
    offset += size
    encoding = "utf-16-le" if is_unicode else "mbcs"
    return raw.decode(encoding, errors="ignore"), offset


def _parse_shortcut(data: bytes, base_dir: str) -> str | None:
    """解析 .lnk 内容, 返回目标程序所在目录; 无法解析时返回 None。

    base_dir 是快捷方式自身所在目录, 用于解析相对目标路径。
    """
    if len(data) < 0x4C or data[:4] != b"L\x00\x00\x00":
        return None
    flags = struct.unpack_from("<I", data, 0x14)[0]
    offset = 0x4C
    unicode_flag = bool(flags & 0x80)

    if flags & 0x01:  # HasLinkTargetIDList: 跳过整个 IDList
        if offset + 2 > len(data):
            return None
        id_list_size = struct.unpack_from("<H", data, offset)[0]
        offset += 2 + id_list_size

    target = None
    if flags & 0x02:  # HasLinkInfo
        if offset + 4 > len(data):
            return None
        info_size = struct.unpack_from("<I", data, offset)[0]
        if info_size < 0x1C or offset + info_size > len(data):
            return None
        target = _parse_link_info(data[offset:offset + info_size])
        offset += info_size

    # StringData 按固定顺序排列: 名称 / 相对路径 / 工作目录 / 参数 / 图标
    relative = working = None
    if flags & 0x04:  # HasName
        _name, offset = _read_string_data(data, offset, unicode_flag)
    if flags & 0x08:  # HasRelativePath
        relative, offset = _read_string_data(data, offset, unicode_flag)
    if flags & 0x10:  # HasWorkingDir
        working, offset = _read_string_data(data, offset, unicode_flag)

    candidate = target or relative or working
    if not candidate:
        return None
    candidate = os.path.expandvars(candidate)
    if not os.path.isabs(candidate):
        candidate = os.path.join(base_dir, candidate)
    if candidate.lower().endswith(".exe"):
        candidate = os.path.dirname(candidate)
    return candidate or None


def _shortcut_target(path: str) -> str | None:
    """读取单个 .lnk 并返回其目标所在目录。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read(256 * 1024)
        return _parse_shortcut(data, os.path.dirname(path))
    except (OSError, ValueError, struct.error):
        return None


def _shortcut_locations() -> list[str]:
    """扫描开始菜单中的快捷方式, 汇总它们指向的程序目录。"""
    locations: list[str] = []
    for root in _start_menu_dirs():
        for path, _size in _iter_files(root):
            if not path.lower().endswith(".lnk"):
                continue
            target = _shortcut_target(path)
            if target:
                locations.append(target)
    return locations


def _usable_install_dir(path: str) -> str | None:
    """规范化为可用的程序目录; 过宽或不存在的位置返回 None。

    排除盘符根、单层目录、Windows / ProgramData、用户目录本身, 以及
    %TEMP% 下的临时解压目录, 避免因为某个安装器把范围写得过大而误保护。
    """
    try:
        normalized = os.path.normcase(os.path.abspath(path))
    except (OSError, ValueError):
        return None
    if not os.path.isdir(normalized):
        return None
    _drive, tail = os.path.splitdrive(normalized)
    parts = [part for part in tail.split(os.sep) if part]
    if len(parts) < 2:
        return None
    top = parts[0].lower()
    if top in ("windows", "programdata"):
        return None
    if top == "users" and len(parts) <= 2:
        return None
    if "\\appdata\\local\\temp\\" in normalized or "\\windows\\temp\\" in normalized:
        return None
    return normalized


def installed_program_dirs(force: bool = False) -> tuple[str, ...]:
    """已安装程序的实际目录列表(规范化小写)。

    来源有两处: 卸载注册表(覆盖常规安装) 和开始菜单快捷方式(覆盖绿色软件)。
    过宽的路径会被排除, 避免误保护整片区域。
    """
    global _install_dirs_cache
    if not IS_WINDOWS:
        return ()
    now = time.monotonic()
    if (not force and _install_dirs_cache is not None
            and now - _install_dirs_cache[0] < _INSTALL_DIRS_TTL):
        return _install_dirs_cache[1]

    result: list[str] = []
    seen: set[str] = set()
    for location in _read_uninstall_locations() + _shortcut_locations():
        normalized = _usable_install_dir(location)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)

    _install_dirs_cache = (now, tuple(result))
    return _install_dirs_cache[1]


# 正在运行的程序目录变化较快, 用较短的缓存
_RUNNING_DIRS_TTL = 5.0
_running_dirs_cache: tuple[float, tuple[str, ...]] | None = None


def running_program_dirs(force: bool = False) -> tuple[str, ...]:
    """当前运行进程所加载模块(EXE/DLL)所在的程序目录。

    用来兜底既没有卸载注册表、也没有开始菜单快捷方式的便携软件:
    只要它正在运行, 其所在目录就能被识别并保护。
    """
    global _running_dirs_cache
    if not IS_WINDOWS:
        return ()
    now = time.monotonic()
    if (not force and _running_dirs_cache is not None
            and now - _running_dirs_cache[0] < _RUNNING_DIRS_TTL):
        return _running_dirs_cache[1]

    dirs: list[str] = []
    seen: set[str] = set()
    for module in loaded_module_paths():
        parent = _usable_install_dir(os.path.dirname(module))
        if parent is None or parent in seen:
            continue
        seen.add(parent)
        dirs.append(parent)

    _running_dirs_cache = (now, tuple(dirs))
    return _running_dirs_cache[1]


def deletion_risk(path: str) -> str | None:
    """提示删除该文件可能损坏程序或丢失数据, 返回风险类别; 无风险返回 None。

    判定顺序: 扩展名 → 系统关键目录 → 已安装程序的实际目录。
    属于"可删但需注意"的类型(安装包、日志等)由 deletion_notice 单独提示。
    """
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1]
    if ext in _RISK_DISK_EXT:
        return "虚拟机磁盘"
    if ext in _RISK_DATA_EXT or name.endswith(_RISK_DATA_SUFFIX):
        return "数据文件"
    if ext in _RISK_PROGRAM_EXT:
        return "程序文件"
    if ext == ".exe" and not any(m in name for m in _INSTALLER_MARKERS):
        return "程序文件"

    lowered = os.path.normcase(os.path.abspath(path))
    if any(marker in lowered for marker in _RISK_PROGRAM_DIRS):
        return "程序文件"
    for root in installed_program_dirs():
        if lowered.startswith(root + os.sep):
            return "程序文件"
    for root in running_program_dirs():
        if lowered.startswith(root + os.sep):
            return "程序文件"
    return None


def deletion_notice(path: str) -> str | None:
    """返回"可以删除但建议确认"的类别; 无提示返回 None。

    与 deletion_risk 不同, 这里的文件不会被阻止删除, 只在界面上给出提示。
    """
    ext = os.path.splitext(os.path.basename(path).lower())[1]
    return _NOTICE_EXT.get(ext)


def protection_reason(path: str, modules: set[str] | None = None) -> str | None:
    """返回禁止删除该文件的原因; 允许删除则返回 None。

    受保护的两种情况:
      1. 正在被程序使用 —— 有进程以文件句柄打开它, 或已被加载成映像模块;
      2. 属于系统 / 已安装程序 / 软件数据 —— 删除会损坏程序或丢失数据。
    """
    if modules is None:
        modules = loaded_module_paths()
    if _norm_path(path) in modules or is_file_in_use(path):
        return "正在被程序使用"
    return deletion_risk(path)


# ---------------------------------------------------- Windows 回收站 / Shell

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


if IS_WINDOWS:
    # 显式声明参数与返回值类型, 避免 64 位下指针/HRESULT 被按默认 int 处理
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _SHFileOperationW = _shell32.SHFileOperationW
    _SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    _SHFileOperationW.restype = ctypes.c_int
    _SHQueryRecycleBinW = _shell32.SHQueryRecycleBinW
    _SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SHQUERYRBINFO)]
    _SHQueryRecycleBinW.restype = ctypes.c_long
    _SHEmptyRecycleBinW = _shell32.SHEmptyRecycleBinW
    _SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
    _SHEmptyRecycleBinW.restype = ctypes.c_long


def move_to_recycle_bin(paths: Iterable[str], chunk: int = 200) -> tuple[int, int, list[str]]:
    """把文件/文件夹移入回收站。

    返回 (成功数, 失败数, 失败路径列表)。非 Windows 平台一律视为失败。
    """
    paths = [os.path.abspath(p) for p in paths if p]
    if not paths:
        return 0, 0, []
    if not IS_WINDOWS:
        return 0, len(paths), list(paths)

    moved = 0
    failed_paths: list[str] = []
    for i in range(0, len(paths), chunk):
        batch = paths[i:i + chunk]
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        # pFrom 需以双 NUL 结尾, 多项之间用单 NUL 分隔
        op.pFrom = "\0".join(batch) + "\0\0"
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        _SHFileOperationW(ctypes.byref(op))
        # 以"文件是否还存在"作为判定依据, 结果更准确
        for path in batch:
            if os.path.exists(path):
                failed_paths.append(path)
            else:
                moved += 1
    return moved, len(failed_paths), failed_paths


def recycle_bin_info() -> tuple[int, int]:
    """返回回收站的 (总字节数, 文件数量)。"""
    if not IS_WINDOWS:
        return 0, 0
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
    try:
        code = _SHQueryRecycleBinW(None, ctypes.byref(info))
    except OSError:
        return 0, 0
    if code != 0:
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def empty_recycle_bin() -> tuple[bool, str]:
    """清空回收站, 返回 (是否成功, 提示信息)。"""
    if not IS_WINDOWS:
        return False, "当前系统不支持回收站清空。"
    try:
        code = _SHEmptyRecycleBinW(
            None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        )
    except OSError as exc:
        return False, f"调用系统接口失败: {exc}"
    if code == 0:
        return True, "回收站已清空。"
    # 0x8000FFFF (E_UNEXPECTED) 在回收站本来就是空的时候也会返回
    if (code & 0xFFFFFFFF) == 0x8000FFFF:
        return True, "回收站已经是空的, 无需清理。"
    return False, f"清空回收站失败(错误码 0x{code & 0xFFFFFFFF:08X})。"


# ------------------------------------------------------------ 临时文件清理

@dataclass
class CleanTarget:
    """一个可清理的位置。"""
    key: str
    label: str
    paths: list[str]
    patterns: list[str] = field(default_factory=list)  # 空 = 目录下所有文件
    note: str = ""
    need_admin: bool = False
    enabled: bool = True
    custom: bool = False  # 用户手工添加的目录, 可以移除


@dataclass
class ScanResult:
    file_count: int = 0
    total_bytes: int = 0
    errors: int = 0
    top_files: list[tuple[str, int]] = field(default_factory=list)
    locked_paths: set[str] = field(default_factory=set)
    locked_bytes: int = 0

    @property
    def locked_count(self) -> int:
        return len(self.locked_paths)

    @property
    def deletable_bytes(self) -> int:
        """预计真正能释放的空间(排除被占用、删不掉的部分)。"""
        return self.total_bytes - self.locked_bytes


@dataclass
class CleanResult:
    deleted: int = 0
    freed_bytes: int = 0
    skipped: int = 0
    blocked: int = 0
    failed: list[str] = field(default_factory=list)
    # 清理前各磁盘的可用空间, 供界面展示"实际释放了多少"
    free_before: dict[str, int] = field(default_factory=dict)


def _existing_dirs(paths: Iterable[str]) -> list[str]:
    """筛出实际存在的目录, 并去掉重复项(保持顺序)。"""
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        if not path:
            continue
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen or not os.path.isdir(path):
            continue
        seen.add(normalized)
        result.append(os.path.abspath(path))
    return result


def browser_cache_paths() -> list[str]:
    """Chrome / Edge / Brave / Firefox 的网页缓存目录(含多用户配置)。"""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return []
    user_data_dirs = [
        os.path.join(local, "Google", "Chrome", "User Data"),
        os.path.join(local, "Microsoft", "Edge", "User Data"),
        os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
        os.path.join(local, "Chromium", "User Data"),
    ]
    candidates: list[str] = []
    for user_data in user_data_dirs:
        candidates.append(os.path.join(user_data, "Default", "Cache"))
        candidates.extend(glob.glob(os.path.join(user_data, "Profile *", "Cache")))
        candidates.extend(glob.glob(os.path.join(user_data, "Profile*", "Cache")))
    # Firefox 的缓存位于各配置目录下的 cache2, 配置目录名是随机串
    candidates.extend(glob.glob(os.path.join(
        local, "Mozilla", "Firefox", "Profiles", "*", "cache2")))
    return _existing_dirs(candidates)


def default_targets() -> list[CleanTarget]:
    """返回内置的清理位置列表(只保留本机实际存在的目录)。"""
    local = os.environ.get("LOCALAPPDATA", "")
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    win = os.environ.get("SystemRoot", r"C:\Windows")
    system_drive = os.path.splitdrive(win)[0] + "\\"
    user_temp = os.environ.get("TEMP") or os.path.join(local, "Temp")

    targets = [
        CleanTarget(
            key="user_temp",
            label="用户临时文件",
            paths=[user_temp],
            note="当前用户 Temp 目录, 各类程序运行产生的临时文件",
        ),
        CleanTarget(
            key="system_temp",
            label="系统临时文件",
            paths=[os.path.join(win, "Temp")],
            note="C:\\Windows\\Temp, 清理需要管理员权限",
            need_admin=True,
        ),
        CleanTarget(
            key="update_cache",
            label="Windows 更新缓存",
            paths=[os.path.join(win, "SoftwareDistribution", "Download")],
            note="已下载的更新安装包, 删除后系统会按需重新下载; 需要管理员权限",
            need_admin=True,
        ),
        CleanTarget(
            key="explorer_cache",
            label="缩略图 / 图标缓存",
            paths=[os.path.join(local, "Microsoft", "Windows", "Explorer")],
            patterns=["thumbcache_*.db", "iconcache_*.db"],
            note="资源管理器生成的缩略图与图标缓存(不影响个人文件)",
        ),
        CleanTarget(
            key="browser_cache",
            label="浏览器缓存",
            paths=browser_cache_paths(),
            note="Chrome / Edge / Firefox 的网页缓存(不影响书签、密码与历史记录)",
        ),
        CleanTarget(
            key="crash_dumps",
            label="崩溃转储文件",
            paths=[os.path.join(local, "CrashDumps")],
            note="程序崩溃时生成的 .dmp 转储文件, 仅用于事后分析",
        ),
        CleanTarget(
            key="inet_cache",
            label="系统网络缓存",
            paths=[os.path.join(local, "Microsoft", "Windows", "INetCache")],
            note="IE / 系统组件下载产生的临时网页文件",
        ),
        CleanTarget(
            key="d3d_cache",
            label="DirectX 着色器缓存",
            paths=[os.path.join(local, "D3DSCache")],
            note="显卡着色器编译缓存, 删除后游戏首次启动会略慢",
        ),
        CleanTarget(
            key="delivery_optimization",
            label="传递优化缓存",
            paths=[os.path.join(win, "SoftwareDistribution", "DeliveryOptimization")],
            note="Windows 更新分发缓存; 需要管理员权限",
            need_admin=True,
        ),
        CleanTarget(
            key="prefetch",
            label="预读取数据 (Prefetch)",
            paths=[os.path.join(win, "Prefetch")],
            note="系统预读取记录; 清理后程序首次启动会略慢, 需要管理员权限",
            need_admin=True,
        ),
        CleanTarget(
            key="error_reports",
            label="Windows 错误报告",
            paths=[os.path.join(program_data, "Microsoft", "Windows", "WER")],
            note="系统与应用错误报告队列; 需要管理员权限",
            need_admin=True,
        ),
        CleanTarget(
            key="windows_old",
            label="旧系统备份 (Windows.old)",
            paths=[os.path.join(system_drive, "Windows.old")],
            note="系统升级前的旧版本备份, 删除后无法回退到旧系统; 需要管理员权限, 默认不勾选",
            need_admin=True,
            enabled=False,
        ),
    ]
    return [t for t in targets if any(os.path.isdir(p) for p in t.paths)]


# ------------------------------------------------------------ 自定义目录

_APP_DIR_NAME = "DiskCleaner"
# 这些目录下的内容不适合作为"自定义清理目录": 删错会破坏系统或程序
_FORBIDDEN_TOP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "system volume information", "$recycle.bin", "recovery", "boot",
}


def app_data_dir() -> str:
    """程序配置目录(%LOCALAPPDATA%\\DiskCleaner), 不可写时退回用户主目录。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, _APP_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return base


def _settings_file() -> str:
    return os.path.join(app_data_dir(), "settings.json")


def _load_settings() -> dict:
    try:
        with open(_settings_file(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_settings(data: dict) -> bool:
    """原子写入配置: 先写临时文件再替换, 避免中断后留下半截 JSON。"""
    path = _settings_file()
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp, path)
        return True
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass
        return False


def load_custom_dirs(include_missing: bool = False) -> list[str]:
    """读取用户保存的自定义清理目录。

    配置文件可能被手工修改, 因此这里再次做安全检查:
    位于系统 / 程序目录的路径一律忽略。
    include_missing=True 时保留当前不可用的目录(例如暂时离线的网络盘),
    供界面维护完整配置; 默认只返回当前可用的目录。
    """
    dirs = _load_settings().get("custom_dirs")
    if not isinstance(dirs, list):
        return []
    safe = [d for d in dirs if isinstance(d, str) and is_dangerous_dir(d) is None]
    return safe if include_missing else _existing_dirs(safe)


def save_custom_dirs(dirs: Iterable[str]) -> bool:
    """保存自定义清理目录, 返回是否写入成功。

    这里不要求目录当前存在: 网络盘暂时离线时不应丢掉用户配置。
    """
    data = _load_settings()
    saved: list[str] = []
    seen: set[str] = set()
    for path in dirs:
        if not path:
            continue
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        saved.append(os.path.abspath(path))
    data["custom_dirs"] = saved
    return _save_settings(data)


def custom_targets(dirs: Iterable[str] | None = None) -> list[CleanTarget]:
    """把自定义目录转换成清理目标; 不传 dirs 时读取已保存的列表。

    无论来源如何, 危险目录都会被过滤掉, 避免绕过界面校验。
    """
    paths = _existing_dirs(dirs) if dirs is not None else load_custom_dirs()
    targets = []
    for path in paths:
        if is_dangerous_dir(path) is not None or not os.path.isdir(path):
            continue
        name = os.path.basename(path.rstrip("\\/")) or path
        targets.append(CleanTarget(
            key=f"custom:{os.path.normcase(path)}",
            label=name,
            paths=[path],
            note=f"自定义目录: {path}",
            custom=True,
        ))
    return targets


def is_dangerous_dir(path: str) -> str | None:
    """检查自定义目录是否安全; 返回拒绝原因, 允许则返回 None。

    拒绝盘符根目录与系统 / 程序目录, 避免用户把整个系统盘加入清理列表。
    """
    if not path:
        return "路径为空"
    full = os.path.abspath(path)
    _drive, tail = os.path.splitdrive(full)
    parts = [part for part in tail.replace("/", "\\").split("\\") if part]
    if not parts:
        return "不能选择磁盘根目录"
    top = parts[0].lower()
    if top in _FORBIDDEN_TOP_DIRS or top.startswith("$"):
        return "不能选择系统或程序目录"
    if top == "users" and len(parts) <= 2:
        return "不能选择用户目录本身, 请选更具体的子目录"
    return None


def free_space_snapshot(targets: Iterable[CleanTarget]) -> dict[str, int]:
    """记录各目标所在磁盘的当前可用空间 {盘符根: 字节数}。"""
    roots: set[str] = set()
    for target in targets:
        for path in target.paths:
            try:
                root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
            except (OSError, ValueError):
                continue
            roots.add(root)
    snapshot: dict[str, int] = {}
    for root in roots:
        try:
            snapshot[root] = shutil.disk_usage(root).free
        except OSError:
            continue
    return snapshot


def free_space_delta(before: dict[str, int]) -> tuple[int, int]:
    """对比当前可用空间与快照, 返回 (差值的合计, 参与统计的磁盘数)。

    只统计差值为正(空间确实变多)的磁盘, 避免其它程序同时写盘造成负值干扰。
    """
    total = 0
    counted = 0
    for root, old in before.items():
        try:
            now = shutil.disk_usage(root).free
        except OSError:
            continue
        counted += 1
        delta = now - old
        if delta > 0:
            total += delta
    return total, counted


def scan_targets(
    targets: Iterable[CleanTarget],
    cancel: CancelCheck | None = None,
    progress: ProgressFn | None = None,
) -> ScanResult:
    """统计待清理目标占用的空间, 并列出最大的若干文件。"""
    result = ScanResult()
    top_n = 100
    heap: list[tuple[int, str]] = []
    scanned = 0
    modules = loaded_module_paths()

    for target in targets:
        if cancel is not None and cancel():
            break
        for root in target.paths:
            for filepath, size in _iter_files(root, cancel):
                if target.patterns and not _matches(os.path.basename(filepath), target.patterns):
                    continue
                result.file_count += 1
                result.total_bytes += size
                scanned += 1
                # 模块查表对所有文件都做(基本零成本); 句柄探测只对较大文件做。
                in_use = _norm_path(filepath) in modules
                if not in_use and size >= IN_USE_CHECK_MIN_SIZE:
                    in_use = is_file_in_use(filepath)
                if in_use:
                    result.locked_paths.add(filepath)
                    result.locked_bytes += size
                if len(heap) < top_n:
                    heapq.heappush(heap, (size, filepath))
                elif size > heap[0][0]:
                    heapq.heapreplace(heap, (size, filepath))
                if progress is not None and scanned % 300 == 0:
                    progress(f"正在统计并检查占用… 已检查 {scanned} 个文件", scanned, 0)

    result.top_files = [(p, s) for s, p in sorted(heap, reverse=True)]
    return result


def _clean_tree(root: str, patterns: list[str], result: "CleanResult",
                cancel: CancelCheck | None, progress: ProgressFn | None,
                modules: set[str]) -> None:
    """后序遍历目录树, 删除匹配的文件, 并移除清理后变空的子目录(root 本身保留)。

    正在被程序使用的文件会被预先拦下, 不尝试删除(宁可不删, 保证不影响运行中的程序)。
    """
    stack: list[tuple[str, bool]] = [(root, False)]
    while stack:
        if cancel is not None and cancel():
            return
        path, processed = stack.pop()
        if processed:
            if path != root:
                try:
                    os.rmdir(path)
                except OSError:
                    pass
            continue
        stack.append((path, True))
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if cancel is not None and cancel():
                        return
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if not _is_reparse_point(info) and not _is_own_runtime(entry.path):
                            stack.append((entry.path, False))
                    elif stat.S_ISREG(info.st_mode):
                        if _is_own_runtime(entry.path) or not _matches(entry.name, patterns):
                            continue
                        # 预检: 已加载模块(查表)或较大且被句柄占用的文件直接拦下
                        if _norm_path(entry.path) in modules or (
                                info.st_size >= IN_USE_CHECK_MIN_SIZE
                                and is_file_in_use(entry.path)):
                            result.blocked += 1
                            continue
                        if _remove_file(entry.path):
                            result.deleted += 1
                            result.freed_bytes += info.st_size
                            if progress is not None and result.deleted % 100 == 0:
                                progress(f"正在清理… 已删除 {result.deleted} 个文件", result.deleted, 0)
                        else:
                            # 权限不足等原因未能删除
                            result.skipped += 1
                            result.failed.append(entry.path)
        except OSError:
            continue


def clean_targets(
    targets: Iterable[CleanTarget],
    cancel: CancelCheck | None = None,
    progress: ProgressFn | None = None,
) -> CleanResult:
    """清理指定目标中的文件(直接删除, 与系统磁盘清理行为一致)。

    被程序使用的文件一律跳过、不删除。
    """
    result = CleanResult()
    targets = list(targets)
    result.free_before = free_space_snapshot(targets)
    modules = loaded_module_paths()
    for target in targets:
        if cancel is not None and cancel():
            break
        for root in target.paths:
            if not os.path.isdir(root):
                continue
            _clean_tree(root, target.patterns, result, cancel, progress, modules)
    return result


# ---------------------------------------------------------------- 大文件扫描

def scan_large_files(
    root: str,
    min_size: int = 100 * 1024 * 1024,
    top_n: int = 300,
    cancel: CancelCheck | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list[tuple[str, int]], int]:
    """扫描 root 下大于 min_size 的文件。

    返回 ([(路径, 大小)...] 按大小降序, 已扫描文件总数)。
    """
    heap: list[tuple[int, str]] = []
    scanned = 0

    for filepath, size in _iter_files(root, cancel):
        scanned += 1
        if progress is not None and scanned % 1000 == 0:
            progress(f"正在扫描… 已检查 {scanned} 个文件", scanned, 0)
        if size < min_size:
            continue
        if len(heap) < top_n:
            heapq.heappush(heap, (size, filepath))
        elif size > heap[0][0]:
            heapq.heapreplace(heap, (size, filepath))

    files = [(path, size) for size, path in sorted(heap, reverse=True)]
    return files, scanned


# -------------------------------------------------------------- 重复文件查找

@dataclass
class DupGroup:
    size: int
    files: list[str]

    @property
    def wasted_bytes(self) -> int:
        """去掉一份后可以释放的空间。"""
        return self.size * (len(self.files) - 1)


def _hash_file(path: str, limit: int | None = None, chunk: int = 1024 * 1024) -> str | None:
    """计算文件哈希; limit 指定只读取前若干字节; 读取失败返回 None。"""
    digest = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as handle:
            remaining = limit
            while True:
                want = chunk if remaining is None else min(chunk, remaining)
                if want <= 0:
                    break
                block = handle.read(want)
                if not block:
                    break
                digest.update(block)
                if remaining is not None:
                    remaining -= len(block)
    except OSError:
        return None
    return digest.hexdigest()


# 并行计算哈希时的线程数: 读盘属于 I/O 密集且会释放 GIL, 用线程池可显著提速。
HASH_WORKERS = min(8, os.cpu_count() or 4)


def _hash_files(paths: list[str], limit: int | None, cancel: CancelCheck | None,
                progress: ProgressFn | None, label: str) -> dict[str, str | None]:
    """并行计算一批文件的哈希, 返回 {路径: 哈希或 None}。"""
    results: dict[str, str | None] = {}
    if not paths:
        return results
    total = len(paths)
    with ThreadPoolExecutor(max_workers=HASH_WORKERS) as pool:
        future_to_path = {pool.submit(_hash_file, path, limit): path for path in paths}
        for index, future in enumerate(as_completed(future_to_path), start=1):
            if cancel is not None and cancel():
                for pending in future_to_path:
                    pending.cancel()
                break
            results[future_to_path[future]] = future.result()
            if progress is not None and index % 200 == 0:
                progress(f"{label}… {index}/{total}", index, total)
    return results


def find_duplicates(
    root: str,
    min_size: int = 1024 * 1024,
    cancel: CancelCheck | None = None,
    progress: ProgressFn | None = None,
) -> list[DupGroup]:
    """查找 root 下内容完全相同的文件。

    分三阶段执行以尽量少读磁盘: 先按大小分组, 再比对头部 64KB, 最后完整校验。
    """
    # 阶段一: 按文件大小分组, 只保留可能重复的
    size_map: dict[int, list[str]] = {}
    scanned = 0
    for filepath, size in _iter_files(root, cancel):
        scanned += 1
        if progress is not None and scanned % 2000 == 0:
            progress(f"阶段 1/3 按大小分组… 已扫描 {scanned} 个文件", scanned, 0)
        if size < min_size:
            continue
        size_map.setdefault(size, []).append(filepath)

    candidates = [(size, files) for size, files in size_map.items() if len(files) > 1]
    if not candidates:
        return []

    # 阶段二: 并行比对前 64KB, 快速排除
    size_of = {path: size for size, files in candidates for path in files}
    heads = _hash_files(list(size_of), 65536, cancel, progress, "阶段 2/3 快速比对")
    if cancel is not None and cancel():
        return []
    head_groups: dict[tuple[int, str], list[str]] = {}
    for path, head in heads.items():
        if head is not None:
            head_groups.setdefault((size_of[path], head), []).append(path)

    refined = [(key, files) for key, files in head_groups.items() if len(files) > 1]
    if not refined:
        return []

    # 阶段三: 并行做完整内容校验
    size_of_full = {path: key[0] for key, files in refined for path in files}
    full_hashes = _hash_files(list(size_of_full), None, cancel, progress, "阶段 3/3 完整校验")
    if cancel is not None and cancel():
        return []
    final: dict[tuple[int, str], list[str]] = {}
    for path, full in full_hashes.items():
        if full is not None:
            final.setdefault((size_of_full[path], full), []).append(path)

    groups = [DupGroup(size=key[0], files=files) for key, files in final.items() if len(files) > 1]
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def cancel_event_checker(event: "threading.Event") -> CancelCheck:
    """把 threading.Event 包装成取消检查函数。"""
    return event.is_set
