# -*- coding: utf-8 -*-
# ============================================================
# 文件名称：test_analyzer.py
# 文件用途：
#   使用模拟数据测试 analyzer.py 的资源判断规则。
#   不执行采集脚本，不改变真实 CPU、内存和磁盘占用。
#
# 本次验证：
#   1. 使用率达到 warn、fail 时，状态是否正确。
#   2. 缺失值、非法文本等是否会被判为 FAIL。
#   3. 采集失败是否能保留失败原因。
#   4. 磁盘是否逐个判断，是否会发现根目录缺失。
#   5. 整体状态是否遵守 FAIL > WARN > PASS。
#
# 主要变量：
#   PROJECT_ROOT   项目根目录
#   rule           测试使用的阈值，不修改真实配置文件
#   cases          多组测试输入及预期结果
#   raw_value      模拟的使用率，例如 "70"
#   expected       预期状态，例如 WARN
#   result         被测试函数返回的结果
#   collected      模拟 collector 返回的采集结果
#   checks         多条检查结果组成的列表
#   by_target      按检查对象整理的结果，方便查找 / 和 /boot
#
# 使用工具：
#   unittest 是 Python 标准库，不需要安装额外依赖。
#
# 退出码：
#   0：测试全部通过
#   非零：有测试失败或发生错误
# ============================================================


# ---------- 1. 导入测试工具 ----------
import sys
import unittest
from pathlib import Path


# ---------- 2. 告诉 Python 去哪里找 analyzer.py ----------
# 当前文件在 tests 目录中，向上两级得到项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 把 src 加入本次 Python 进程的模块查找路径。
# 这不会修改系统环境变量，也不会移动任何文件。
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from analyzer import (
    analyze_disk,
    analyze_single,
    check_usage,
    make_check,
    summarize,
)


