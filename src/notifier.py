# -*- coding: utf-8 -*-

# 文件用途：
# 接收 main.py 已生成的 system_info 和 checks，按配置筛选故障、抑制重复提醒，
# 并通过带加签的钉钉群自定义机器人 Webhook 发送告警。

"""
整体执行流程：
开始
  ↓
读取并校验 alerts.dingtalk 配置
  ├─ 开关关闭 → 返回，不读取密钥、不发送请求
  ↓
根据主机名、category、target 生成稳定故障标识
  ↓
读取跨进程状态文件
  ├─ 状态文件异常 → 停止通知并返回明确错误
  ↓
更新活动故障并判断首次告警或重复提醒是否到期
  ├─ 故障可靠消失 → 清除活动状态
  ├─ 数据缺失或检查项未出现 → 保留原活动状态
  └─ 无到期故障 → 原子保存状态后返回
  ↓
从环境变量或仓库外受限文件读取 Webhook 与 Secret
  ↓
生成限长消息，计算 HMAC-SHA256 加签并执行一次 HTTPS 请求
  ├─ dry-run → 只预览，不请求、不读写状态
  ├─ HTTP 或业务结果失败 → 保留原成功时间并返回错误
  └─ 接口确认成功 → 原子更新对应故障的最近成功发送时间
  ↓
结束

命令行分支：
  --dry-run  → 使用明确的模拟故障预览，不发送、不更新状态
  --test-send → 发送明确标注“测试告警”的模拟消息，不更新状态
"""

# 主要变量：
# PROJECT_ROOT：项目根目录。
# ENV_WEBHOOK / ENV_SECRET：cron 环境中需要显式提供的凭据变量名。
# alert_config：校验后的钉钉告警配置。
# fault_id：由主机名、category、target 生成的稳定故障标识。
# state：跨巡检进程保存的活动故障和最近成功发送时间。
# latest_snapshot：最近一次成功保存报告后的巡检内存结果快照。
# due_faults：首次出现或已达到重复提醒间隔的故障。
# message：发送给钉钉的限长 Markdown 内容。

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import socket
import ssl
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

# ---------- 1. 定义路径、凭据变量和错误类型 ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
ENV_WEBHOOK = "DINGTALK_WEBHOOK_URL"
ENV_SECRET = "DINGTALK_WEBHOOK_SECRET"
STATE_VERSION = 1
MAX_RESPONSE_BYTES = 65536


class NotificationError(Exception):
    """表示通知配置、状态、凭据或请求失败。"""


# ---------- 2. 校验告警配置，不让模糊值改变通知行为 ----------

def _require_int(config, name, minimum, maximum):
    """读取指定范围内的整数，并拒绝 True/False 冒充数字。"""

    value = config.get(name)

    if type(value) is not int or not minimum <= value <= maximum:
        raise NotificationError(
            "alerts.dingtalk.{} 必须是 {} 到 {} 的整数".format(
                name, minimum, maximum
            )
        )

    return value


