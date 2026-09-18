# -*- coding: utf-8 -*-
# ============================================================
# 文件名称：analyzer.py
#
# 文件用途：
#   读取配置，调用采集器，分析以下五类检查：
#   CPU、内存、磁盘、关键进程、必需服务。
"""
整体执行流程：
开始
  ↓
读取 config.yaml，检查阈值、采集超时、进程名和服务名
  ├─ 配置错误 → 输出错误，退出码 2
  ↓
依次调用采集器并分析结果
  │
  ├─ CPU、内存：根据使用率和阈值生成检查结果
  ├─ 磁盘：逐个分区检查使用率
  ├─ 关键进程：配置列表非空时，检查进程是否存在
  └─ 必需服务：配置列表非空时，检查加载状态和活动状态
  │
  └─ 采集失败或数据异常 → 生成 FAIL 检查结果
  ↓
合并全部检查结果
  ↓
计算总状态：FAIL 优先，其次 WARN，最后 PASS
  ↓
打印 JSON
  ↓
返回退出码：PASS 为 0，WARN 为 1，FAIL 为 2
"""
#
# 本次新增：
#   1. 检查配置中的进程名和服务名是否有效。
#   2. 判断关键进程是否存在。
#   3. 判断必需服务是否加载成功并处于 active 状态。
#
# 主要变量：
#   PROJECT_ROOT     项目根目录
#   CONFIG_PATH      配置文件路径
#   config           读取后的全部配置
#   thresholds       CPU、内存、磁盘的阈值
#   rule             某项阈值，例如 {"warn": 70, "fail": 90}
#   collected        collect() 返回的完整采集结果
#   data             采集结果中的数据字典
#   checks           保存各项分析结果的列表
#   raw_value        尚未转换的使用率
#   value            转换后的数字，或进程、服务的实际状态
#   expected_names   配置中要求检查的进程名或服务名
#   prefix           编号字段前缀，例如 required_1、service_1
#   count            返回的记录数量，或某进程的匹配数量
#   present          进程是否存在，字符串 "1" 或 "0"
#   load_state       服务加载状态
#   active_state     服务活动状态
#   sub_state        服务具体状态
#   unit_file_state  服务启用配置状态
#   overall_status   本次检查整体状态
#
# 每条检查结果：
#   category  类别
#   target    检查对象
#   status    PASS / WARN / FAIL
#   value     实际值；没有有效数据时为 None
#   reason    判断原因
#
# 退出码：
#   0：全部 PASS
#   1：有 WARN，没有 FAIL
#   2：有 FAIL，或配置出错
#
# 运行环境：
#   Linux 项目虚拟环境，Python 3.6。
# ============================================================


# ---------- 1. 导入需要的工具 ----------
import json
import math
import re
from pathlib import Path

import yaml

# 复用之前写好的采集函数，不在分析层重复执行命令的逻辑。
from collector import collect


# ---------- 2. 确定配置文件位置 ----------
# 不依赖终端当前所在目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


# ---------- 3. 将输入转换为有限数字 ----------
def to_number(raw, field_name):
    """把数字字符串转换成小数，并拒绝无效输入。"""

    # True 虽然可以转换为 1，但不能当作真实使用率。
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError("{} 必须是数字".format(field_name))

    try:
        value = float(raw)
    except (ValueError, OverflowError):
        raise ValueError("{} 无法转换为数字".format(field_name))

    # 拒绝 NaN 和无穷大，避免比较阈值时出现错误判断。
    if not math.isfinite(value):
        raise ValueError("{} 不能是 NaN 或无穷大".format(field_name))

    return value


# ---------- 4. 检查进程和服务名称列表 ----------
def validate_names(config, key, pattern, service=False):
    """检查名称是否符合 Shell 脚本接受的格式。"""

    # key 为 processes 或 services。
    names = config.get(key)

    if not isinstance(names, list):
        raise ValueError("{} 必须是列表".format(key))

    # 保存已经出现过的名字，用来发现重复配置。
    seen = set()

    for name in names:
        if not isinstance(name, str) or re.fullmatch(pattern, name) is None:
            raise ValueError(
                "{} 中存在不支持的名称：{}".format(key, name)
            )

        # 对服务名称统一补后缀，再检查重复。
        # sshd 和 sshd.service 实际是同一个检查对象。
        normalized = name

        if service and not name.endswith(".service"):
            normalized = name + ".service"

        if normalized in seen:
            raise ValueError(
                "{} 中存在重复名称：{}".format(key, name)
            )

        seen.add(normalized)