# ---------- 3. 定义一组资源分析测试 ----------
# 继承 unittest.TestCase，便于使用 assertEqual 等检查方法。
# 方法名以 test_ 开头，unittest 就会自动运行它。
class ResourceAnalyzerTests(unittest.TestCase):

    # ---------- 3.1 验证两个阈值的边界 ----------
    def test_usage_boundaries(self):
        """低于、等于和超过阈值时，状态应正确。"""
        rule = {"warn": 70, "fail": 90}

        # 每组包含：输入值、预期状态。
        # 重点检查等于 70 和等于 90 的情况。
        cases = [
            ("0", "PASS"),
            ("69.99", "PASS"),
            ("70", "WARN"),
            ("89.99", "WARN"),
            ("90", "FAIL"),
            ("100", "FAIL"),
        ]

        for raw_value, expected in cases:
            # subTest 为每组数据加上标记。
            # 某一组失败后，仍会继续检查其他组。
            with self.subTest(value=raw_value):
                result = check_usage(
                    "cpu", "CPU", raw_value, rule
                )

                # 比较实际状态和预期状态。
                # 不一致时，unittest 会报告测试失败。
                self.assertEqual(result["status"], expected)

    # ---------- 3.2 验证无效使用率 ----------
    def test_invalid_usage(self):
        """无效输入必须失败，不能替换为零后判 PASS。"""
        rule = {"warn": 70, "fail": 90}

        cases = [
            None,       # 字段缺失。
            "",         # 空字符串。
            "abc",      # 不能转换为数字的文本。
            "-1",       # 负使用率。
            "101",      # CPU 总体使用率超过 100。
            "nan",      # 非正常数值。
            "inf",      # 无穷大。
            True,       # 布尔值，不能当作使用率。
        ]

        for raw_value in cases:
            with self.subTest(value=raw_value):
                result = check_usage(
                    "cpu", "CPU", raw_value, rule
                )

                self.assertEqual(result["status"], "FAIL")

                # 取不到有效使用率时，value 应为 None。
                # 不能用 0 冒充真实数据。
                self.assertIsNone(result["value"])

    # ---------- 3.3 验证采集失败 ----------
    def test_collection_failure(self):
        """Shell 采集失败时，分析结果也必须明确失败。"""
        collected = {
            "ok": False,
            "data": {},
            "error": "脚本执行失败",
            "stderr": "Permission denied",
        }

        result = analyze_single(
            collected,
            "cpu",
            "CPU",
            "cpu_usage_percent",
            {"warn": 70, "fail": 90},
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertIsNone(result["value"])

        # 原始错误信息应保留，方便之后排查原因。
        self.assertIn("Permission denied", result["reason"])

    # ---------- 3.4 验证关键字段缺失 ----------
    def test_missing_usage_field(self):
        """即使采集标记成功，缺少使用率也不能判 PASS。"""
        collected = {
            "ok": True,
            "data": {"cpu_cores": "8"},
        }

        result = analyze_single(
            collected,
            "cpu",
            "CPU",
            "cpu_usage_percent",
            {"warn": 70, "fail": 90},
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertIsNone(result["value"])

    # ---------- 3.5 验证多个磁盘分别判断 ----------
    def test_disk_thresholds(self):
        """根目录达到警告阈值，boot 达到失败阈值。"""
        collected = {
            "ok": True,
            "data": {
                "disk_count": "2",
                "disk_1_mount": "/",
                "disk_1_usage_percent": "75",
                "disk_2_mount": "/boot",
                "disk_2_usage_percent": "90",
            },
        }

        checks = analyze_disk(
            collected, {"warn": 75, "fail": 90}
        )

        # 两块文件系统应得到两条结果。
        self.assertEqual(len(checks), 2)

        # 按挂载点查找，避免测试依赖结果排列顺序。
        by_target = {}
        for item in checks:
            by_target[item["target"]] = item

        self.assertEqual(by_target["/"]["status"], "WARN")
        self.assertEqual(by_target["/boot"]["status"], "FAIL")
        self.assertEqual(summarize(checks), "FAIL")

    # ---------- 3.6 验证根目录不能遗漏 ----------
    def test_missing_root_disk(self):
        """只检查到 boot 时，应额外报告根目录缺失。"""
        collected = {
            "ok": True,
            "data": {
                "disk_count": "1",
                "disk_1_mount": "/boot",
                "disk_1_usage_percent": "18",
            },
        }

        checks = analyze_disk(
            collected, {"warn": 75, "fail": 90}
        )

        by_target = {}
        for item in checks:
            by_target[item["target"]] = item

        self.assertEqual(by_target["/boot"]["status"], "PASS")
        self.assertEqual(by_target["/"]["status"], "FAIL")
        self.assertIsNone(by_target["/"]["value"])

    # ---------- 3.7 验证磁盘数量与数据一致 ----------
    def test_incomplete_disk_data(self):
        """声明有两块文件系统，却只返回一块，必须报错。"""
        collected = {
            "ok": True,
            "data": {
                "disk_count": "2",
                "disk_1_mount": "/",
                "disk_1_usage_percent": "14",
            },
        }

        checks = analyze_disk(
            collected, {"warn": 75, "fail": 90}
        )

        self.assertEqual(summarize(checks), "FAIL")
        self.assertIsNone(checks[0]["value"])

    # ---------- 3.8 验证整体状态优先级 ----------
    def test_overall_status(self):
        """有 FAIL 优先 FAIL；没有结果也不能判 PASS。"""
        cases = [
            (["PASS", "PASS"], "PASS"),
            (["PASS", "WARN"], "WARN"),
            (["WARN", "FAIL", "PASS"], "FAIL"),
            ([], "FAIL"),
        ]

        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                checks = []

                # 这里只测试汇总，因此人工构造检查结果。
                for status in statuses:
                    checks.append(
                        make_check(
                            "test", "模拟检查项", status, None,
                            "仅用于验证状态汇总",
                        )
                    )

                self.assertEqual(summarize(checks), expected)


# ---------- 4. 直接执行文件时运行全部测试 ----------
# verbosity=2 会逐项显示测试名称和结果。
if __name__ == "__main__":
    unittest.main(verbosity=2)