"""cleaner_core 的单元测试。

所有测试都在系统临时目录中进行, 不会触碰真实文件, 也不会真的清空回收站。
运行方式:
    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cleaner_core as core  # noqa: E402


def make_file(root: str, name: str, content: bytes = b"x" * 10) -> str:
    """在 root 下创建文件(自动建子目录), 返回完整路径。"""
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


class HumanSizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(core.human_size(0), "0 B")
        self.assertEqual(core.human_size(1023), "1023 B")
        self.assertEqual(core.human_size(1024), "1.0 KB")
        self.assertEqual(core.human_size(1024 ** 3), "1.0 GB")

    def test_total_size_skips_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_file(tmp, "a.bin", b"12345")
            missing = os.path.join(tmp, "missing.bin")
            self.assertEqual(core.total_size([path, missing]), 5)


class RiskTest(unittest.TestCase):
    def setUp(self):
        # 隔离真实注册表与运行中的进程, 避免本机环境影响判定结果
        self._patch_installed = mock.patch.object(
            core, "installed_program_dirs", return_value=())
        self._patch_running = mock.patch.object(
            core, "running_program_dirs", return_value=())
        self._patch_installed.start()
        self._patch_running.start()

    def tearDown(self):
        self._patch_installed.stop()
        self._patch_running.stop()

    def test_extensions(self):
        self.assertEqual(core.deletion_risk(r"C:\data\a.db"), "数据文件")
        self.assertEqual(core.deletion_risk(r"C:\data\a.dll"), "程序文件")
        self.assertEqual(core.deletion_risk(r"C:\data\a.vhdx"), "虚拟机磁盘")
        self.assertEqual(core.deletion_risk(r"C:\data\a.exe"), "程序文件")
        self.assertIsNone(core.deletion_risk(r"C:\data\a.txt"))

    def test_installer_is_not_protected(self):
        self.assertIsNone(core.deletion_risk(r"C:\data\setup.exe"))
        self.assertIsNone(core.deletion_risk(r"C:\data\app-install.exe"))

    def test_program_directories(self):
        self.assertEqual(
            core.deletion_risk(r"C:\Program Files\App\a.dat"), "程序文件")

    def test_system_directories(self):
        self.assertEqual(core.deletion_risk(r"C:\Windows\System32\config\SYSTEM"), "程序文件")
        self.assertEqual(core.deletion_risk(r"C:\Windows\WinSxS\abc.dat"), "程序文件")
        self.assertEqual(core.deletion_risk(r"C:\Windows\Installer\abc.msi"), "程序文件")
        self.assertEqual(core.deletion_risk(r"C:\Windows\SystemApps\abc.dll"), "程序文件")
        self.assertEqual(core.deletion_risk(r"C:\ProgramData\Package Cache\x.dat"), "程序文件")
        self.assertEqual(core.deletion_risk(r"C:\ProgramData\Microsoft\Windows Defender\x.dat"),
                         "程序文件")
        self.assertEqual(core.deletion_risk(r"C:\Windows\Fonts\a.ttf"), "程序文件")

    def test_cleanable_system_dirs_are_not_protected(self):
        self.assertIsNone(core.deletion_risk(r"C:\Windows\Temp\a.txt"))
        self.assertIsNone(core.deletion_risk(r"C:\Windows\Prefetch\APP.pf"))
        self.assertIsNone(
            core.deletion_risk(r"C:\Windows\SoftwareDistribution\Download\a.cab"))

    def test_more_data_and_disk_types(self):
        self.assertEqual(core.deletion_risk(r"D:\mail\archive.pst"), "数据文件")
        self.assertEqual(core.deletion_risk(r"D:\sql\db.mdf"), "数据文件")
        self.assertEqual(core.deletion_risk(r"D:\vm\disk.vmdk"), "虚拟机磁盘")
        self.assertEqual(core.deletion_risk(r"D:\vm\disk.qcow2"), "虚拟机磁盘")

    def test_notices_do_not_block(self):
        cases = (
            (r"C:\Downloads\setup.msi", "安装包"),
            (r"C:\logs\app.log", "日志文件"),
            (r"D:\backup\old.bak", "备份文件"),
            (r"D:\iso\win.iso", "光盘镜像"),
            (r"D:\mail\cache.ost", "邮件缓存"),
        )
        for path, kind in cases:
            self.assertIsNone(core.deletion_risk(path), path)
            self.assertEqual(core.deletion_notice(path), kind, path)
        self.assertIsNone(core.deletion_notice(r"C:\Downloads\video.mp4"))


class InstalledDirTest(unittest.TestCase):
    def test_installed_directory_is_protected(self):
        root = os.path.normcase(os.path.abspath(r"D:\Games\Demo"))
        with mock.patch.object(core, "installed_program_dirs", return_value=(root,)):
            self.assertEqual(core.deletion_risk(r"D:\Games\Demo\data.bin"), "程序文件")
            # 同前缀的相邻目录不能被误判
            self.assertIsNone(core.deletion_risk(r"D:\Games\Demo2\data.bin"))

    def test_lists_normalized_absolute_dirs(self):
        for path in core.installed_program_dirs(force=True):
            self.assertTrue(os.path.isabs(path))
            self.assertEqual(path, os.path.normcase(path))


class RunningDirTest(unittest.TestCase):
    def test_running_program_dir_is_protected(self):
        root = os.path.normcase(os.path.abspath(r"D:\Portable\App"))
        with mock.patch.object(core, "installed_program_dirs", return_value=()), \
                mock.patch.object(core, "running_program_dirs", return_value=(root,)):
            self.assertEqual(core.deletion_risk(r"D:\Portable\App\data.bin"), "程序文件")
            self.assertIsNone(core.deletion_risk(r"D:\Portable\Other\data.bin"))


class DangerousDirTest(unittest.TestCase):
    def test_rejects_roots_and_system_dirs(self):
        self.assertIsNotNone(core.is_dangerous_dir("C:\\"))
        self.assertIsNotNone(core.is_dangerous_dir(r"C:\Windows"))
        self.assertIsNotNone(core.is_dangerous_dir(r"C:\Windows\Temp"))
        self.assertIsNotNone(core.is_dangerous_dir(r"C:\Program Files\App"))
        self.assertIsNotNone(core.is_dangerous_dir(r"C:\Users"))

    def test_allows_user_subdirectory(self):
        self.assertIsNone(core.is_dangerous_dir(r"C:\Users\someone\AppData\Local\Cache"))
        self.assertIsNone(core.is_dangerous_dir(r"D:\cache\wechat"))

    def test_case_insensitive(self):
        self.assertIsNotNone(core.is_dangerous_dir(r"C:\WINDOWS\Temp"))
        self.assertIsNotNone(core.is_dangerous_dir(r"C:\program files"))

    def test_rejects_unc_share_root(self):
        self.assertIsNotNone(core.is_dangerous_dir(r"\\server\share"))


class ScanCleanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        make_file(self.root, "a.tmp", b"a" * 100)
        make_file(self.root, "sub/b.log", b"b" * 200)
        self.target = core.CleanTarget(key="t", label="测试", paths=[self.root])

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_counts_files_and_bytes(self):
        with mock.patch.object(core, "loaded_module_paths", return_value=set()):
            result = core.scan_targets([self.target])
        self.assertEqual(result.file_count, 2)
        self.assertEqual(result.total_bytes, 300)
        self.assertEqual(result.deletable_bytes, 300)
        self.assertEqual(len(result.top_files), 2)
        # 最大的文件排在前面
        self.assertEqual([size for _path, size in result.top_files], [200, 100])

    def test_scan_honors_patterns(self):
        target = core.CleanTarget(key="t", label="测试", paths=[self.root],
                                  patterns=["*.tmp"])
        with mock.patch.object(core, "loaded_module_paths", return_value=set()):
            result = core.scan_targets([target])
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.total_bytes, 100)

    def test_clean_removes_files_but_keeps_root(self):
        with mock.patch.object(core, "loaded_module_paths", return_value=set()):
            result = core.clean_targets([self.target])
        self.assertEqual(result.deleted, 2)
        self.assertEqual(result.freed_bytes, 300)
        self.assertEqual(result.skipped, 0)
        self.assertTrue(os.path.isdir(self.root))
        self.assertEqual(os.listdir(self.root), [])

    def test_clean_skips_locked_files(self):
        path = make_file(self.root, "locked.dat", b"z" * 10)
        with mock.patch.object(core, "loaded_module_paths",
                               return_value={core._norm_path(path)}):
            result = core.clean_targets([self.target])
        self.assertEqual(result.blocked, 1)
        self.assertTrue(os.path.exists(path))

    def test_cancel_stops_scan(self):
        with mock.patch.object(core, "loaded_module_paths", return_value=set()):
            result = core.scan_targets([self.target], cancel=lambda: True)
        self.assertEqual(result.file_count, 0)

    def test_free_before_is_recorded(self):
        with mock.patch.object(core, "loaded_module_paths", return_value=set()):
            result = core.clean_targets([self.target])
        self.assertTrue(result.free_before)
        total, counted = core.free_space_delta(result.free_before)
        self.assertGreaterEqual(total, 0)
        self.assertEqual(counted, len(result.free_before))


class LargeFileTest(unittest.TestCase):
    def test_min_size_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "big.bin", b"x" * 100)
            make_file(tmp, "small.bin", b"x" * 10)
            files, scanned = core.scan_large_files(tmp, min_size=50)
        self.assertEqual(scanned, 2)
        self.assertEqual([os.path.basename(p) for p, _ in files], ["big.bin"])

    def test_top_n_keeps_largest(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "a.bin", b"x" * 10)
            make_file(tmp, "b.bin", b"x" * 30)
            make_file(tmp, "c.bin", b"x" * 20)
            files, _scanned = core.scan_large_files(tmp, min_size=0, top_n=2)
        self.assertEqual([os.path.basename(p) for p, _ in files],
                         ["b.bin", "c.bin"])


class DuplicateTest(unittest.TestCase):
    def test_groups_identical_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "a/one.bin", b"same")
            make_file(tmp, "b/two.bin", b"same")
            make_file(tmp, "c/other.bin", b"diff")
            groups = core.find_duplicates(tmp, min_size=1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].files), 2)
        self.assertEqual(groups[0].size, 4)
        self.assertEqual(groups[0].wasted_bytes, 4)

    def test_no_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_file(tmp, "a.bin", b"one")
            make_file(tmp, "b.bin", b"two")
            self.assertEqual(core.find_duplicates(tmp, min_size=1), [])

    def test_same_head_different_tail(self):
        """前 64KB 相同、后续内容不同, 不能被误判为重复。"""
        with tempfile.TemporaryDirectory() as tmp:
            head = b"h" * 65536
            make_file(tmp, "a.bin", head + b"tail-a")
            make_file(tmp, "b.bin", head + b"tail-b")
            self.assertEqual(core.find_duplicates(tmp, min_size=1), [])


class JunctionTest(unittest.TestCase):
    """目录联接(junction)不能被跟随, 否则会扫描/删除到目录之外。"""

    def _make_junction(self, base: str) -> tuple[str, str]:
        target = os.path.join(base, "target")
        make_file(target, "inside.txt", b"x")
        scan = os.path.join(base, "scan")
        os.makedirs(scan)
        link = os.path.join(scan, "link")
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                              capture_output=True)
        if proc.returncode != 0:
            self.skipTest("当前环境无法创建 junction")
        return scan, os.path.join(target, "inside.txt")

    def test_scan_does_not_follow_junction(self):
        with tempfile.TemporaryDirectory() as base:
            scan, _inside = self._make_junction(base)
            self.assertEqual(list(core._iter_files(scan)), [])

    def test_clean_does_not_delete_through_junction(self):
        with tempfile.TemporaryDirectory() as base:
            scan, inside = self._make_junction(base)
            target = core.CleanTarget(key="t", label="t", paths=[scan])
            with mock.patch.object(core, "loaded_module_paths", return_value=set()):
                core.clean_targets([target])
            self.assertTrue(os.path.exists(inside))


class SettingsTest(unittest.TestCase):
    def test_custom_dirs_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as config:
            cache = os.path.join(tmp, "cache")
            os.makedirs(cache)
            missing = os.path.join(tmp, "missing")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": config}):
                self.assertTrue(core.save_custom_dirs([cache, missing]))
                # 不存在的目录在读取时会被过滤
                self.assertEqual(core.load_custom_dirs(), [os.path.abspath(cache)])
                targets = core.custom_targets()
                self.assertEqual(len(targets), 1)
                self.assertTrue(targets[0].custom)
                self.assertEqual(targets[0].paths[0], os.path.abspath(cache))

                self.assertTrue(core.save_custom_dirs([]))
                self.assertEqual(core.load_custom_dirs(), [])

    def test_ignores_dangerous_entries_from_config(self):
        """手工篡改配置文件也不能把系统目录加进清理列表。"""
        with tempfile.TemporaryDirectory() as config:
            appdir = os.path.join(config, "DiskCleaner")
            os.makedirs(appdir)
            with open(os.path.join(appdir, "settings.json"), "w", encoding="utf-8") as f:
                json.dump({"custom_dirs": [r"C:\Windows\Temp", r"C:\Program Files"]}, f)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": config}):
                self.assertEqual(core.load_custom_dirs(), [])
                self.assertEqual(core.custom_targets(), [])

    def test_custom_targets_rejects_dangerous_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = core.custom_targets([r"C:\Windows\Temp", tmp])
            self.assertEqual([t.paths[0] for t in targets], [os.path.abspath(tmp)])

    def test_offline_dir_is_kept_in_config(self):
        """暂时离线的目录不应在保存时被丢弃。"""
        with tempfile.TemporaryDirectory() as config:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": config}):
                missing = r"D:\offline-cache"
                self.assertTrue(core.save_custom_dirs([missing]))
                self.assertEqual(core.load_custom_dirs(), [])
                self.assertEqual(
                    core.load_custom_dirs(include_missing=True), [missing])

    def test_save_deduplicates_paths(self):
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as config:
            cache = os.path.join(tmp, "cache")
            os.makedirs(cache)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": config}):
                core.save_custom_dirs([cache, cache, cache + os.sep])
                self.assertEqual(len(core.load_custom_dirs(include_missing=True)), 1)


class RecycleBinTest(unittest.TestCase):
    def test_empty_input_is_noop(self):
        self.assertEqual(core.move_to_recycle_bin([]), (0, 0, []))

    def test_local_fixed_drive_supports_recycle_bin(self):
        root = os.path.splitdrive(os.environ.get("SystemRoot", r"C:\Windows"))[0] + "\\"
        self.assertTrue(core.recycle_bin_supported(os.path.join(root, "x.tmp")))

    def test_unc_path_is_not_supported(self):
        self.assertFalse(core.recycle_bin_supported(r"\\server\share\file.bin"))

    def test_unsupported_filter(self):
        root = os.path.splitdrive(os.environ.get("SystemRoot", r"C:\Windows"))[0] + "\\"
        local = os.path.join(root, "a.tmp")
        remote = r"\\server\share\b.bin"
        self.assertEqual(core.unsupported_recycle_paths([local, remote]), [remote])


if __name__ == "__main__":
    unittest.main()