# ---------- 5. 读取和校验当前阶段使用的配置 ----------
def load_config():
    """读取 YAML，检查阈值、超时、进程名和服务名。"""

    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    if not isinstance(config, dict):
        raise ValueError("配置文件顶层必须是字典")

    thresholds = config.get("thresholds")

    if not isinstance(thresholds, dict):
        raise ValueError("thresholds 必须是字典")

    # 检查三个资源阈值块。
    for category in ("cpu", "memory", "disk"):
        rule = thresholds.get(category)

        if not isinstance(rule, dict):
            raise ValueError("缺少 {} 阈值配置".format(category))

        for name in ("warn", "fail"):
            if type(rule.get(name)) not in (int, float):
                raise ValueError(
                    "{}.{} 必须是数字".format(category, name)
                )

        warn = to_number(rule["warn"], category + ".warn")
        fail = to_number(rule["fail"], category + ".fail")

        if not 0 <= warn < fail <= 100:
            raise ValueError(
                "{} 阈值必须满足 0 <= warn < fail <= 100".format(
                    category
                )
            )

    # 检查采集超时。
    collector_config = config.get("collector")

    if not isinstance(collector_config, dict):
        raise ValueError("collector 必须是字典")

    timeout_seconds = collector_config.get("timeout_seconds")

    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须是正整数")

    # 名称规则与 process.sh、service.sh 中的规则保持一致。
    validate_names(
        config,
        "processes",
        r"[a-zA-Z0-9_.-]{1,15}",
    )

    validate_names(
        config,
        "services",
        r"[a-zA-Z0-9_][a-zA-Z0-9_.@-]*",
        service=True,
    )

    return config


# ---------- 6. 生成一条统一格式的检查结果 ----------
def make_check(category, target, status, value, reason):
    """资源、进程和服务都使用同样的结果字段。"""
    return {
        "category": category,
        "target": target,
        "status": status,
        "value": value,
        "reason": reason,
    }


# ---------- 7. 判断资源使用率 ----------
def check_usage(category, target, raw_value, rule, upper_limit=100):
    """先校验使用率，再与 warn、fail 比较。"""
    try:
        value = to_number(raw_value, target)

        if value < 0:
            raise ValueError("使用率不能为负数")

        if upper_limit is not None and value > upper_limit:
            raise ValueError(
                "使用率不能超过 {}".format(upper_limit)
            )

    except ValueError as exc:
        # 无效数据不用 0 替代，明确报告失败。
        return make_check(
            category, target, "FAIL", None,
            "数据异常：{}".format(exc),
        )

    warn = rule["warn"]
    fail = rule["fail"]

    if value >= fail:
        status = "FAIL"
        reason = "使用率达到失败阈值 {}%".format(fail)
    elif value >= warn:
        status = "WARN"
        reason = "使用率达到警告阈值 {}%".format(warn)
    else:
        status = "PASS"
        reason = "使用率低于警告阈值 {}%".format(warn)

    return make_check(category, target, status, value, reason)


# ---------- 8. 从完整采集结果中提取 data ----------
def get_data(collected):
    """采集成功才能继续分析，失败时保留原始错误。"""
    if collected.get("ok") is not True:
        reason = collected.get("error") or "未知采集错误"
        stderr = collected.get("stderr")

        if stderr:
            reason = "{}；{}".format(reason, stderr)

        raise ValueError("采集失败：{}".format(reason))

    data = collected.get("data")

    if not isinstance(data, dict) or not data:
        raise ValueError("采集数据为空或格式不正确")

    return data


# ---------- 9. 读取非负整数形式的计数字段 ----------
def read_count(data, field):
    """将 '3' 转换成 3；字段缺失或内容异常时报告错误。"""
    raw = data.get(field)

    # 只允许 0～9，不接受负数、小数或空值。
    if not isinstance(raw, str) or re.fullmatch(r"[0-9]+", raw) is None:
        raise ValueError("{} 必须是非负整数字符串".format(field))

    return int(raw)


# ---------- 10. 分析 CPU 或内存 ----------
def analyze_single(collected, category, target, field, rule):
    """读取一个使用率字段，并调用统一的阈值判断。"""
    try:
        data = get_data(collected)
    except ValueError as exc:
        return make_check(
            category, target, "FAIL", None, str(exc)
        )

    return check_usage(
        category, target, data.get(field), rule
    )


