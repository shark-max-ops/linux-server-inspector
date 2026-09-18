# -*- coding: utf-8 -*-

# 文件用途：
# 使用临时文件测试清理工具，确认过期判断、文件名限制、
# 符号链接保护以及删除前的文件变化检查。
# 不读取或删除项目中的真实报告和运行日志。

"""
整体执行流程：
开始
  ↓
导入清理工具中的扫描和删除函数
  ↓
为每个测试创建独立的临时目录
  ↓
创建模拟报告、日志或符号链接，设置文件修改时间
  ↓
执行对应测试
  ├─ 过期且名称正确 → 应被选中
  ├─ 未过期或刚好到分界时间 → 应保留
  ├─ 名称不符合规则 → 应保留
  ├─ 子目录中的文件或符号链接 → 应跳过
  ├─ 只扫描 → 不应删除文件
  ├─ 扫描后文件被修改 → 不应删除
  └─ 文件已被其他操作删除 → 应正常跳过
  ↓
比较实际结果和预期结果
  ├─ 一致 → 测试通过
  └─ 不一致 → 测试失败
  ↓
自动清理本次测试使用的临时目录
  ↓
打印测试结果
  ↓
返回退出码：全部通过为 0，否则为非 0
"""

# 主要变量：
# PROJECT_ROOT：项目根目录。
# self.temp_dir：管理临时目录的对象。
# self.root：临时目录路径。
# self.cutoff：测试使用的过期分界时间。
# self.directories：需要扫描的临时目录和文件名规则。
# REPORT_NAME：符合正式报告命名规则的模拟文件名。
# RUN_NAME：符合单次日志命名规则的模拟文件名。
# candidates：扫描得到的待清理文件列表。

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


# ---------- 1. 导入项目中的清理函数 ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from cleanup import (
    REPORT_PATTERN,
    RUN_PATTERN,
    delete_candidates,
    delete_candidates_detailed,
    find_candidates,
)


# ---------- 2. 准备符合规则的模拟文件名 ----------

REPORT_NAME = (
    "inspection_20260901_090000_"
    "0123456789abcdef0123456789abcdef.txt"
)

RUN_NAME = "run_20260901_090000_Ab12Cd.log"