def validate_dingtalk_config(config):
    """返回经过校验的钉钉配置副本。"""

    alerts = config.get("alerts")

    if not isinstance(alerts, dict):
        raise NotificationError("alerts 必须是配置字典")

    dingtalk = alerts.get("dingtalk")

    if not isinstance(dingtalk, dict):
        raise NotificationError("alerts.dingtalk 必须是配置字典")

    enabled = dingtalk.get("enabled")
    at_all = dingtalk.get("at_all")
    cleanup_notification_enabled = dingtalk.get(
        "cleanup_notification_enabled"
    )

    if type(enabled) is not bool:
        raise NotificationError("alerts.dingtalk.enabled 必须是布尔值")

    if type(at_all) is not bool:
        raise NotificationError("alerts.dingtalk.at_all 必须是布尔值")

    if type(cleanup_notification_enabled) is not bool:
        raise NotificationError(
            "alerts.dingtalk.cleanup_notification_enabled 必须是布尔值"
        )

    statuses = dingtalk.get("statuses")

    if not isinstance(statuses, list) or not statuses:
        raise NotificationError("alerts.dingtalk.statuses 必须是非空列表")

    allowed_statuses = {"WARN", "FAIL"}

    if (
        any(
            not isinstance(status, str) or status not in allowed_statuses
            for status in statuses
        )
        or len(set(statuses)) != len(statuses)
    ):
        raise NotificationError(
            "alerts.dingtalk.statuses 只能包含不重复的 WARN、FAIL"
        )

    state_file = dingtalk.get("state_file")
    latest_snapshot_file = dingtalk.get("latest_snapshot_file")
    credential_file = dingtalk.get("credential_file")

    if (
        not isinstance(state_file, str)
        or not state_file.strip()
        or any(char in state_file for char in ("\x00", "\n", "\r"))
    ):
        raise NotificationError(
            "alerts.dingtalk.state_file 必须是有效的非空路径"
        )

    if credential_file is not None and (
        not isinstance(credential_file, str)
        or not credential_file.strip()
        or any(char in credential_file for char in ("\x00", "\n", "\r"))
    ):
        raise NotificationError(
            "alerts.dingtalk.credential_file 必须为空或有效路径"
        )

    if (
        not isinstance(latest_snapshot_file, str)
        or not latest_snapshot_file.strip()
        or any(
            char in latest_snapshot_file for char in ("\x00", "\n", "\r")
        )
    ):
        raise NotificationError(
            "alerts.dingtalk.latest_snapshot_file 必须是有效的非空路径"
        )

    result = dict(dingtalk)
    result["enabled"] = enabled
    result["at_all"] = at_all
    result["cleanup_notification_enabled"] = cleanup_notification_enabled
    result["statuses"] = tuple(statuses)
    result["repeat_interval_minutes"] = _require_int(
        dingtalk, "repeat_interval_minutes", 1, 10080
    )
    result["request_timeout_seconds"] = _require_int(
        dingtalk, "request_timeout_seconds", 1, 120
    )
    result["max_details"] = _require_int(
        dingtalk, "max_details", 1, 50
    )
    result["max_message_chars"] = _require_int(
        dingtalk, "max_message_chars", 500, 20000
    )
    result["cleanup_max_details"] = _require_int(
        dingtalk, "cleanup_max_details", 1, 100
    )
    result["snapshot_max_age_minutes"] = _require_int(
        dingtalk, "snapshot_max_age_minutes", 1, 1440
    )

    return result


# ---------- 3. 读取仓库外凭据，并检查文件访问权限 ----------

def _parse_credential_file(path):
    """解析简单 KEY=VALUE 文件；不把文件当作 Shell 脚本执行。"""

    credential_path = Path(path)

    if not credential_path.is_absolute():
        raise NotificationError("钉钉凭据文件必须使用仓库外的绝对路径")

    try:
        file_stat = credential_path.stat()
    except OSError as exc:
        raise NotificationError("无法读取钉钉凭据文件：{}".format(exc))

    if not stat.S_ISREG(file_stat.st_mode):
        raise NotificationError("钉钉凭据路径不是普通文件")

    # 凭据即使使用绝对路径，也不能放回项目仓库目录中。
    try:
        credential_path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise NotificationError("钉钉凭据文件必须位于项目仓库之外")

    # Linux 部署时拒绝组用户或其他用户可读写执行的凭据文件。
    if os.name == "posix" and file_stat.st_mode & 0o077:
        raise NotificationError("钉钉凭据文件权限过宽，应设置为 600")

    values = {}

    try:
        with credential_path.open("r", encoding="utf-8") as file_obj:
            for line_number, raw_line in enumerate(file_obj, start=1):
                line = raw_line.strip()

                if not line or line.startswith("#"):
                    continue

                if "=" not in line:
                    raise NotificationError(
                        "钉钉凭据文件第 {} 行缺少等号".format(
                            line_number
                        )
                    )

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                if key not in (ENV_WEBHOOK, ENV_SECRET):
                    raise NotificationError(
                        "钉钉凭据文件第 {} 行包含不支持的字段".format(
                            line_number
                        )
                    )

                if key in values:
                    raise NotificationError(
                        "钉钉凭据文件包含重复字段：{}".format(key)
                    )

                values[key] = value
    except UnicodeError as exc:
        raise NotificationError("钉钉凭据文件不是有效 UTF-8：{}".format(exc))
    except OSError as exc:
        raise NotificationError("无法读取钉钉凭据文件：{}".format(exc))

    return values