# ---------- 11. 分析各个磁盘挂载点 ----------
def analyze_disk(collected, rule):
    """逐块检查磁盘，确保根目录没有遗漏。"""
    checks = []

    try:
        data = get_data(collected)
        disk_count = read_count(data, "disk_count")

        if disk_count <= 0:
            raise ValueError("没有可检查的磁盘文件系统")

        mount_keys = []

        for key in data:
            if key.startswith("disk_") and key.endswith("_mount"):
                mount_keys.append(key)

        if len(mount_keys) != disk_count:
            raise ValueError("磁盘数量与挂载点字段数量不一致")

    except ValueError as exc:
        return [
            make_check("disk", "磁盘采集", "FAIL", None, str(exc))
        ]

    root_found = False

    for index in range(1, disk_count + 1):
        prefix = "disk_{}".format(index)
        mount = data.get(prefix + "_mount")

        if not isinstance(mount, str) or not mount.startswith("/"):
            checks.append(
                make_check(
                    "disk", prefix, "FAIL", None,
                    "磁盘挂载点缺失或格式异常",
                )
            )
            continue

        if mount == "/":
            root_found = True

        # df 的使用率可能超过 100，不将它当成无效数据。
        checks.append(
            check_usage(
                "disk",
                mount,
                data.get(prefix + "_usage_percent"),
                rule,
                upper_limit=None,
            )
        )

    if not root_found:
        checks.append(
            make_check(
                "disk", "/", "FAIL", None,
                "采集结果缺少根目录文件系统",
            )
        )

    return checks


# ---------- 12. 分析关键进程 ----------
def analyze_process(collected, expected_names):
    """按照配置中的名单，判断每种关键进程是否存在。"""
    checks = []

    # 配置为空列表，表示没有指定关键进程检查。
    # 不伪造一条 PASS，直接返回空结果。
    if not expected_names:
        return checks

    try:
        data = get_data(collected)
        count = read_count(data, "required_count")

        # 例如要求检查 sshd、crond，应返回两组结果。
        if count != len(expected_names):
            raise ValueError("返回的关键进程数量与配置不一致")

    except ValueError as exc:
        # 整个采集失败时，每个要求检查的对象都标记失败。
        return [
            make_check("process", name, "FAIL", None, str(exc))
            for name in expected_names
        ]

    # enumerate(..., start=1) 同时得到编号和进程名。
    # 例如第一轮：index=1，name="sshd"。
    for index, name in enumerate(expected_names, start=1):
        prefix = "required_{}".format(index)

        try:
            # 确认返回的是我们要求检查的那个进程。
            # 防止记录错位，却把另一个进程的结果套过来。
            if data.get(prefix + "_name") != name:
                raise ValueError("返回的进程名称与配置不一致")

            count = read_count(data, prefix + "_count")
            present = data.get(prefix + "_present")

            if present not in ("0", "1"):
                raise ValueError("present 字段必须是 0 或 1")

            # count 与 present 必须相互对应：
            # count > 0 时 present 应为 1；
            # count = 0 时 present 应为 0。
            expected_present = "1" if count > 0 else "0"

            if present != expected_present:
                raise ValueError("进程数量与存在标记矛盾")

        except ValueError as exc:
            checks.append(
                make_check(
                    "process", name, "FAIL", None,
                    "数据异常：{}".format(exc),
                )
            )
            continue

        # 这里的 value 保存进程匹配数量，不是使用率。
        if count > 0:
            checks.append(
                make_check(
                    "process", name, "PASS", count,
                    "找到 {} 个非僵尸进程".format(count),
                )
            )
        else:
            checks.append(
                make_check(
                    "process", name, "FAIL", 0,
                    "未找到要求存在的非僵尸进程",
                )
            )

    return checks


