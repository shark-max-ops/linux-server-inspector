# -*- coding: utf-8 -*-

# 文件用途：
# 使用临时状态文件和模拟 HTTPS 传输验证钉钉告警开关、去重、恢复、
# 业务失败、超时、异常状态及凭据脱敏；测试绝不连接真实钉钉接口。

"""
整体执行流程：
开始
  ↓
为每个测试创建独立临时目录、模拟配置和模拟巡检结果
  ↓
注入成功、业务失败或超时的传输函数
  ├─ 验证不应发送的分支 → 确认传输函数未调用
  ├─ 验证应发送的分支 → 检查消息和状态文件
  └─ 验证失败分支 → 检查错误及敏感值脱敏
  ↓
测试结束后由临时目录自动清理状态文件
"""

# 主要变量：
# temp_dir：每个测试独立使用的临时目录。
# config：不修改真实 config.yaml 的模拟告警配置。
# system_info：带模拟主机名的主机信息。
# fail_check / pass_check：同一稳定故障的 FAIL 与 PASS 结果。
# calls：记录模拟传输被调用的次数和消息体。

import base64
import hashlib
import hmac
import json
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, parse


# ---------- 1. 将 src 加入本次测试的模块路径 ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from notifier import (
    NotificationError,
    build_signed_url,
    process_notifications,
    save_latest_inspection,
    send_cleanup_notification,
    test_send,
)


# ---------- 2. 准备复用的模拟数据和传输函数 ----------