def _validate_webhook(webhook):
    """只接受钉钉官方 HTTPS Webhook 形式。"""

    try:
        parsed = parse.urlsplit(webhook)
    except ValueError:
        raise NotificationError("钉钉 Webhook 格式不合法")

    try:
        port = parsed.port
    except ValueError:
        raise NotificationError("钉钉 Webhook 端口不合法")

    if (
        parsed.scheme != "https"
        or parsed.hostname != "oapi.dingtalk.com"
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise NotificationError("钉钉 Webhook 必须使用官方 HTTPS 地址")

    if parsed.path != "/robot/send" or parsed.username or parsed.password:
        raise NotificationError("钉钉 Webhook 路径不合法")

    query = dict(parse.parse_qsl(parsed.query, keep_blank_values=True))

    if not query.get("access_token"):
        raise NotificationError("钉钉 Webhook 缺少 access_token")


def load_credentials(alert_config, environ=None):
    """环境变量优先；缺少的值可由仓库外受限文件补充。"""

    environ = os.environ if environ is None else environ
    values = {
        ENV_WEBHOOK: environ.get(ENV_WEBHOOK, "").strip(),
        ENV_SECRET: environ.get(ENV_SECRET, "").strip(),
    }

    if not values[ENV_WEBHOOK] or not values[ENV_SECRET]:
        credential_file = alert_config.get("credential_file")

        if credential_file:
            file_values = _parse_credential_file(credential_file)

            for key in (ENV_WEBHOOK, ENV_SECRET):
                if not values[key]:
                    values[key] = file_values.get(key, "").strip()

    if not values[ENV_WEBHOOK] or not values[ENV_SECRET]:
        raise NotificationError(
            "缺少钉钉 Webhook 或加签 Secret；请配置环境变量或受限凭据文件"
        )

    _validate_webhook(values[ENV_WEBHOOK])

    return values[ENV_WEBHOOK], values[ENV_SECRET]


# ---------- 4. 生成稳定故障标识并读写跨进程状态 ----------

def _fault_id(hostname, check):
    """数值和原因不参加标识，避免小幅变化产生新的故障。"""

    identity = json.dumps(
        [hostname, check["category"], check["target"]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(identity).hexdigest()


def _state_path(raw_path):
    """相对状态路径统一按项目根目录解释。"""

    path = Path(raw_path)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def _runtime_path(raw_path):
    """把配置中的运行时文件路径转换为绝对路径。"""

    return _state_path(raw_path)


def load_state(path):
    """读取状态；不存在表示尚未产生过活动故障。"""

    if not path.exists():
        return {"version": STATE_VERSION, "active_faults": {}}

    try:
        with path.open("r", encoding="utf-8") as file_obj:
            state = json.load(file_obj)
    except (OSError, UnicodeError, ValueError) as exc:
        raise NotificationError("钉钉告警状态文件异常：{}".format(exc))

    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state.get("version") != STATE_VERSION
        or not isinstance(state.get("active_faults"), dict)
    ):
        raise NotificationError("钉钉告警状态文件结构不合法")

    for fault_id, item in state["active_faults"].items():
        if (
            not isinstance(fault_id, str)
            or not isinstance(item, dict)
            or not isinstance(item.get("hostname"), str)
            or not isinstance(item.get("category"), str)
            or not isinstance(item.get("target"), str)
            or (
                item.get("last_success_at") is not None
                and (
                    isinstance(item.get("last_success_at"), bool)
                    or not isinstance(
                        item.get("last_success_at"), (int, float)
                    )
                    or not math.isfinite(item.get("last_success_at"))
                    or item.get("last_success_at") < 0
                )
            )
        ):
            raise NotificationError("钉钉告警状态文件包含非法故障记录")

    return state


def save_state_atomic(path, state, label="钉钉告警状态"):
    """先完整写入同目录临时文件，再原子替换正式状态文件。"""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix=".dingtalk_state_",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(
                state,
                temp_file,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.chmod(str(temp_path), 0o600)
        os.replace(str(temp_path), str(path))
        temp_path = None

    except (OSError, TypeError, ValueError) as exc:
        raise NotificationError("无法原子保存{}：{}".format(label, exc))
    finally:
        if "temp_path" in locals() and temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def save_latest_inspection(config, system_info, checks, now=None):
    """原子保存 main.py 已生成的数据，供清理通知读取。"""

    alert_config = validate_dingtalk_config(config)

    if not isinstance(system_info, dict):
        raise NotificationError("system_info 必须是字典")

    hostname = system_info.get("hostname")

    if not isinstance(hostname, str) or not hostname.strip():
        raise NotificationError("system_info 缺少真实主机名，无法保存巡检快照")

    if not isinstance(checks, list) or not checks:
        raise NotificationError("checks 必须是非空列表")

    for check in checks:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("category"), str)
            or not isinstance(check.get("target"), str)
            or check.get("status") not in ("PASS", "WARN", "FAIL")
            or "value" not in check
            or not isinstance(check.get("reason"), str)
        ):
            raise NotificationError("checks 包含不合法的检查结果")

    moment = datetime.now(timezone.utc).astimezone() if now is None else now

    if moment.tzinfo is None or moment.utcoffset() is None:
        raise NotificationError("巡检快照时间必须包含时区")

    snapshot = {
        "version": STATE_VERSION,
        "captured_at": moment.timestamp(),
        "captured_at_text": _format_time(moment),
        "system_info": system_info,
        "checks": checks,
    }
    snapshot_path = _runtime_path(alert_config["latest_snapshot_file"])
    save_state_atomic(snapshot_path, snapshot, label="最近巡检快照")
    return snapshot_path


def load_latest_inspection(path):
    """读取并验证最近巡检快照；不读取 TXT 报告。"""

    try:
        with path.open("r", encoding="utf-8") as file_obj:
            snapshot = json.load(file_obj)
    except (OSError, UnicodeError, ValueError) as exc:
        raise NotificationError("最近巡检快照不可用：{}".format(exc))

    captured_at = snapshot.get("captured_at") if isinstance(snapshot, dict) else None
    checks = snapshot.get("checks") if isinstance(snapshot, dict) else None
    system_info = snapshot.get("system_info") if isinstance(snapshot, dict) else None

    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("version")) is not int
        or snapshot.get("version") != STATE_VERSION
        or isinstance(captured_at, bool)
        or not isinstance(captured_at, (int, float))
        or not math.isfinite(captured_at)
        or captured_at < 0
        or not isinstance(snapshot.get("captured_at_text"), str)
        or not isinstance(system_info, dict)
        or not isinstance(system_info.get("hostname"), str)
        or not isinstance(checks, list)
        or not checks
    ):
        raise NotificationError("最近巡检快照结构不合法")

    for check in checks:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("category"), str)
            or not isinstance(check.get("target"), str)
            or check.get("status") not in ("PASS", "WARN", "FAIL")
        ):
            raise NotificationError("最近巡检快照包含非法检查结果")

    return snapshot


