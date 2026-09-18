# -*- coding: utf-8 -*-

# 文件用途：
# 作为服务器巡检的统一入口，读取配置，调用采集和分析模块，
# 最后先生成真实的中文 TXT 报告，再尝试发送钉钉告警，
# 并返回巡检总体状态对应的退出码。

"""
整体执行流程：
开始
  ↓
读取 config.yaml，检查各模块配置和报告保存目录
  ├─ 配置错误 → 输出错误，退出码 2
  ↓
采集系统基本信息
  ├─ 采集失败或必要字段缺失 → 记录 FAIL，继续其他检查
  ↓
采集并分析 CPU、内存和磁盘
  ↓
按照配置采集并分析进程、服务
  ├─ 对象列表为空 → 跳过对应检查
  ↓
采集并分析网络、安全和日志
  ├─ 单项采集失败 → 由分析模块记录 FAIL，继续其他检查
  ↓
合并全部检查结果，计算总体状态
  ↓
生成并保存中文 TXT 报告
  ├─ 保存失败 → 输出错误，退出码 2
  ↓
原子保存最近巡检 JSON 快照，供清理通知读取
  ├─ 保存失败 → 记录错误，不删除已保存报告
  ↓
使用内存中的 system_info 和 checks 尝试发送钉钉告警
  ├─ 开关关闭或无到期故障 → 记录结果后继续
  ├─ 通知失败 → 明确记录，但不覆盖巡检状态和报告
  └─ 通知成功 → 更新对应故障的最近成功发送时间
  ↓
打印总体状态、检查数量和报告路径
  ↓
返回退出码：PASS 为 0，WARN 为 1，FAIL 为 2
"""

# 主要变量：
# config：从 config.yaml 读取的完整配置。
# timeout_seconds：单个 Shell 采集脚本的超时时间。
# network_config：经过检查的网络配置。
# security_config：经过检查的安全配置。
# log_config：经过检查的日志配置。
# report_dir：报告保存目录。
# system_info：系统基本信息，供报告展示。
# checks：所有模块的检查结果列表。
# collected：某个 Shell 脚本的采集结果。
# overall_status：本次巡检的总体状态。
# report_path：生成的 TXT 报告路径。
# notification_result：钉钉通知模块返回的发送或抑制结果。
# snapshot_path：供定时清理通知读取的最近巡检 JSON 快照路径。

import sys

import yaml

from collector import collect

from analyzer import (
    analyze_disk,
    analyze_process,
    analyze_service,
    analyze_single,
    get_data,
    load_config,
    make_check,
    summarize,
)

from network_analyzer import (
    analyze_network,
    validate_network_config,
)

from security_analyzer import (
    analyze_security,
    validate_security_config,
)

from log_analyzer import (
    analyze_logs,
    validate_log_config,
)

from reporter import write_report

from notifier import (
    NotificationError,
    process_notifications,
    redact_sensitive,
    save_latest_inspection,
)


# ---------- 1. 检查报告保存目录的配置 ----------

def validate_output_config(config):
    """确认配置中提供了有效的报告目录字符串。"""

    output_config = config.get("output")

    if not isinstance(output_config, dict):
        raise ValueError("output 必须是配置字典")

    report_dir = output_config.get("report_dir")

    # 这里检查配置的类型和内容。
    # 实际能否创建目录、写入文件，由报告模块在保存时检查。
    if not isinstance(report_dir, str) or not report_dir.strip():
        raise ValueError("output.report_dir 必须是非空字符串")

    # 路径中不能包含空字符，否则文件操作会失败。
    if "\x00" in report_dir:
        raise ValueError("output.report_dir 不能包含空字符")

    return report_dir


# ---------- 2. 采集系统信息，同时生成一条检查结果 ----------

