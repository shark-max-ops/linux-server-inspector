# -*- coding: utf-8 -*-
# ============================================================
# 文件名称：log_analyzer.py
#
# 文件用途：
#   解析 log.sh 返回的 JSON 日志，生成日志检查结果。
#   包括系统错误、SSH 认证失败、指定服务错误和截断提示。
"""
整体执行流程：
开始
  ↓
读取 config.yaml，检查日志配置
  ├─ 配置错误 → 输出错误，退出码 2
  ↓
调用 log.sh，获取系统、SSH、指定服务的日志
  ↓
检查采集状态、来源、时间范围、服务名称等
  ├─ 不符合要求 → 生成 FAIL 检查结果
  ↓
逐组分析日志
  │
  ├─ 检查条数与上限标记
  ├─ 逐条处理异常转义、解析 JSON
  ├─ 系统和服务：检查错误日志
  ├─ SSH：匹配明确认证失败
  ├─ 根据配置生成 PASS 或 WARN
  └─ 必要时追加上限、编码告警
  ↓
合并全部检查结果
  ↓
计算总状态：FAIL 优先，其次 WARN，最后 PASS
  ↓
打印 JSON
  ↓
返回退出码：0 / 1 / 2
"""
# 主要变量：
#   config / log_config  全部配置、日志配置
#   collected / data     完整采集结果、其中的数据字典
#   checks               本次检查结果列表
#   prefix               日志组前缀，例如 system_error 或 ssh
#   count                本次返回的日志数量
#   limit_reached        是否达到返回数量上限
#   raw                  一条原始 JSON 日志字符串
#   entry                JSON 解析后的日志字典
#   message              日志中的 MESSAGE 文本
#   repaired             当前记录是否修复了异常转义
#   repaired_count       修复过异常转义的记录数量
#   messages             当前日志组中提取出的消息
#   failed_messages      明确匹配到的 SSH 认证失败消息
#   examples             输出的示例消息，最多五条
#
# 判断规则：
#   已采集的错误日志或认证失败：按配置决定是否 WARN。
#   达到返回上限：按配置决定是否 WARN。
#   日志读取或解析失败：FAIL，不误报为零条异常。
#   异常转义修复成功：额外 WARN，提示原始数据存在编码问题。
#
# 注意：
#   返回条数不是完整的全天统计。
#   SSH 统计的是明确的失败日志记录，不是独立攻击次数。
#   不删除日志，不修改任何服务。
#
# 运行环境：
#   Linux 项目虚拟环境，Python 3.6。
# ============================================================


# ---------- 1. 导入标准库与已有函数 ----------
import json
import re

import yaml

from collector import collect
from analyzer import (
    get_data,
    load_config,
    make_check,
    read_count,
    summarize,
    validate_names,
)


# ---------- 2. 定义两个需要识别的文本模式 ----------
# 只识别之前实际看到的异常格式：
# \uffffffe6 表示一个被异常转义的字节 e6。
# 连续的这些字节可能组成一个 UTF-8 中文字符串。
BAD_ESCAPE_PATTERN = re.compile(
    r"(?:\\uffffff[0-9a-fA-F]{2})+"
)

# 只识别 OpenSSH 明确写出的认证失败消息。
# 不把 Invalid user、连接断开、服务启动等消息算作失败次数。
SSH_FAILURE_PATTERN = re.compile(
    r"^Failed (?:password|publickey|keyboard-interactive"
    r"(?:/pam)?|none|hostbased) for "
)


# ---------- 3. 校验日志分析配置 ----------
def validate_log_config(config):
    """检查服务名单和三个告警开关。"""
    log_config = config.get("logs")

    if not isinstance(log_config, dict):
        raise ValueError("logs 必须是字典")

    # 复用已有服务名称校验，避免传入 Shell 不支持的名称。
    validate_names(
        {"services": log_config.get("services")},
        "services",
        r"[a-zA-Z0-9_][a-zA-Z0-9_.@-]*",
        service=True,
    )

    for field in (
        "warn_on_error_entries",
        "warn_on_ssh_login_failure",
        "warn_on_limit_reached",
    ):
        if type(log_config.get(field)) is not bool:
            raise ValueError(
                "logs.{} 必须为 true 或 false".format(field)
            )

    return log_config


