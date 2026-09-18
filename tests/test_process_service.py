# -*- coding: utf-8 -*-
# ============================================================
# 文件名称：test_process_service.py
#
# 文件用途：
#   验证进程、服务分析在正常和异常情况下的判断。
#   只调用分析函数，不执行 Shell，不启停真实服务。
#
# 主要变量：
#   PROJECT_ROOT   项目根目录
#   collected      模拟 collector 返回的完整结果
#   data           模拟的采集字段
#   checks         分析函数返回的检查结果列表
#   cases          多组测试输入及预期状态
#   load / active / sub  模拟服务的三种运行状态
#   expected       本组测试应得到的状态
#
# 测试范围：
#   1. 进程存在与不存在。
#   2. 进程字段矛盾或缺失。
#   3. 返回记录与要求检查的对象不一致。
#   4. 服务正常、停止、失败、未找到或被屏蔽。
#   5. 采集失败。
#   6. 未配置检查对象时，不伪造 PASS。
#
# 运行环境：
#   Python 3.6，使用标准库 unittest。
# ============================================================


# ---------- 1. 导入工具并定位源码 ----------
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 让测试文件能够找到 src 中的 analyzer.py。
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from analyzer import analyze_process, analyze_service


# ---------- 2. 准备一份正常的进程采集结果 ----------
def process_sample():
    """每次调用都生成新的字典，避免测试互相影响。"""
    return {
        "ok": True,
        "data": {
            "required_count": "1",
            "required_1_name": "sshd",
            "required_1_count": "3",
            "required_1_present": "1",
        },
    }


# ---------- 3. 准备一份正常的服务采集结果 ----------
def service_sample():
    """模拟 sshd 已加载并正在运行。"""
    return {
        "ok": True,
        "data": {
            "service_count": "1",
            "service_1_name": "sshd.service",
            "service_1_load_state": "loaded",
            "service_1_active_state": "active",
            "service_1_sub_state": "running",
            "service_1_unit_file_state": "enabled",
        },
    }