def _is_reliable_recovery(check, alert_statuses):
    """只有明确、有实际值的非告警结果才证明故障已消失。"""

    return (
        check.get("status") in ("PASS", "WARN", "FAIL")
        and check.get("status") not in alert_statuses
        and "value" in check
        and check.get("value") is not None
    )


# ---------- 5. 生成限长消息，明确展示主机、时间、状态和明细 ----------

def _clean_text(value, limit=300):
    """移除控制字符并限制单字段长度，避免破坏 Markdown 排版。"""

    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif value is None:
        text = "无有效数据"
    else:
        text = str(value)

    text = " ".join(text.replace("`", "'").split())

    if len(text) > limit:
        text = text[: limit - 1] + "…"

    return text


def _format_time(moment):
    """输出带冒号时区偏移的 ISO 风格时间。"""

    text = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    return text[:-2] + ":" + text[-2:]


def build_message(hostname, faults, alert_config, moment, test=False):
    """按明细数量和字符上限生成 Markdown 告警。"""

    shown_faults = faults[: alert_config["max_details"]]
    title = "Linux 巡检测试告警" if test else "Linux 巡检告警"
    status_text = ", ".join(sorted({item["status"] for item in faults}))
    lines = [
        "### {}".format(title),
        "- 主机：{}".format(_clean_text(hostname, 200)),
        "- 时间：{}".format(_format_time(moment)),
        "- 状态：{}".format(status_text),
        "- 故障数：{}".format(len(faults)),
        "",
        "#### 失败检查明细",
    ]

    for index, check in enumerate(shown_faults, start=1):
        lines.append(
            "{}. **[{}] {} / {}**  ".format(
                index,
                _clean_text(check["status"], 20),
                _clean_text(check["category"], 100),
                _clean_text(check["target"], 160),
            )
        )
        lines.append("   实际值：{}  ".format(_clean_text(check["value"])))
        lines.append("   说明：{}".format(_clean_text(check["reason"])))

    omitted = len(faults) - len(shown_faults)

    if omitted > 0:
        lines.extend(["", "另有 {} 项明细因数量限制未展示。".format(omitted)])

    if test:
        lines.extend(["", "**这是模拟数据的测试告警，不代表主机真实故障。**"])

    message = "\n".join(lines)
    limit = alert_config["max_message_chars"]

    if len(message) > limit:
        marker = "\n\n（消息已按配置截断，请查看本地巡检报告。）"
        message = message[: limit - len(marker)] + marker

    return title, message