# ---------- 4. 修复已经确认的异常中文转义 ----------
def repair_legacy_escapes(raw):
    """将特定异常字节序列还原为 UTF-8，再安全放回 JSON。"""

    def replace_sequence(match):
        # 从整段异常文本中提取每个字节的两位十六进制数。
        # 例如 e6、8f、90，组合后可以解码为一个汉字。
        hex_values = re.findall(
            r"\\uffffff([0-9a-fA-F]{2})",
            match.group(0),
        )

        byte_values = bytes(int(value, 16) for value in hex_values)

        # 严格解码：如果这些字节不能组成 UTF-8，就报错。
        # 不用忽略错误的方式静默丢字。
        decoded = byte_values.decode("utf-8")

        # json.dumps 负责转义引号、反斜杠等特殊字符。
        # [1:-1] 去掉最外层引号，便于放回原来的 JSON 字符串。
        return json.dumps(decoded, ensure_ascii=True)[1:-1]

    repaired = BAD_ESCAPE_PATTERN.search(raw) is not None

    if repaired:
        raw = BAD_ESCAPE_PATTERN.sub(replace_sequence, raw)

    return raw, repaired


# ---------- 5. 解析一条日志 ----------
def parse_entry(raw):
    """获取一条日志的字典和是否修复过编码的标记。"""
    if not isinstance(raw, str) or not raw:
        raise ValueError("日志记录缺失或为空")

    normalized, repaired = repair_legacy_escapes(raw)
    entry = json.loads(normalized)

    if not isinstance(entry, dict):
        raise ValueError("日志 JSON 必须是字典")

    message = entry.get("MESSAGE")

    # journalctl 有时会用整数数组表示非文本内容。
    # 数组必须是有效字节，而且能严格解码为 UTF-8 才接受。
    if isinstance(message, list):
        if not all(
            type(value) is int and 0 <= value <= 255
            for value in message
        ):
            raise ValueError("MESSAGE 数组不是有效的字节数据")

        message = bytes(message).decode("utf-8")
        entry["MESSAGE"] = message

    if not isinstance(message, str):
        raise ValueError("MESSAGE 缺失或不是可读文本")

    return entry, repaired


# ---------- 6. 分析一组日志 ----------
def analyze_group(data, prefix, target, log_config, max_entries, ssh=False):
    """一组日志生成内容检查，必要时再加入完整性与编码提示。"""
    try:
        count = read_count(data, prefix + "_entry_count")
        limit_reached = data.get(prefix + "_limit_reached")

        if limit_reached not in ("0", "1"):
            raise ValueError("日志上限标记必须是 0 或 1")

        if count > max_entries:
            raise ValueError("返回条数超过采集器声明的上限")

        # 与 log.sh 的标记方式保持一致。
        expected_limit = "1" if count >= max_entries else "0"

        if limit_reached != expected_limit:
            raise ValueError("日志条数与上限标记矛盾")

        # 确认条数和实际 entry 字段数量相符。
        record_pattern = re.compile(
            re.escape(prefix) + r"_entry_[0-9]+"
        )

        record_keys = [
            key for key in data
            if record_pattern.fullmatch(key)
        ]

        if len(record_keys) != count:
            raise ValueError("日志条数与实际记录数量不一致")

        messages = []
        failed_messages = []
        repaired_count = 0

        for index in range(1, count + 1):
            key = "{}_entry_{}".format(prefix, index)

            try:
                entry, repaired = parse_entry(data.get(key))
            except ValueError as exc:
                raise ValueError(
                    "{} 无法解析：{}".format(key, exc)
                )

            if repaired:
                repaired_count += 1

            message = entry["MESSAGE"]
            messages.append(message)

            if ssh:
                # 先确认消息来源，再匹配失败文本。
                # 避免把其他程序提到 Failed password 的文字算进去。
                identifier = entry.get("SYSLOG_IDENTIFIER")
                command = entry.get("_COMM")

                if (
                    (identifier == "sshd" or command == "sshd")
                    and SSH_FAILURE_PATTERN.match(message)
                ):
                    failed_messages.append(message)
            else:
                # 系统和服务错误组应来自 journalctl -p err。
                # PRIORITY 0～3 分别对应 emerg、alert、crit、err。
                priority = entry.get("PRIORITY")

                if priority not in ("0", "1", "2", "3"):
                    raise ValueError(
                        "{} 的错误日志级别不符合预期".format(key)
                    )

    except ValueError as exc:
        # 解析不完整时，不继续输出“零条异常”的正常结论。
        return [
            make_check("logs", target, "FAIL", None, str(exc))
        ]

    # ---------- 6.1 判断本组日志内容 ----------
    if ssh:
        problem_count = len(failed_messages)
        warning_enabled = log_config["warn_on_ssh_login_failure"]
        examples = failed_messages[-5:]
        description = "返回的 SSH 日志中识别到 {} 条明确认证失败记录".format(
            problem_count
        )
    else:
        problem_count = count
        warning_enabled = log_config["warn_on_error_entries"]
        examples = messages[-5:]
        description = "返回了 {} 条错误及更严重级别的日志".format(count)

    if problem_count > 0 and warning_enabled:
        status = "WARN"
    else:
        status = "PASS"

    if problem_count > 0 and not warning_enabled:
        description += "；该内容告警已关闭，不代表问题已消除"

    description += "；仅依据本次可用记录，不保证覆盖完整查询时段"

    # 限制示例数量和长度，避免报告被长日志占满。
    # 原始完整记录仍保留在采集器结果中。
    examples = [
        text if len(text) <= 500 else text[:500] + "……[示例截断]"
        for text in examples
    ]

    checks = [
        make_check(
            "logs",
            target,
            status,
            {
                "returned_entries": count,
                "matched_problem_entries": problem_count,
                "examples": examples,
            },
            description,
        )
    ]

    # ---------- 6.2 单独提示采集条数达到上限 ----------
    if limit_reached == "1":
        enabled = log_config["warn_on_limit_reached"]

        checks.append(
            make_check(
                "logs",
                target + "_limit",
                "WARN" if enabled else "PASS",
                max_entries,
                "达到返回上限，可能有未返回记录"
                + ("；上限告警已关闭" if not enabled else ""),
            )
        )

    # ---------- 6.3 说明本组日志修复过异常编码 ----------
    if repaired_count:
        checks.append(
            make_check(
                "logs",
                target + "_encoding",
                "WARN",
                repaired_count,
                "发现并按 UTF-8 字节修复异常转义；原始采集内容未修改",
            )
        )

    return checks