def inspect_system_info(timeout_seconds):
    """返回主机信息字典，以及系统信息采集的检查结果列表。"""

    collected = collect(
        "system_info.sh",
        timeout_seconds=timeout_seconds,
    )

    # 即使系统信息采集失败，也给后续报告提供一个空字典。
    # 报告模块会把缺失的主机信息显示为“未获取”。
    system_info = {}

    try:
        data = get_data(collected)

        # 先保留已获取的非空文字，便于报告展示。
        # 某个字段缺失时，不必丢弃其他有效字段。
        required_fields = (
            "time",
            "hostname",
            "os",
            "kernel",
            "uptime_seconds",
            "user",
        )

        for field in required_fields:
            value = data.get(field)

            if isinstance(value, str) and value.strip():
                system_info[field] = value

        missing_fields = [
            field
            for field in required_fields
            if field not in system_info
        ]

        if missing_fields:
            raise ValueError(
                "系统信息缺少有效字段：{}".format(
                    ", ".join(missing_fields)
                )
            )

        # 运行秒数应由数字组成，不能是负数或任意文字。
        uptime = system_info["uptime_seconds"]

        if not uptime or any(char not in "0123456789" for char in uptime):
            raise ValueError("uptime_seconds 必须是非负整数")

    except ValueError as exc:
        check = make_check(
            "system",
            "system_info",
            "FAIL",
            None,
            str(exc),
        )
        return system_info, [check]

    # PASS 仅表示系统信息采集完整，不表示整台服务器没有问题。
    check = make_check(
        "system",
        "system_info",
        "PASS",
        system_info["hostname"],
        "系统基本信息采集完整",
    )

    return system_info, [check]


# ---------- 3. 依次执行资源、进程和服务检查 ----------

def inspect_resources_and_services(config):
    """采集并分析 CPU、内存、磁盘、进程和服务。"""

    checks = []
    timeout_seconds = config["collector"]["timeout_seconds"]

    # CPU 和内存的分析方式相同：
    # 从采集结果中取出使用率，与对应阈值比较。
    usage_tasks = (
        ("cpu.sh", "cpu", "CPU", "cpu_usage_percent"),
        ("memory.sh", "memory", "Memory", "memory_usage_percent"),
    )

    for script, category, target, field in usage_tasks:
        collected = collect(
            script,
            timeout_seconds=timeout_seconds,
        )

        check = analyze_single(
            collected,
            category,
            target,
            field,
            config["thresholds"][category],
        )

        # 单项分析返回一个字典，因此使用 append。
        checks.append(check)

    # 磁盘可能有多个挂载点，因此返回的是检查结果列表。
    collected = collect(
        "disk.sh",
        timeout_seconds=timeout_seconds,
    )

    checks.extend(
        analyze_disk(collected, config["thresholds"]["disk"])
    )

    # 空进程名单表示没有配置必需进程，不调用该采集脚本。
    process_names = config["processes"]

    if process_names:
        collected = collect(
            "process.sh",
            script_args=process_names,
            timeout_seconds=timeout_seconds,
        )

        checks.extend(
            analyze_process(collected, process_names)
        )

    # 服务名称作为参数传给 service.sh。
    # 服务分析函数会检查返回名称和状态是否符合要求。
    service_names = config["services"]

    if service_names:
        collected = collect(
            "service.sh",
            script_args=service_names,
            timeout_seconds=timeout_seconds,
        )

        checks.extend(
            analyze_service(collected, service_names)
        )

    return checks


# ---------- 4. 执行网络、安全和日志检查 ----------

def inspect_network_security_logs(
    timeout_seconds,
    network_config,
    security_config,
    log_config,
):
    """调用三个独立分析模块，并合并它们的结果。"""

    checks = []

    # 网络采集只读取监听等信息。
    # 必需端口和允许端口，由网络分析模块根据配置判断。
    collected = collect(
        "network.sh",
        timeout_seconds=timeout_seconds,
    )

    checks.extend(
        analyze_network(collected, network_config)
    )

    # 安全采集读取防火墙状态和 SSH 配置。
    # 不修改防火墙或 SSH 设置。
    collected = collect(
        "security.sh",
        timeout_seconds=timeout_seconds,
    )

    checks.extend(
        analyze_security(collected, security_config)
    )

    # 日志脚本总会查询系统错误和 SSH 日志。
    # 配置中的 services 决定额外查询哪些服务的错误日志。
    collected = collect(
        "log.sh",
        script_args=log_config["services"],
        timeout_seconds=timeout_seconds,
    )

    checks.extend(
        analyze_logs(collected, log_config)
    )

    return checks