# ---------- 6. 加签并执行一次 HTTPS 请求，不做自动重试 ----------

def build_signed_url(webhook, secret, timestamp_ms):
    """按钉钉规则添加毫秒时间戳和 HMAC-SHA256 签名。"""

    string_to_sign = "{}\n{}".format(timestamp_ms, secret).encode("utf-8")
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign, hashlib.sha256
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    parsed = parse.urlsplit(webhook)
    query_items = [
        (key, value)
        for key, value in parse.parse_qsl(
            parsed.query, keep_blank_values=True
        )
        if key not in ("timestamp", "sign")
    ]
    query_items.extend([
        ("timestamp", str(timestamp_ms)),
        ("sign", signature),
    ])

    return parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parse.urlencode(query_items),
            parsed.fragment,
        )
    )


def redact_sensitive(text, webhook=None, secret=None):
    """清除错误文字中可能出现的 token、签名 URL 和 Secret。"""

    result = str(text)

    if webhook:
        result = result.replace(webhook, "[已脱敏Webhook]")

        try:
            query = dict(parse.parse_qsl(parse.urlsplit(webhook).query))
            token = query.get("access_token")

            if token:
                result = result.replace(token, "[已脱敏Token]")
        except ValueError:
            pass

    if secret:
        result = result.replace(secret, "[已脱敏Secret]")

    # 即使异常中出现新生成的完整 URL，也不保留敏感查询参数值。
    import re
    result = re.sub(
        r"(?i)(access_token|sign|timestamp)=([^&\s]+)",
        r"\1=[已脱敏]",
        result,
    )
    return result


def _default_transport(url, payload, timeout_seconds):
    """使用系统 CA 验证 TLS，限制响应大小并返回状态与正文。"""

    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    context = ssl.create_default_context()

    with request.urlopen(
        http_request,
        timeout=timeout_seconds,
        context=context,
    ) as response:
        response_body = response.read(MAX_RESPONSE_BYTES + 1)

        if len(response_body) > MAX_RESPONSE_BYTES:
            raise NotificationError("钉钉响应内容超过安全限制")

        return response.getcode(), response_body


def send_message(
    webhook,
    secret,
    title,
    message,
    alert_config,
    timestamp_ms,
    transport=None,
):
    """发送一次消息，同时检查 HTTP 状态和钉钉业务结果。"""

    signed_url = build_signed_url(webhook, secret, timestamp_ms)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": message},
        "at": {"atMobiles": [], "isAtAll": alert_config["at_all"]},
    }
    transport = _default_transport if transport is None else transport

    try:
        status_code, response_body = transport(
            signed_url,
            payload,
            alert_config["request_timeout_seconds"],
        )
    except NotificationError as exc:
        raise NotificationError(redact_sensitive(exc, webhook, secret))
    except (error.URLError, error.HTTPError, socket.timeout, OSError) as exc:
        safe_error = redact_sensitive(exc, webhook, secret)

        if isinstance(exc, socket.timeout):
            raise NotificationError(
                "钉钉请求超时，消息可能已送达但未收到响应：{}".format(
                    safe_error
                )
            )

        raise NotificationError("钉钉请求失败：{}".format(safe_error))

    if status_code < 200 or status_code >= 300:
        raise NotificationError("钉钉 HTTP 状态异常：{}".format(status_code))

    try:
        if isinstance(response_body, bytes):
            response_body = response_body.decode("utf-8")

        result = json.loads(response_body)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise NotificationError("钉钉响应不是有效 JSON：{}".format(exc))

    if not isinstance(result, dict) or result.get("errcode") != 0:
        error_code = result.get("errcode") if isinstance(result, dict) else None
        error_message = result.get("errmsg") if isinstance(result, dict) else None
        raise NotificationError(
            "钉钉业务返回失败：errcode={}，errmsg={}".format(
                _clean_text(error_code, 50),
                redact_sensitive(
                    _clean_text(error_message, 200), webhook, secret
                ),
            )
        )