# ---------- 13. 分析必需服务 ----------
def analyze_service(collected, expected_names):
    """判断指定服务是否加载成功并处于 active 状态。"""
    checks = []

    if not expected_names:
        return checks

    try:
        data = get_data(collected)
        count = read_count(data, "service_count")

        if count != len(expected_names):
            raise ValueError("返回的服务数量与配置不一致")

    except ValueError as exc:
        return [
            make_check("service", name, "FAIL", None, str(exc))
            for name in expected_names
        ]

    for index, name in enumerate(expected_names, start=1):
        prefix = "service_{}".format(index)

        # service.sh 返回的名称包含 .service。
        # 配置中的 sshd 在这里转换为 sshd.service。
        service_name = name

        if not service_name.endswith(".service"):
            service_name += ".service"

        try:
            if data.get(prefix + "_name") != service_name:
                raise ValueError("返回的服务名称与配置不一致")

            # 读取服务的四种状态。
            load_state = data.get(prefix + "_load_state")
            active_state = data.get(prefix + "_active_state")
            sub_state = data.get(prefix + "_sub_state")
            unit_file_state = data.get(prefix + "_unit_file_state")

            # 字段缺失时不能猜测，更不能默认为 active。
            for field_name, field_value in (
                ("load_state", load_state),
                ("active_state", active_state),
                ("sub_state", sub_state),
                ("unit_file_state", unit_file_state),
            ):
                if not isinstance(field_value, str) or not field_value.strip():
                    raise ValueError(
                        "{} 缺失或为空".format(field_name)
                    )

        except ValueError as exc:
            checks.append(
                make_check(
                    "service", service_name, "FAIL", None,
                    "数据异常：{}".format(exc),
                )
            )
            continue

        # 把状态保存在 value 中，方便报告同时展示。
        value = {
            "load_state": load_state,
            "active_state": active_state,
            "sub_state": sub_state,
            "unit_file_state": unit_file_state,
        }

        # 分清几种失败原因，报告中不要全部只写“服务异常”。
        if load_state == "not-found":
            status = "FAIL"
            reason = "未找到该服务单元"

        elif load_state != "loaded":
            status = "FAIL"
            reason = "服务未正常加载，加载状态为 {}".format(load_state)

        elif active_state != "active":
            status = "FAIL"
            reason = "服务未处于活动状态：{} / {}".format(
                active_state, sub_state
            )

        elif sub_state == "unknown":
            status = "FAIL"
            reason = "服务活动状态为 active，但具体子状态未知"

        else:
            status = "PASS"

            # active/exited 可能是正常的一次性服务，
            # 所以不强制所有服务的子状态都必须为 running。
            reason = "服务处于 active，子状态为 {}".format(sub_state)

        # 开机启用状态只是记录，当前不以它决定 PASS/FAIL。
        reason += "；启用配置状态为 {}".format(unit_file_state)

        checks.append(
            make_check(
                "service", service_name, status, value, reason
            )
        )

    return checks


# ---------- 14. 汇总整体状态 ----------
def summarize(checks):
    """有 FAIL 就整体 FAIL，否则有 WARN 就整体 WARN。"""
    if not checks:
        return "FAIL"

    statuses = []

    for item in checks:
        statuses.append(item["status"])

    for status in statuses:
        if status not in ("PASS", "WARN", "FAIL"):
            return "FAIL"

    if "FAIL" in statuses:
        return "FAIL"

    if "WARN" in statuses:
        return "WARN"

    return "PASS"


# ---------- 15. 执行当前已实现的五类检查 ----------
def main():
    """读取配置，依次采集、分析，再打印整体结果。"""

    # 先检查配置。
    # 配置错误时不开始采集，直接说明问题。
    try:
        config = load_config()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps(
            {"error": "配置读取或校验失败：{}".format(exc)},
            ensure_ascii=True,
            indent=2,
        ))
        return 2

    thresholds = config["thresholds"]
    timeout_seconds = config["collector"]["timeout_seconds"]
    checks = []

    # 第一部分：CPU 和内存。
    cpu_result = collect(
        "cpu.sh", timeout_seconds=timeout_seconds
    )
    checks.append(
        analyze_single(
            cpu_result, "cpu", "CPU",
            "cpu_usage_percent", thresholds["cpu"],
        )
    )

    memory_result = collect(
        "memory.sh", timeout_seconds=timeout_seconds
    )
    checks.append(
        analyze_single(
            memory_result, "memory", "Memory",
            "memory_usage_percent", thresholds["memory"],
        )
    )

    # 第二部分：磁盘。
    # 一次分析返回多条结果，所以使用 extend。
    disk_result = collect(
        "disk.sh", timeout_seconds=timeout_seconds
    )
    checks.extend(
        analyze_disk(disk_result, thresholds["disk"])
    )

    # 第三部分：关键进程。
    # 配置中的列表直接传给 Shell。
    # 例如等价于：bash process.sh sshd crond。
    if config["processes"]:
        process_result = collect(
            "process.sh",
            script_args=config["processes"],
            timeout_seconds=timeout_seconds,
        )
        checks.extend(
            analyze_process(process_result, config["processes"])
        )

    # 第四部分：必需服务。
    # 例如等价于：bash service.sh sshd crond。
    if config["services"]:
        service_result = collect(
            "service.sh",
            script_args=config["services"],
            timeout_seconds=timeout_seconds,
        )
        checks.extend(
            analyze_service(service_result, config["services"])
        )

    overall_status = summarize(checks)

    # 更新范围名称，说明本次已经包含进程和服务检查。
    # 尚未加入网络、安全与日志分析。
    output = {
        "scope": "resources_processes_services",
        "checks": checks,
        "overall_status": overall_status,
    }

    print(json.dumps(
        output,
        ensure_ascii=True,
        indent=2,
    ))

    exit_codes = {"PASS": 0, "WARN": 1, "FAIL": 2}
    return exit_codes[overall_status]


# ---------- 16. 直接执行时启动检查 ----------
# 被测试文件 import 时，不自动运行 main。
if __name__ == "__main__":
    raise SystemExit(main())
