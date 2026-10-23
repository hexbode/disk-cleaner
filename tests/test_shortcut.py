"""开始菜单快捷方式(.lnk)解析的单元测试。

测试数据按 Shell Link 二进制格式手工构造, 不依赖系统里真实的快捷方式。
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cleaner_core as core  # noqa: E402


def build_link_info(target: str) -> bytes:
    """构造带 Unicode LocalBasePath 的 LinkInfo 结构。"""
    raw = target.encode("utf-16-le") + b"\x00\x00"
    info = bytearray(0x24 + len(raw))
    struct.pack_into("<I", info, 0, len(info))   # LinkInfoSize
    struct.pack_into("<I", info, 4, 0x24)        # LinkInfoHeaderSize
    struct.pack_into("<I", info, 8, 1)           # VolumeIDAndLocalBasePath
    struct.pack_into("<I", info, 0x1C, 0x24)     # LocalBasePathOffsetUnicode
    info[0x24:] = raw
    return bytes(info)


def build_lnk(target: str | None = None, relative: str | None = None) -> bytes:
    """构造一个最小的 .lnk 文件内容。"""
    flags = 0x80  # IsUnicode
    body = b""
    if target is not None:
        flags |= 0x02  # HasLinkInfo
        body += build_link_info(target)
    if relative is not None:
        flags |= 0x08  # HasRelativePath
        body += struct.pack("<H", len(relative)) + relative.encode("utf-16-le")
    header = bytearray(0x4C)
    struct.pack_into("<I", header, 0, 0x4C)      # HeaderSize
    struct.pack_into("<I", header, 0x14, flags)  # LinkFlags
    return bytes(header) + body


class ShortcutParseTest(unittest.TestCase):
    def test_link_info_target(self):
        data = build_lnk(target=r"C:\Games\Demo\app.exe")
        self.assertEqual(core._parse_shortcut(data, r"C:\Shortcuts"),
                         r"C:\Games\Demo")

    def test_relative_target_uses_shortcut_dir(self):
        data = build_lnk(relative=r"App\app.exe")
        self.assertEqual(core._parse_shortcut(data, r"C:\Shortcuts"),
                         r"C:\Shortcuts\App")

    def test_working_directory_is_used_as_fallback(self):
        header = bytearray(0x4C)
        struct.pack_into("<I", header, 0, 0x4C)
        struct.pack_into("<I", header, 0x14, 0x80 | 0x10)  # IsUnicode | HasWorkingDir
        working = r"D:\Tools\Portable"
        body = struct.pack("<H", len(working)) + working.encode("utf-16-le")
        self.assertEqual(core._parse_shortcut(bytes(header) + body, r"C:\Shortcuts"),
                         working)

    def test_invalid_content_returns_none(self):
        self.assertIsNone(core._parse_shortcut(b"", r"C:\Shortcuts"))
        self.assertIsNone(core._parse_shortcut(b"not a shortcut", r"C:\Shortcuts"))
        self.assertIsNone(core._parse_shortcut(b"L\x00\x00\x00", r"C:\Shortcuts"))

    def test_truncated_link_info_returns_none(self):
        data = build_lnk(target=r"C:\Games\Demo\app.exe")[:0x50]
        self.assertIsNone(core._parse_shortcut(data, r"C:\Shortcuts"))

    def test_reads_target_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "demo.lnk")
            with open(path, "wb") as handle:
                handle.write(build_lnk(target=r"C:\Games\Demo\app.exe"))
            self.assertEqual(core._shortcut_target(path), r"C:\Games\Demo")

    def test_missing_file_returns_none(self):
        self.assertIsNone(core._shortcut_target(r"C:\no\such\shortcut.lnk"))


class UsableDirTest(unittest.TestCase):
    def test_rejects_broad_paths(self):
        self.assertIsNone(core._usable_install_dir("C:\\"))
        self.assertIsNone(core._usable_install_dir(r"C:\Windows"))
        self.assertIsNone(core._usable_install_dir(r"C:\ProgramData"))
        self.assertIsNone(core._usable_install_dir(r"C:\Users"))
        self.assertIsNone(core._usable_install_dir(r"C:\missing"))

    def test_accepts_deep_existing_dir(self):
        with mock.patch.object(core.os.path, "isdir", return_value=True):
            self.assertIsNotNone(core._usable_install_dir(r"D:\Apps\Demo"))

    def test_rejects_temp_extraction_dirs(self):
        with mock.patch.object(core.os.path, "isdir", return_value=True):
            self.assertIsNone(core._usable_install_dir(
                r"C:\Users\someone\AppData\Local\Temp\installer"))


if __name__ == "__main__":
    unittest.main()