# ---------- 7. 发送每日资料清理汇总 ----------

def _format_bytes(byte_count):
    """把非负字节数转换成便于阅读的大小。"""

    if type(byte_count) is not int or byte_count < 0:
        raise NotificationError("清理统计中的字节数不合法")

    size = float(byte_count)

    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return "{:.2f} {}".format(size, unit)
        size /= 1024


def _validate_cleanup_summary(summary):
    """确认清理统计都是非负整数，避免发送误导性数据。"""

    if not isinstance(summary, dict):
        raise NotificationError("清理统计必须是字典")

    fields = (
        "candidate_count",
        "candidate_bytes",
        "deleted_count",
        "deleted_bytes",
        "skipped_count",
        "error_count",
        "keep_days",
    )

    for field in fields:
        value = summary.get(field)

        if type(value) is not int or value < 0:
            raise NotificationError("清理统计字段 {} 不合法".format(field))


def _build_cleanup_message(summary, snapshot, snapshot_note, alert_config, moment):
    """生成包含清理结果和最近巡检分类的限长 Markdown。"""

    _validate_cleanup_summary(summary)
    hostname = "未获取"
    lines = [
        "### 资料清理",
        "- 清理时间：{}".format(_format_time(moment)),
        "- 保留期限：最近 {} 天".format(summary["keep_days"]),
        "- 待清理：{} 个，{}".format(
            summary["candidate_count"],
            _format_bytes(summary["candidate_bytes"]),
        ),
        "- 实际删除：{} 个，释放 {}".format(
            summary["deleted_count"],
            _format_bytes(summary["deleted_bytes"]),
        ),
        "- 跳过：{} 个".format(summary["skipped_count"]),
        "- 删除失败：{} 个".format(summary["error_count"]),
    ]

    if snapshot is None:
        lines.extend([
            "",
            "#### 最近巡检状态",
            "- 状态不可用：{}".format(_clean_text(snapshot_note, 300)),
        ])
    else:
        hostname = snapshot["system_info"]["hostname"]
        counts = {"PASS": 0, "WARN": 0, "FAIL": 0}

        for check in snapshot["checks"]:
            counts[check["status"]] += 1

        lines.insert(1, "- 主机：{}".format(_clean_text(hostname, 200)))
        lines.extend([
            "",
            "#### 最近巡检状态",
            "- 巡检时间：{}".format(snapshot["captured_at_text"]),
            "- PASS：{}，WARN：{}，FAIL：{}".format(
                counts["PASS"], counts["WARN"], counts["FAIL"]
            ),
        ])

        ordered_checks = []

        # 有限明细优先展示 FAIL，其次 WARN，最后 PASS。
        for status in ("FAIL", "WARN", "PASS"):
            ordered_checks.extend(
                item
                for item in snapshot["checks"]
                if item["status"] == status
            )

        details = ordered_checks[: alert_config["cleanup_max_details"]]

        for status in ("FAIL", "WARN", "PASS"):
            matching = [item for item in details if item["status"] == status]

            if not matching:
                continue

            lines.append("")
            lines.append("**{} 检查项**".format(status))

            for check in matching:
                lines.append(
                    "- {} / {}".format(
                        _clean_text(check["category"], 100),
                        _clean_text(check["target"], 180),
                    )
                )

        omitted = len(snapshot["checks"]) - len(details)

        if omitted > 0:
            lines.extend([
                "",
                "另有 {} 项因明细数量限制未展示。".format(omitted),
            ])

    message = "\n".join(lines)
    limit = alert_config["max_message_chars"]

    if len(message) > limit:
        marker = "\n\n（消息已按配置截断，请查看本地清理日志和巡检报告。）"
        message = message[: limit - len(marker)] + marker

    return "资料清理", message