# ---------- 7. 组织系统、SSH 和指定服务日志分析 ----------
def analyze_logs(collected, log_config):
    """先检查公共信息，再分别处理各组日志。"""
    try:
        data = get_data(collected)

        if data.get("log_source") != "systemd_journal":
            raise ValueError("日志来源缺失或不受支持")

        start_epoch = read_count(data, "log_start_epoch")
        end_epoch = read_count(data, "log_end_epoch")
        lookback = read_count(data, "log_lookback_seconds")
        max_entries = read_count(data, "log_max_entries")
        service_count = read_count(data, "service_log_count")

        if lookback <= 0 or end_epoch - start_epoch != lookback:
            raise ValueError("日志查询时间范围不一致")

        if max_entries <= 0:
            raise ValueError("日志返回上限必须大于零")

        if service_count != len(log_config["services"]):
            raise ValueError("返回的服务日志组数与配置不一致")

        # 先确认所有服务名，防止把其他服务的日志误套过来。
        for index, name in enumerate(log_config["services"], start=1):
            service_name = (
                name if name.endswith(".service") else name + ".service"
            )

            key = "service_log_{}_name".format(index)

            if data.get(key) != service_name:
                raise ValueError("服务日志名称与配置不一致")

    except ValueError as exc:
        return [
            make_check("logs", "日志采集", "FAIL", None, str(exc))
        ]

    checks = []

    checks.extend(
        analyze_group(
            data, "system_error", "system_errors",
            log_config, max_entries,
        )
    )

    checks.extend(
        analyze_group(
            data, "ssh", "ssh_authentication",
            log_config, max_entries, ssh=True,
        )
    )

    for index, name in enumerate(log_config["services"], start=1):
        checks.extend(
            analyze_group(
                data,
                "service_log_{}".format(index),
                "service_errors:" + name,
                log_config,
                max_entries,
            )
        )

    # 系统错误与服务错误可能包含同一条日志。
    # 本模块分别展示检查结果，不将各组数量相加称为总错误数。
    return checks


# ---------- 8. 提供独立运行入口 ----------
def main():
    """读取配置、调用 log.sh、分析并打印结果。"""
    try:
        config = load_config()
        log_config = validate_log_config(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps(
            {"error": "配置读取或校验失败：{}".format(exc)},
            ensure_ascii=True,
            indent=2,
        ))
        return 2

    # 将 logs.services 中的名单传给 log.sh。
    # 当前配置等价于：bash log.sh sshd crond。
    collected = collect(
        "log.sh",
        script_args=log_config["services"],
        timeout_seconds=config["collector"]["timeout_seconds"],
    )

    checks = analyze_logs(collected, log_config)
    overall_status = summarize(checks)

    print(json.dumps(
        {
            "scope": "available_journal_records",
            "checks": checks,
            "overall_status": overall_status,
        },
        ensure_ascii=True,
        indent=2,
    ))

    return {"PASS": 0, "WARN": 1, "FAIL": 2}[overall_status]


# ---------- 9. 直接执行时启动分析 ----------
if __name__ == "__main__":
    raise SystemExit(main())