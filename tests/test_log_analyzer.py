# -*- coding: utf-8 -*-

# 文件用途：
# 测试日志分析模块能否正确识别错误日志、SSH 认证失败、
# 异常编码和不完整的数据。
# 所有日志都由本文件构造，不读取真实日志，不修改系统配置。

"""
整体执行流程：
开始
  ↓
定位项目的 src 文件夹，导入日志分析函数
  ↓
为每个测试准备独立的配置和模拟采集数据
  ↓
分别构造需要验证的情况
  ├─ 没有错误日志 → 应得到 PASS
  ├─ 存在系统错误 → 应得到 WARN
  ├─ SSH 认证失败 → 应得到 WARN
  ├─ 其他程序写出相似消息 → 不应算作 SSH 认证失败
  ├─ 出现异常中文转义 → 修复文字，并记录编码 WARN
  ├─ 日志条数达到上限 → 应追加上限 WARN
  └─ 采集失败或数据不完整 → 应得到 FAIL
  ↓
调用日志分析函数，把实际结果与预期结果比较
  ├─ 一致 → 当前测试通过
  └─ 不一致或出现未预期异常 → 记录测试失败或错误
  ↓
打印全部测试结果
  ↓
返回退出码：全部通过为 0，否则为非 0
"""

# 主要变量：
# PROJECT_ROOT：项目根目录。
# SRC_DIR：存放 Python 程序的 src 文件夹。
# self.log_config：每个测试使用的日志分析配置。
# self.data：模拟 log.sh 返回的 key=value 数据。
# entries：准备放入某一组的日志记录列表。
# prefix：日志字段前缀，例如 system_error 或 ssh。
# checks：分析函数返回的检查结果列表。
# by_target：以检查对象名称为键，便于查找某项结果的字典。

import json
import sys
import unittest
from pathlib import Path


# ---------- 1. 让测试文件能够导入 src 中的代码 ----------

# 当前文件位于 tests 文件夹中。
# 向上找两层，就能找到 linux-server-inspector 项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

# 将 src 放到模块搜索路径前面。
# 这样无论终端当前在哪个目录，都能找到项目的分析模块。
sys.path.insert(0, str(SRC_DIR))

from log_analyzer import analyze_logs, parse_entry


# ---------- 2. 定义日志分析测试 ----------