def send_cleanup_notification(
    config,
    cleanup_summary,
    now=None,
    transport=None,
    environ=None,
):
    """发送一次清理汇总；不更新故障提醒状态。"""

    alert_config = validate_dingtalk_config(config)

    if not alert_config["enabled"]:
        return {"action": "disabled", "message": "钉钉告警开关关闭"}

    if not alert_config["cleanup_notification_enabled"]:
        return {"action": "disabled", "message": "资料清理通知开关关闭"}

    moment = datetime.now(timezone.utc).astimezone() if now is None else now

    if moment.tzinfo is None or moment.utcoffset() is None:
        raise NotificationError("清理通知时间必须包含时区")

    snapshot = None
    snapshot_note = "没有最近巡检快照"
    snapshot_path = _runtime_path(alert_config["latest_snapshot_file"])

    try:
        candidate = load_latest_inspection(snapshot_path)
        age_seconds = moment.timestamp() - candidate["captured_at"]
        max_age_seconds = alert_config["snapshot_max_age_minutes"] * 60

        if age_seconds < 0:
            snapshot_note = "最近巡检快照时间晚于清理时间"
        elif age_seconds > max_age_seconds:
            snapshot_note = "最近巡检快照超过允许时效"
        else:
            snapshot = candidate
    except NotificationError as exc:
        snapshot_note = str(exc)

    webhook, secret = load_credentials(alert_config, environ=environ)
    title, message = _build_cleanup_message(
        cleanup_summary,
        snapshot,
        snapshot_note,
        alert_config,
        moment,
    )
    send_message(
        webhook,
        secret,
        title,
        message,
        alert_config,
        int(moment.timestamp() * 1000),
        transport=transport,
    )
    return {"action": "sent", "message": "资料清理通知发送成功"}


# ---------- 8. 处理一次真实巡检通知 ----------

def process_notifications(
    system_info,
    checks,
    config,
    dry_run=False,
    now=None,
    transport=None,
    environ=None,
):
    """处理去重、状态更新和发送，返回便于日志记录的结果。"""

    alert_config = validate_dingtalk_config(config)

    if not isinstance(system_info, dict):
        raise NotificationError("system_info 必须是字典")

    hostname = system_info.get("hostname")

    if not isinstance(hostname, str) or not hostname.strip():
        raise NotificationError("system_info 缺少真实主机名，不能发送告警")

    if not isinstance(checks, list) or not checks:
        raise NotificationError("checks 必须是非空列表")

    for check in checks:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("category"), str)
            or not check.get("category").strip()
            or not isinstance(check.get("target"), str)
            or not check.get("target").strip()
            or check.get("status") not in ("PASS", "WARN", "FAIL")
            or "value" not in check
            or not isinstance(check.get("reason"), str)
        ):
            raise NotificationError("checks 包含不合法的检查结果")

    moment = datetime.now(timezone.utc).astimezone() if now is None else now

    if moment.tzinfo is None or moment.utcoffset() is None:
        raise NotificationError("通知时间必须包含时区")

    alert_statuses = set(alert_config["statuses"])
    faults = [check for check in checks if check["status"] in alert_statuses]

    if dry_run:
        if not faults:
            return {"action": "no_alert", "message": "没有匹配的告警检查项"}

        title, message = build_message(
            hostname, faults, alert_config, moment, test=True
        )
        return {"action": "dry_run", "title": title, "message": message}

    if not alert_config["enabled"]:
        return {"action": "disabled", "message": "钉钉告警开关关闭"}

    state_path = _state_path(alert_config["state_file"])
    state = load_state(state_path)
    active_faults = state["active_faults"]
    for check in checks:
        fault_id = _fault_id(hostname, check)

        if _is_reliable_recovery(check, alert_statuses):
            active_faults.pop(fault_id, None)

    current_faults = []
    current_fault_ids = []
    due_ids = set()
    now_timestamp = moment.timestamp()
    repeat_seconds = alert_config["repeat_interval_minutes"] * 60

    for check in faults:
        fault_id = _fault_id(hostname, check)
        item = active_faults.get(fault_id)

        if item is None:
            item = {
                "hostname": hostname,
                "category": check["category"],
                "target": check["target"],
                "last_success_at": None,
            }
            active_faults[fault_id] = item

        last_success_at = item.get("last_success_at")

        if (
            last_success_at is None
            or now_timestamp - last_success_at >= repeat_seconds
        ) and fault_id not in due_ids:
            current_faults.append(check)
            current_fault_ids.append(fault_id)
            due_ids.add(fault_id)

    # 先保存活动状态；若保存失败，不发送可能无法正确去重的消息。
    save_state_atomic(state_path, state)

    if not current_faults:
        action = "no_alert" if not faults else "suppressed"
        return {"action": action, "message": "没有到期的钉钉告警"}

    webhook, secret = load_credentials(alert_config, environ=environ)
    title, message = build_message(
        hostname, current_faults, alert_config, moment, test=False
    )
    timestamp_ms = int(now_timestamp * 1000)

    send_message(
        webhook,
        secret,
        title,
        message,
        alert_config,
        timestamp_ms,
        transport=transport,
    )

    # 只有 HTTP 与业务结果都确认成功，才更新最近成功发送时间。
    for fault_id in current_fault_ids:
        active_faults[fault_id]["last_success_at"] = now_timestamp

    save_state_atomic(state_path, state)

    return {
        "action": "sent",
        "message": "钉钉告警发送成功，共 {} 项".format(
            len(current_faults)
        ),
    }