# ---------- 5. 统一入口：检查配置、执行巡检、保存报告 ----------

def main():
    """执行一次完整巡检，并返回对应退出码。"""

    try:
        # 先完成全部配置检查，再开始调用采集脚本。
        config = load_config()

        network_config = validate_network_config(config)
        security_config = validate_security_config(config)
        log_config = validate_log_config(config)

        report_dir = validate_output_config(config)
        timeout_seconds = config["collector"]["timeout_seconds"]

    except (OSError, ValueError, yaml.YAMLError) as exc:
        print("巡检配置错误：{}".format(exc), file=sys.stderr)
        return 2

    print("开始巡检……", flush=True)

    # 先取得主机信息和系统采集检查结果。
    print("正在采集系统基本信息……", flush=True)

    system_info, checks = inspect_system_info(timeout_seconds)

    # 将资源、进程和服务结果添加到总列表。
    print("正在检查 CPU、内存、磁盘、进程和服务……", flush=True)

    checks.extend(
        inspect_resources_and_services(config)
    )

    # 将三个独立分析模块的结果也添加到总列表。
    print("正在检查网络、安全和日志……", flush=True)

    checks.extend(
        inspect_network_security_logs(
            timeout_seconds,
            network_config,
            security_config,
            log_config,
        )
    )

    overall_status = summarize(checks)

    try:
        # demo=False 表示保存真实巡检报告。
        # 相对目录 reports 由 reporter 按项目根目录解释。
        report_path = write_report(
            system_info,
            checks,
            report_dir,
            demo=False,
        )

    except (OSError, ValueError, TypeError) as exc:
        print("报告保存失败：{}".format(exc), file=sys.stderr)
        print(
            "已完成分析，总体状态：{}".format(overall_status),
            file=sys.stderr,
        )
        return 2

    # 报告成功保存后，再原子保存同一批内存结果的最新快照。
    # 清理通知读取该 JSON，不解析 TXT，也不重复计算阈值。
    try:
        snapshot_path = save_latest_inspection(
            config,
            system_info,
            checks,
        )
        print("巡检快照：{}".format(snapshot_path))
    except NotificationError as exc:
        print(
            "巡检快照保存失败（巡检报告已保存）：{}".format(
                redact_sensitive(exc)
            ),
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            "巡检快照发生未预期异常（巡检报告已保存）：{}".format(
                type(exc).__name__
            ),
            file=sys.stderr,
        )

    # 终端打印简要结果，详细内容放在报告中。
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}

    for check in checks:
        counts[check["status"]] += 1

    print("")
    print("巡检完成")
    print("总体状态：{}".format(overall_status))
    print("检查数量：{}".format(len(checks)))
    print(
        "PASS：{}，WARN：{}，FAIL：{}".format(
            counts["PASS"],
            counts["WARN"],
            counts["FAIL"],
        )
    )
    print("报告路径：{}".format(report_path))

    # 报告已经成功落盘后才调用通知模块。
    # 通知配置、状态文件或网络失败只记录到运行日志，
    # 不改变本次巡检由 checks 决定的原始退出码。
    try:
        notification_result = process_notifications(
            system_info,
            checks,
            config,
        )
        print("钉钉通知：{}".format(notification_result["message"]))
    except NotificationError as exc:
        print(
            "钉钉通知失败（巡检报告已保存）：{}".format(
                redact_sensitive(exc)
            ),
            file=sys.stderr,
        )
    except Exception as exc:
        # 未预期的通知异常同样不能改变原巡检结论。
        # 这里只记录异常类型，避免未知异常文字夹带凭据。
        print(
            "钉钉通知发生未预期异常（巡检报告已保存）：{}".format(
                type(exc).__name__
            ),
            file=sys.stderr,
        )

    # 与之前独立分析模块保持同样的退出码约定。
    return {"PASS": 0, "WARN": 1, "FAIL": 2}[overall_status]


if __name__ == "__main__":
    raise SystemExit(main())