class CleanupTests(unittest.TestCase):
    """在独立临时目录中验证清理行为。"""

    # ---------- 3. 每个测试都重新准备目录 ----------

    def setUp(self):
        """创建临时目录，并设置固定的时间分界。"""

        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="inspector_cleanup_test_"
        )

        # 即使测试中途失败，也会执行临时目录清理。
        self.addCleanup(self.temp_dir.cleanup)

        self.root = Path(self.temp_dir.name)

        # 使用固定时间，避免测试结果受真实日期变化影响。
        self.cutoff = 1700000000

        # 一个临时目录中同时测试两种文件名规则。
        self.directories = [
            (self.root, REPORT_PATTERN),
            (self.root, RUN_PATTERN),
        ]

    def create_file(self, name, modified_at):
        """创建模拟文件，并设置它的最后修改时间。"""

        path = self.root / name
        path.write_text("模拟巡检内容\n", encoding="utf-8")

        # os.utime 的两个时间分别是访问时间和修改时间。
        os.utime(str(path), (modified_at, modified_at))

        return path

    def scan(self):
        """按固定的分界时间扫描临时目录。"""

        return find_candidates(self.directories, self.cutoff)

    def delete_quietly(self, candidates):
        """执行删除，但不让普通提示打乱测试结果的排版。"""

        with redirect_stdout(io.StringIO()):
            return delete_candidates(candidates, self.cutoff)

    # ---------- 4. 验证过期文件和预览行为 ----------

    def test_select_expired_files(self):
        """过期且命名正确的报告和运行日志应被选中。"""

        report = self.create_file(
            REPORT_NAME, self.cutoff - 100
        )
        run_log = self.create_file(
            RUN_NAME, self.cutoff - 50
        )

        candidates = self.scan()
        paths = {path for path, file_stat in candidates}

        self.assertEqual(paths, {report, run_log})

    def test_scan_does_not_delete(self):
        """扫描只返回候选文件，不应删除或改写文件。"""

        report = self.create_file(
            REPORT_NAME, self.cutoff - 100
        )

        candidates = self.scan()

        self.assertEqual(len(candidates), 1)
        self.assertTrue(report.exists())
        self.assertEqual(
            report.read_text(encoding="utf-8"),
            "模拟巡检内容\n",
        )

    # ---------- 5. 验证保留期限边界 ----------

    def test_keep_recent_file(self):
        """修改时间晚于过期分界的文件应保留。"""

        report = self.create_file(
            REPORT_NAME, self.cutoff + 100
        )

        self.assertEqual(self.scan(), [])
        self.assertTrue(report.exists())

    def test_keep_boundary_file(self):
        """修改时间刚好等于分界时间时，不应删除。"""

        report = self.create_file(
            REPORT_NAME, self.cutoff
        )

        self.assertEqual(self.scan(), [])
        self.assertTrue(report.exists())

    # ---------- 6. 验证清理范围限制 ----------

    def test_ignore_unrelated_names(self):
        """入口日志、演示报告和不完整文件名都应保留。"""

        names = [
            "cron_launcher.log",
            "demo_report_20260901.txt",
            "inspection_notes.txt",
            "run_notes.log",
            "config.yaml",
        ]

        for name in names:
            self.create_file(name, self.cutoff - 100)

        self.assertEqual(self.scan(), [])

        for name in names:
            self.assertTrue((self.root / name).exists())

    def test_ignore_subdirectories(self):
        """不应进入子目录，也不应把目录当普通文件处理。"""

        # 即使目录名字符合报告规则，也不能选中它。
        matching_directory = self.root / REPORT_NAME
        matching_directory.mkdir()

        # 子目录里的过期日志也不在本次扫描范围内。
        nested_log = matching_directory / RUN_NAME
        nested_log.write_text("子目录内容", encoding="utf-8")
        os.utime(
            str(nested_log),
            (self.cutoff - 100, self.cutoff - 100),
        )

        self.assertEqual(self.scan(), [])
        self.assertTrue(nested_log.exists())

    def test_ignore_symlink(self):
        """符合命名规则的符号链接也应跳过。"""

        target = self.create_file(
            "original.txt", self.cutoff - 100
        )

        link = self.root / REPORT_NAME

        try:
            link.symlink_to(target)
        except OSError as exc:
            # Windows 开发端可能未授予创建符号链接的权限。
            # 目标 Linux 仍会正常执行本测试，不把权限限制误报为代码失败。
            if os.name == "nt":
                self.skipTest("Windows 当前账户不能创建符号链接：{}".format(exc))
            raise

        self.assertEqual(self.scan(), [])
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())

    # ---------- 7. 验证实际删除及删除前复查 ----------

    def test_delete_expired_file(self):
        """实际执行删除时，只删除选中的过期文件。"""

        expired = self.create_file(
            REPORT_NAME, self.cutoff - 100
        )
        recent = self.create_file(
            RUN_NAME, self.cutoff + 100
        )

        deleted_count, error_count = self.delete_quietly(
            self.scan()
        )

        self.assertEqual(deleted_count, 1)
        self.assertEqual(error_count, 0)
        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())

    def test_delete_summary_contains_actual_bytes_and_skips(self):
        """详细统计应区分实际释放空间和扫描后跳过的文件。"""

        deleted = self.create_file(REPORT_NAME, self.cutoff - 100)
        skipped = self.create_file(RUN_NAME, self.cutoff - 100)
        candidates = self.scan()
        deleted_size = deleted.stat().st_size
        skipped.unlink()

        with redirect_stdout(io.StringIO()):
            summary = delete_candidates_detailed(candidates, self.cutoff)

        self.assertEqual(summary["deleted_count"], 1)
        self.assertEqual(summary["deleted_bytes"], deleted_size)
        self.assertEqual(summary["skipped_count"], 1)
        self.assertEqual(summary["error_count"], 0)

    def test_keep_changed_file(self):
        """扫描后被修改的文件，即使仍然过期也应跳过。"""

        report = self.create_file(
            REPORT_NAME, self.cutoff - 100
        )
        candidates = self.scan()

        # 扫描之后增加内容，让文件大小发生变化。
        report.write_text(
            "扫描以后新增的内容，比原来的内容更长。\n",
            encoding="utf-8",
        )

        # 仍设为过期，验证的是“文件变化检查”，而不是期限检查。
        os.utime(
            str(report),
            (self.cutoff - 100, self.cutoff - 100),
        )

        deleted_count, error_count = self.delete_quietly(
            candidates
        )

        self.assertEqual(deleted_count, 0)
        self.assertEqual(error_count, 0)
        self.assertTrue(report.exists())

    def test_missing_file_is_skipped(self):
        """扫描后已不存在的文件应跳过，不应导致程序失败。"""

        report = self.create_file(
            REPORT_NAME, self.cutoff - 100
        )
        candidates = self.scan()

        # 模拟文件被其他操作提前删除。
        report.unlink()

        deleted_count, error_count = self.delete_quietly(
            candidates
        )

        self.assertEqual(deleted_count, 0)
        self.assertEqual(error_count, 0)


# ---------- 8. 启动测试 ----------

if __name__ == "__main__":
    unittest.main(verbosity=2)
