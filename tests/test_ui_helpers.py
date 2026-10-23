"""界面辅助函数的单元测试(不创建窗口)。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import disk_cleaner as ui  # noqa: E402


class ParseSizeTest(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(ui.parse_size("100 MB"), 100 * 1024 ** 2)
        self.assertEqual(ui.parse_size("1 GB"), 1024 ** 3)
        self.assertEqual(ui.parse_size("500 KB"), 500 * 1024)

    def test_parse_display_size(self):
        self.assertEqual(ui.parse_display_size("1.0 KB"), 1024)
        self.assertEqual(ui.parse_display_size("1,024.0 MB"), 1024 ** 3)
        self.assertEqual(ui.parse_display_size(""), -1)
        self.assertEqual(ui.parse_display_size("abc"), -1)


class CsvCellTest(unittest.TestCase):
    def test_formula_prefixes_are_escaped(self):
        for value in ("=1+1", "+1", "-1", "@SUM(A1)", " =cmd", "\t=x"):
            self.assertTrue(ui.csv_cell(value).startswith("'"), value)

    def test_normal_text_is_unchanged(self):
        for value in ("C:\\tmp\\a.txt", "1.0 GB", "普通文件", ""):
            self.assertEqual(ui.csv_cell(value), value)


if __name__ == "__main__":
    unittest.main()