# ---------- 4. 定义进程与服务测试 ----------
class ProcessServiceTests(unittest.TestCase):

    # ---------- 4.1 检查进程存在和不存在 ----------
    def test_process_presence(self):
        """数量大于零应 PASS，数量为零应 FAIL。"""
        cases = [
            ("3", "1", "PASS"),
            ("0", "0", "FAIL"),
        ]

        for count, present, expected in cases:
            with self.subTest(count=count, present=present):
                collected = process_sample()
                data = collected["data"]

                data["required_1_count"] = count
                data["required_1_present"] = present

                checks = analyze_process(collected, ["sshd"])

                self.assertEqual(len(checks), 1)
                self.assertEqual(checks[0]["status"], expected)

                # 没找到进程时，实际值应保留为 0。
                # 它与“没有有效数据”的 None 不同。
                self.assertEqual(checks[0]["value"], int(count))

    # ---------- 4.2 检查矛盾数据和缺失字段 ----------
    def test_invalid_process_data(self):
        """不能把矛盾、缺失或非法的进程数据判为正常。"""
        cases = [
            ("0", "1"),     # 数量为零，却声称存在。
            ("3", "0"),     # 数量为三，却声称不存在。
            ("abc", "1"),   # 数量不是整数。
            ("3", None),    # 缺少 present 字段。
        ]

        for count, present in cases:
            with self.subTest(count=count, present=present):
                collected = process_sample()
                data = collected["data"]
                data["required_1_count"] = count

                if present is None:
                    del data["required_1_present"]
                else:
                    data["required_1_present"] = present

                checks = analyze_process(collected, ["sshd"])

                self.assertEqual(checks[0]["status"], "FAIL")
                self.assertIsNone(checks[0]["value"])

    # ---------- 4.3 检查返回记录是否对应正确对象 ----------
    def test_record_mismatch(self):
        """返回的名字或数量不对时，不能套用其他对象的结果。"""

        # 要求 sshd，却收到 crond 的记录。
        collected = process_sample()
        collected["data"]["required_1_name"] = "crond"

        checks = analyze_process(collected, ["sshd"])
        self.assertEqual(checks[0]["status"], "FAIL")

        # 要求检查两个进程，却只返回一组。
        collected = process_sample()

        checks = analyze_process(collected, ["sshd", "crond"])
        self.assertEqual(len(checks), 2)

        for item in checks:
            self.assertEqual(item["status"], "FAIL")

        # 服务名称不匹配也必须失败。
        collected = service_sample()
        collected["data"]["service_1_name"] = "crond.service"

        checks = analyze_service(collected, ["sshd"])
        self.assertEqual(checks[0]["status"], "FAIL")

    # ---------- 4.4 检查不同服务状态 ----------
    def test_service_states(self):
        """正常、停止、失败和不存在都应得到对应结果。"""
        cases = [
            ("loaded", "active", "running", "PASS"),
            ("loaded", "active", "exited", "PASS"),
            ("loaded", "inactive", "dead", "FAIL"),
            ("loaded", "failed", "failed", "FAIL"),
            ("not-found", "inactive", "dead", "FAIL"),
            ("masked", "inactive", "dead", "FAIL"),
            ("loaded", "active", "unknown", "FAIL"),
        ]

        for load, active, sub, expected in cases:
            with self.subTest(load=load, active=active, sub=sub):
                collected = service_sample()
                data = collected["data"]

                data["service_1_load_state"] = load
                data["service_1_active_state"] = active
                data["service_1_sub_state"] = sub

                checks = analyze_service(collected, ["sshd"])

                self.assertEqual(checks[0]["status"], expected)

        # active/exited 这个测试是在验证通用服务规则：
        # 某些一次性服务可以正常处于这种状态。
        # 并不是说真实的 SSH 守护进程应该处于 exited。

    # ---------- 4.5 检查开机启动状态不影响当前规则 ----------
    def test_disabled_but_active(self):
        """正在运行但未启用开机启动，当前规则仍应 PASS。"""
        collected = service_sample()
        collected["data"]["service_1_unit_file_state"] = "disabled"

        checks = analyze_service(collected, ["sshd"])

        self.assertEqual(checks[0]["status"], "PASS")

        # 仍需保留 disabled，方便用户从报告中了解实际配置。
        self.assertEqual(
            checks[0]["value"]["unit_file_state"],
            "disabled",
        )

    # ---------- 4.6 检查服务字段缺失 ----------
    def test_missing_service_field(self):
        """缺少活动状态时，不能猜测服务正在运行。"""
        collected = service_sample()
        del collected["data"]["service_1_active_state"]

        checks = analyze_service(collected, ["sshd"])

        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertIsNone(checks[0]["value"])

    # ---------- 4.7 检查采集失败 ----------
    def test_collection_failure(self):
        """查询失败时，每个要求检查的对象都应报告失败。"""
        collected = {
            "ok": False,
            "data": {},
            "error": "模拟查询失败",
            "stderr": "Permission denied",
        }

        # 同一份失败结果分别交给进程分析和服务分析。
        for analyze in (analyze_process, analyze_service):
            with self.subTest(function=analyze.__name__):
                checks = analyze(collected, ["sshd", "crond"])

                self.assertEqual(len(checks), 2)

                for item in checks:
                    self.assertEqual(item["status"], "FAIL")
                    self.assertIsNone(item["value"])
                    self.assertIn(
                        "Permission denied", item["reason"]
                    )

    # ---------- 4.8 检查空名单 ----------
    def test_empty_configuration(self):
        """没有指定对象时返回空列表，不添加虚假的通过记录。"""
        self.assertEqual(
            analyze_process(process_sample(), []),
            [],
        )
        self.assertEqual(
            analyze_service(service_sample(), []),
            [],
        )


# ---------- 5. 直接运行时启动测试 ----------
# unittest 会运行所有以 test_ 开头的方法。
if __name__ == "__main__":
    unittest.main(verbosity=2)