class LogAnalyzerTests(unittest.TestCase):
    """使用模拟日志，检查分析结果是否符合规则。"""

    def setUp(self):
        """每个测试开始前，都重新准备一份干净的数据。"""

        # 三项告警全部开启，服务日志检查 sshd。
        # 测试不读取 config.yaml，避免真实配置影响测试结果。
        self.log_config = {
            "services": ["sshd"],
            "warn_on_error_entries": True,
            "warn_on_ssh_login_failure": True,
            "warn_on_limit_reached": True,
        }

        # 模拟一次成功采集的基础信息。
        # 结束时间减去开始时间，正好等于 86400 秒。
        # 所有采集值都使用字符串，与 collector 的输出保持一致。
        self.data = {
            "log_source": "systemd_journal",
            "log_start_epoch": "100000",
            "log_end_epoch": "186400",
            "log_lookback_seconds": "86400",
            "log_max_entries": "50",

            "system_error_entry_count": "0",
            "system_error_limit_reached": "0",

            "ssh_entry_count": "0",
            "ssh_limit_reached": "0",

            "service_log_count": "1",
            "service_log_1_name": "sshd.service",
            "service_log_1_entry_count": "0",
            "service_log_1_limit_reached": "0",
        }

    def put_entries(self, prefix, entries):
        """把一组模拟日志放进采集数据，并填写对应条数。"""

        # 例如 prefix 为 ssh，有两条记录时：
        # 写入 ssh_entry_1、ssh_entry_2 和 ssh_entry_count。
        self.data[prefix + "_entry_count"] = str(len(entries))

        # 按采集脚本的规则，达到最大条数就标记为 1。
        max_entries = int(self.data["log_max_entries"])
        self.data[prefix + "_limit_reached"] = (
            "1" if len(entries) >= max_entries else "0"
        )

        # 字典转换成 JSON 文本。
        # 已经是字符串的内容保持原样，方便测试损坏的 JSON。
        for index, entry in enumerate(entries, start=1):
            key = "{}_entry_{}".format(prefix, index)

            if isinstance(entry, dict):
                self.data[key] = json.dumps(entry, ensure_ascii=True)
            else:
                self.data[key] = entry

    def run_analysis(self):
        """模拟 collector 的成功返回，调用真正的日志分析函数。"""

        collected = {
            "ok": True,
            "data": self.data,
            "stderr": "",
            "error": "",
        }

        return analyze_logs(collected, self.log_config)

    def find_check(self, checks, target):
        """按 target 查找结果；缺少该结果时直接让测试失败。"""

        by_target = {
            check["target"]: check
            for check in checks
        }

        self.assertIn(target, by_target)
        return by_target[target]

    # ---------- 3. 检查正常情况 ----------

    def test_no_errors(self):
        """各组没有日志时，应返回正常检查结果。"""

        checks = self.run_analysis()

        # 本次检查系统日志、SSH 认证和一个服务，共三项。
        self.assertEqual(len(checks), 3)

        for check in checks:
            self.assertEqual(check["status"], "PASS")
            self.assertEqual(check["value"]["returned_entries"], 0)

    # ---------- 4. 检查系统和服务错误 ----------

    def test_system_error(self):
        """发现一条系统错误时，应告警并保留消息示例。"""

        self.put_entries("system_error", [
            {"PRIORITY": "3", "MESSAGE": "Example system error"}
        ])

        check = self.find_check(
            self.run_analysis(), "system_errors"
        )

        self.assertEqual(check["status"], "WARN")
        self.assertEqual(check["value"]["matched_problem_entries"], 1)
        self.assertEqual(
            check["value"]["examples"], ["Example system error"]
        )

    def test_service_error(self):
        """指定服务出现错误日志时，应产生该服务的告警。"""

        self.put_entries("service_log_1", [
            {"PRIORITY": "3", "MESSAGE": "Example sshd error"}
        ])

        check = self.find_check(
            self.run_analysis(), "service_errors:sshd"
        )

        self.assertEqual(check["status"], "WARN")
        self.assertEqual(check["value"]["matched_problem_entries"], 1)

    # ---------- 5. 检查 SSH 认证失败识别 ----------

    def test_ssh_failure(self):
        """SSH 成功和失败记录混合时，只统计明确的失败记录。"""

        self.put_entries("ssh", [
            {
                "SYSLOG_IDENTIFIER": "sshd",
                "MESSAGE": "Accepted password for root from 192.0.2.1",
            },
            {
                "SYSLOG_IDENTIFIER": "sshd",
                "MESSAGE": "Failed password for root from 192.0.2.1",
            },
        ])

        check = self.find_check(
            self.run_analysis(), "ssh_authentication"
        )

        self.assertEqual(check["status"], "WARN")
        self.assertEqual(check["value"]["returned_entries"], 2)
        self.assertEqual(check["value"]["matched_problem_entries"], 1)

    def test_other_program_message(self):
        """其他程序写出相似文字，不应算作 SSH 认证失败。"""

        self.put_entries("ssh", [
            {
                "SYSLOG_IDENTIFIER": "example-app",
                "_COMM": "example-app",
                "MESSAGE": "Failed password for root from 192.0.2.1",
            }
        ])

        check = self.find_check(
            self.run_analysis(), "ssh_authentication"
        )

        self.assertEqual(check["status"], "PASS")
        self.assertEqual(check["value"]["matched_problem_entries"], 0)

    # ---------- 6. 检查异常中文编码 ----------

    def test_legacy_encoding(self):
        """异常转义应还原为正确中文，并追加编码告警。"""

        # r 表示保留反斜杠本身，不让 Python 提前处理转义。
        # e6 8f 90 是“提”字的 UTF-8 字节。
        raw = (
            r'{"PRIORITY":"3",'
            r'"MESSAGE":"\uffffffe6\uffffff8f\uffffff90"}'
        )

        # 先检查单条日志解析：文字应准确还原，修复标记应为真。
        entry, repaired = parse_entry(raw)

        self.assertEqual(entry["MESSAGE"], "提")
        self.assertTrue(repaired)

        # 再检查整组分析：应记录一条编码修复告警。
        self.put_entries("system_error", [raw])
        checks = self.run_analysis()

        encoding_check = self.find_check(
            checks, "system_errors_encoding"
        )

        self.assertEqual(encoding_check["status"], "WARN")
        self.assertEqual(encoding_check["value"], 1)

        # 分析函数不能覆盖原始采集文本。
        self.assertEqual(self.data["system_error_entry_1"], raw)

    # ---------- 7. 检查条数上限和告警开关 ----------

    def test_limit_reached(self):
        """达到返回上限时，应提醒可能还有日志没有返回。"""

        # 将本次模拟上限改成 1，放入一条日志即可触发边界。
        self.data["log_max_entries"] = "1"

        self.put_entries("system_error", [
            {"PRIORITY": "3", "MESSAGE": "Example error"}
        ])

        check = self.find_check(
            self.run_analysis(), "system_errors_limit"
        )

        self.assertEqual(check["status"], "WARN")

    def test_disabled_error_warning(self):
        """关闭错误日志告警后，仍应保留实际发现的记录数。"""

        self.log_config["warn_on_error_entries"] = False

        self.put_entries("system_error", [
            {"PRIORITY": "3", "MESSAGE": "Example error"}
        ])

        check = self.find_check(
            self.run_analysis(), "system_errors"
        )

        self.assertEqual(check["status"], "PASS")
        self.assertEqual(check["value"]["matched_problem_entries"], 1)

    # ---------- 8. 检查失败和损坏的数据 ----------

    def test_collection_failure(self):
        """采集失败时，不能把空数据当成没有错误日志。"""

        collected = {
            "ok": False,
            "data": {},
            "stderr": "Example collection error",
            "error": "日志采集失败",
        }

        checks = analyze_logs(collected, self.log_config)

        self.assertTrue(checks)
        self.assertTrue(
            all(check["status"] == "FAIL" for check in checks)
        )

    def test_missing_entry(self):
        """声明有一条日志却未提供内容时，应报告失败。"""

        self.data["system_error_entry_count"] = "1"

        check = self.find_check(
            self.run_analysis(), "system_errors"
        )

        self.assertEqual(check["status"], "FAIL")

    def test_invalid_json(self):
        """日志不是合法 JSON 时，应报告失败。"""

        self.put_entries("system_error", [
            '{"MESSAGE":'
        ])

        check = self.find_check(
            self.run_analysis(), "system_errors"
        )

        self.assertEqual(check["status"], "FAIL")

    def test_service_name_mismatch(self):
        """返回服务与要求检查的服务不同，应报告失败。"""

        # 配置要求检查 sshd，这里故意返回 crond。
        self.data["service_log_1_name"] = "crond.service"

        checks = self.run_analysis()

        self.assertTrue(checks)
        self.assertTrue(
            all(check["status"] == "FAIL" for check in checks)
        )


# ---------- 9. 直接运行本文件时，启动所有测试 ----------

if __name__ == "__main__":
    # verbosity=2 会显示每个测试的名字和中文说明。
    # unittest 自动发现上面所有以 test_ 开头的方法。
    unittest.main(verbosity=2)