class DingTalkNotifierTests(unittest.TestCase):
    """逐项验证通知流程，不访问网络、不改变真实运行状态。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "alert_state.json"
        self.snapshot_path = Path(self.temp_dir.name) / "latest.json"
        self.config = {
            "alerts": {
                "dingtalk": {
                    "enabled": True,
                    "statuses": ["FAIL"],
                    "repeat_interval_minutes": 30,
                    "request_timeout_seconds": 5,
                    "max_details": 10,
                    "max_message_chars": 4000,
                    "cleanup_notification_enabled": True,
                    "cleanup_max_details": 30,
                    "latest_snapshot_file": str(self.snapshot_path),
                    "snapshot_max_age_minutes": 5,
                    "at_all": False,
                    "state_file": str(self.state_path),
                    "credential_file": None,
                }
            }
        }
        self.system_info = {"hostname": "test-host"}
        self.fail_check = {
            "category": "disk",
            "target": "/data",
            "status": "FAIL",
            "value": 95.1,
            "reason": "使用率达到失败阈值",
        }
        self.pass_check = {
            "category": "disk",
            "target": "/data",
            "status": "PASS",
            "value": 30.0,
            "reason": "使用率低于警告阈值",
        }
        self.environ = {
            "DINGTALK_WEBHOOK_URL": (
                "https://oapi.dingtalk.com/robot/send?access_token="
                "test-access-token"
            ),
            "DINGTALK_WEBHOOK_SECRET": "SEC-test-secret",
        }
        self.now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        self.calls = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def success_transport(self, url, payload, timeout_seconds):
        """模拟 HTTP 与钉钉业务结果同时成功。"""

        self.calls.append((url, payload, timeout_seconds))
        return 200, b'{"errcode":0,"errmsg":"ok"}'

    def run_notify(self, checks, now=None, transport=None):
        """使用当前测试配置调用通知主函数。"""

        return process_notifications(
            self.system_info,
            checks,
            self.config,
            now=self.now if now is None else now,
            transport=(
                self.success_transport if transport is None else transport
            ),
            environ=self.environ,
        )

    def read_state(self):
        """读取临时状态文件，便于检查成功时间。"""

        return json.loads(self.state_path.read_text(encoding="utf-8"))

    # ---------- 3. 开关和无故障分支 ----------

    def test_signature_uses_timestamp_newline_and_secret(self):
        """签名严格使用毫秒时间戳、换行和 Secret。"""

        timestamp_ms = 1770000000123
        signed_url = build_signed_url(
            self.environ["DINGTALK_WEBHOOK_URL"],
            self.environ["DINGTALK_WEBHOOK_SECRET"],
            timestamp_ms,
        )
        query = dict(parse.parse_qsl(parse.urlsplit(signed_url).query))
        source = "{}\n{}".format(
            timestamp_ms, self.environ["DINGTALK_WEBHOOK_SECRET"]
        ).encode("utf-8")
        expected = base64.b64encode(
            hmac.new(
                self.environ["DINGTALK_WEBHOOK_SECRET"].encode("utf-8"),
                source,
                hashlib.sha256,
            ).digest()
        ).decode("ascii")

        self.assertEqual(query["timestamp"], str(timestamp_ms))
        self.assertEqual(query["sign"], expected)

    def test_disabled_does_not_send_or_create_state(self):
        """开关关闭时不读取凭据、不发送、不创建状态。"""

        self.config["alerts"]["dingtalk"]["enabled"] = False
        result = process_notifications(
            self.system_info,
            [self.fail_check],
            self.config,
            now=self.now,
            transport=self.success_transport,
            environ={},
        )

        self.assertEqual(result["action"], "disabled")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state_path.exists())

    def test_no_fail_does_not_send(self):
        """只有 PASS 时不发送请求。"""

        result = self.run_notify([self.pass_check])

        self.assertEqual(result["action"], "no_alert")
        self.assertEqual(self.calls, [])

    def test_dry_run_previews_without_send_or_state(self):
        """dry-run 只返回测试预览，不请求、不读写状态。"""

        result = process_notifications(
            self.system_info,
            [self.fail_check],
            self.config,
            dry_run=True,
            now=self.now,
            transport=self.success_transport,
            environ={},
        )

        self.assertEqual(result["action"], "dry_run")
        self.assertIn("测试告警", result["message"])
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state_path.exists())

    def test_explicit_test_send_uses_mock_data_without_state(self):
        """test-send 可在开关关闭时发送模拟消息，且不更新状态。"""

        self.config["alerts"]["dingtalk"]["enabled"] = False
        result = test_send(
            self.config,
            transport=self.success_transport,
            environ=self.environ,
            now=self.now,
        )

        message = self.calls[0][1]["markdown"]["text"]
        self.assertEqual(result["action"], "test_sent")
        self.assertIn("测试告警", message)
        self.assertIn("模拟数据", message)
        self.assertFalse(self.state_path.exists())

    def test_cleanup_notification_uses_latest_snapshot(self):
        """资料清理消息包含实际清理统计和最近巡检状态。"""

        checks = [self.pass_check, self.fail_check]
        save_latest_inspection(
            self.config,
            self.system_info,
            checks,
            now=self.now,
        )
        summary = {
            "candidate_count": 3,
            "candidate_bytes": 4096,
            "deleted_count": 2,
            "deleted_bytes": 3072,
            "skipped_count": 1,
            "error_count": 0,
            "keep_days": 7,
        }
        result = send_cleanup_notification(
            self.config,
            summary,
            now=self.now + timedelta(minutes=1),
            transport=self.success_transport,
            environ=self.environ,
        )
        message = self.calls[0][1]["markdown"]["text"]

        self.assertEqual(result["action"], "sent")
        self.assertIn("资料清理", message)
        self.assertIn("实际删除：2 个", message)
        self.assertIn("PASS：1，WARN：0，FAIL：1", message)
        self.assertIn("disk / /data", message)
        self.assertTrue(self.snapshot_path.exists())
        self.assertFalse(self.state_path.exists())

    def test_cleanup_notification_marks_stale_snapshot_unavailable(self):
        """快照过旧时仍发送清理结果，但不冒充当前巡检状态。"""

        save_latest_inspection(
            self.config,
            self.system_info,
            [self.pass_check],
            now=self.now,
        )
        summary = {
            "candidate_count": 0,
            "candidate_bytes": 0,
            "deleted_count": 0,
            "deleted_bytes": 0,
            "skipped_count": 0,
            "error_count": 0,
            "keep_days": 7,
        }
        send_cleanup_notification(
            self.config,
            summary,
            now=self.now + timedelta(minutes=6),
            transport=self.success_transport,
            environ=self.environ,
        )
        message = self.calls[0][1]["markdown"]["text"]

        self.assertIn("状态不可用", message)
        self.assertIn("超过允许时效", message)

    # ---------- 4. 首次、重复和间隔提醒 ----------

    def test_first_failure_sends_and_records_success(self):
        """首次故障发送一次，并在业务成功后记录成功时间。"""

        result = self.run_notify([self.fail_check])
        state = self.read_state()
        item = next(iter(state["active_faults"].values()))

        self.assertEqual(result["action"], "sent")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(item["last_success_at"], self.now.timestamp())
        self.assertFalse(self.calls[0][1]["at"]["isAtAll"])
        self.assertIn("test-host", self.calls[0][1]["markdown"]["text"])

    def test_repeat_is_suppressed_before_interval(self):
        """同一故障数值变化后仍使用稳定标识，并在间隔内抑制。"""

        self.run_notify([self.fail_check])
        changed = dict(self.fail_check, value=96.2)
        result = self.run_notify(
            [changed], now=self.now + timedelta(minutes=29)
        )

        self.assertEqual(result["action"], "suppressed")
        self.assertEqual(len(self.calls), 1)

    def test_repeat_sends_after_interval(self):
        """达到配置间隔后再次提醒。"""

        self.run_notify([self.fail_check])
        result = self.run_notify(
            [self.fail_check], now=self.now + timedelta(minutes=30)
        )

        self.assertEqual(result["action"], "sent")
        self.assertEqual(len(self.calls), 2)

    def test_failure_reappears_after_reliable_recovery(self):
        """明确 PASS 会清除活动状态，再次 FAIL 可立即重新告警。"""

        self.run_notify([self.fail_check])
        recovered = self.run_notify(
            [self.pass_check], now=self.now + timedelta(minutes=1)
        )
        reappeared = self.run_notify(
            [self.fail_check], now=self.now + timedelta(minutes=2)
        )

        self.assertEqual(recovered["action"], "no_alert")
        self.assertEqual(reappeared["action"], "sent")
        self.assertEqual(len(self.calls), 2)

    def test_missing_value_does_not_clear_active_failure(self):
        """无有效值的非 FAIL 结果不能被当作可靠恢复。"""

        self.run_notify([self.fail_check])
        incomplete = dict(self.pass_check, value=None)
        self.run_notify(
            [incomplete], now=self.now + timedelta(minutes=1)
        )
        state = self.read_state()

        self.assertEqual(len(state["active_faults"]), 1)

    # ---------- 5. 接口、超时、状态和脱敏失败分支 ----------

    def test_business_failure_does_not_record_success(self):
        """HTTP 成功但 errcode 非零时，不更新最近成功发送时间。"""

        def business_failure(url, payload, timeout_seconds):
            self.calls.append((url, payload, timeout_seconds))
            return 200, b'{"errcode":310000,"errmsg":"sign not match"}'

        with self.assertRaisesRegex(NotificationError, "业务返回失败"):
            self.run_notify([self.fail_check], transport=business_failure)

        item = next(iter(self.read_state()["active_faults"].values()))
        self.assertIsNone(item["last_success_at"])

    def test_timeout_does_not_record_success(self):
        """超时明确提示送达结果未知，并保留空成功时间。"""

        def timeout_transport(url, payload, timeout_seconds):
            raise socket.timeout("timed out")

        with self.assertRaisesRegex(
            NotificationError, "可能已送达但未收到响应"
        ):
            self.run_notify([self.fail_check], transport=timeout_transport)

        item = next(iter(self.read_state()["active_faults"].values()))
        self.assertIsNone(item["last_success_at"])

    def test_corrupt_state_stops_before_send(self):
        """状态文件损坏时停止通知，避免丢失去重依据。"""

        self.state_path.write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(NotificationError, "状态文件异常"):
            self.run_notify([self.fail_check])

        self.assertEqual(self.calls, [])

    def test_error_redacts_token_secret_and_signed_url(self):
        """网络异常即使携带完整 URL 和 Secret，也不得泄露凭据。"""

        captured_url = {"value": None}

        def leaking_transport(url, payload, timeout_seconds):
            captured_url["value"] = url
            raise error.URLError(
                "request {} secret {}".format(
                    url, self.environ["DINGTALK_WEBHOOK_SECRET"]
                )
            )

        with self.assertRaises(NotificationError) as context:
            self.run_notify([self.fail_check], transport=leaking_transport)

        message = str(context.exception)
        self.assertNotIn("test-access-token", message)
        self.assertNotIn("SEC-test-secret", message)
        self.assertNotIn(captured_url["value"], message)
        self.assertIn("已脱敏", message)


if __name__ == "__main__":
    unittest.main()