# ---------- 9. 提供不会制造真实故障的预览和显式测试入口 ----------

def _load_yaml_config():
    """命令行模式读取项目现有 YAML。"""

    # 正常巡检由 main.py 传入已读取的配置；只有独立命令才需要读 YAML。
    import yaml

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
    except yaml.YAMLError as exc:
        raise NotificationError("配置文件 YAML 格式错误：{}".format(exc))

    if not isinstance(config, dict):
        raise NotificationError("配置文件顶层必须是字典")

    return config


def _test_data():
    """构造明确标注用途的模拟数据，不操作任何系统服务或磁盘。"""

    system_info = {"hostname": "demo-host（模拟）"}
    checks = [
        {
            "category": "test",
            "target": "dingtalk_test_send",
            "status": "FAIL",
            "value": "模拟值",
            "reason": "仅验证钉钉机器人通道，不代表真实故障",
        }
    ]
    return system_info, checks


def test_send(config, transport=None, environ=None, now=None):
    """显式发送模拟测试消息；忽略 enabled，但绝不更新状态。"""

    alert_config = validate_dingtalk_config(config)
    system_info, checks = _test_data()
    moment = datetime.now(timezone.utc).astimezone() if now is None else now
    webhook, secret = load_credentials(alert_config, environ=environ)
    title, message = build_message(
        system_info["hostname"], checks, alert_config, moment, test=True
    )
    send_message(
        webhook,
        secret,
        title,
        message,
        alert_config,
        int(moment.timestamp() * 1000),
        transport=transport,
    )
    return {"action": "test_sent", "message": "钉钉测试告警发送成功"}


def main(argv=None):
    """解析互斥命令，只允许明确预览或明确测试发送。"""

    parser = argparse.ArgumentParser(description="钉钉告警预览和测试工具")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="使用模拟数据预览消息，不发送、不更新状态",
    )
    group.add_argument(
        "--test-send",
        action="store_true",
        help="显式发送模拟测试告警，不更新状态",
    )
    args = parser.parse_args(argv)

    try:
        config = _load_yaml_config()

        if args.dry_run:
            system_info, checks = _test_data()
            result = process_notifications(
                system_info,
                checks,
                config,
                dry_run=True,
            )
            print(result["message"])
        else:
            result = test_send(config)
            print(result["message"])

    except (OSError, NotificationError) as exc:
        print(
            "钉钉通知工具失败：{}".format(redact_sensitive(exc)),
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
