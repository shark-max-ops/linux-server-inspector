# -*- coding: utf-8 -*-

# 文件用途：
# 把巡检结果整理成中文 TXT 报告，保存到指定文件夹。
# 直接运行本文件时，使用模拟数据生成演示报告，不进行真实巡检。

"""
整体执行流程：
开始
  ↓
接收主机信息、检查结果和报告保存目录
  ↓
检查数据格式
  ├─ 主机信息不是字典 → 报错
  ├─ 检查结果为空或格式不正确 → 报错
  └─ 存在不认识的状态 → 报错
  ↓
统计 PASS、WARN、FAIL 数量，计算总体状态
  ↓
组织报告内容
  ├─ 报告生成时间和主机信息
  ├─ 总体状态与各状态数量
  └─ 每项检查的状态、实际值和说明
  ↓
创建报告保存目录
  ↓
先将完整报告写入临时文件
  ├─ 写入失败 → 清理临时文件，向调用者报错
  ↓
将临时文件重命名为正式 TXT 报告
  ↓
返回报告的完整路径
"""

# 主要变量：
# PROJECT_ROOT：项目根目录。
# CATEGORY_NAMES：英文分类与中文名称的对应关系。
# system_info：系统基本信息，例如主机名和操作系统。
# checks：分析模块返回的检查结果列表。
# counts：PASS、WARN、FAIL 各自的数量。
# overall_status：根据全部检查结果得到的总体状态。
# lines：按顺序存放报告的每一行文字。
# report_dir：保存报告的文件夹。
# report_path：最终报告文件的完整路径。
# temp_path：写入过程中使用的临时文件路径。

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from analyzer import summarize


# ---------- 1. 定义项目路径和分类名称 ----------

# 根据代码文件的位置确定根目录。
# 不依赖 Xshell 当前位于哪个文件夹。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CATEGORY_NAMES = {
    "system": "系统",
    "cpu": "CPU",
    "memory": "内存",
    "disk": "磁盘",
    "process": "进程",
    "service": "服务",
    "network": "网络",
    "security": "安全",
    "logs": "日志",
}


# ---------- 2. 将一个值转换成适合报告展示的文字 ----------

def display_value(value):
    """保留中文，并将换行等控制字符显示为转义文字。"""

    # None 表示没有获得有效值，不应显示成数字 0。
    if value is None:
        return "无有效数据"

    # 字典、列表、字符串等统一通过 JSON 转换。
    # ensure_ascii=False 让中文直接显示，而不是显示为 \\uXXXX。
    # allow_nan=False 防止将 NaN、Infinity 写成有效数值。
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )

    # 字符串转换后外面会有一对双引号。
    # 去掉外层引号，但保留内部的换行转义等内容。
    if isinstance(value, str):
        return text[1:-1]

    return text


# ---------- 3. 检查传入数据是否符合报告要求 ----------

def validate_report_data(system_info, checks):
    """提前检查输入，避免把不完整的结果写成正常报告。"""

    if not isinstance(system_info, dict):
        raise ValueError("系统信息必须是字典")

    # 空列表不能被展示为“全部通过”。
    if not isinstance(checks, list) or not checks:
        raise ValueError("检查结果必须是非空列表")

    for index, check in enumerate(checks, start=1):
        if not isinstance(check, dict):
            raise ValueError("第 {} 项检查必须是字典".format(index))

        # 这三个字段都需要有实际文字。
        for field in ("category", "target", "reason"):
            value = check.get(field)

            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "第 {} 项检查的 {} 必须是非空字符串".format(
                        index, field
                    )
                )

        # 只接受项目约定的三种状态。
        if check.get("status") not in ("PASS", "WARN", "FAIL"):
            raise ValueError(
                "第 {} 项检查的状态不合法".format(index)
            )

        # value 可以是 None，但不能连这个字段都没有。
        if "value" not in check:
            raise ValueError(
                "第 {} 项检查缺少 value 字段".format(index)
            )


# ---------- 4. 把数据组织成完整的报告文字 ----------

def render_report(system_info, checks, generated_at, demo=False):
    """生成报告内容，暂时不写入磁盘。"""

    validate_report_data(system_info, checks)

    # 先为三种状态准备计数器，然后逐项累加。
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}

    for check in checks:
        counts[check["status"]] += 1

    # 使用已有的汇总规则，避免不同模块采用不同规则。
    overall_status = summarize(checks)

    # 演示报告明确标注，避免与真实巡检报告混淆。
    title = "Linux 服务器巡检报告"

    if demo:
        title += "（模拟数据，仅用于验证报告功能）"

    lines = [
        title,
        "=" * 60,
        "报告生成时间：{}".format(display_value(generated_at)),
        "",
        "一、主机信息",
    ]

    # 左边是采集数据中的字段名，右边是报告里的中文名称。
    info_fields = [
        ("hostname", "主机名"),
        ("os", "操作系统"),
        ("kernel", "内核版本"),
        ("time", "系统信息采集时间"),
        ("uptime_seconds", "运行时长（秒）"),
        ("user", "采集用户"),
    ]

    for field, label in info_fields:
        value = system_info.get(field)

        # 主机信息缺失时明确显示“未获取”，不自行猜测。
        if value is None or value == "":
            value = "未获取"

        lines.append("{}：{}".format(label, display_value(value)))

    lines.extend([
        "",
        "二、检查汇总",
        "总体状态：{}".format(overall_status),
        "检查数量：{}".format(len(checks)),
        "PASS：{}".format(counts["PASS"]),
        "WARN：{}".format(counts["WARN"]),
        "FAIL：{}".format(counts["FAIL"]),
        "",
        "三、检查明细",
    ])

    # 保留传入检查项的顺序，逐项写出状态、值和原因。
    for index, check in enumerate(checks, start=1):
        category = CATEGORY_NAMES.get(
            check["category"], check["category"]
        )

        lines.extend([
            "",
            "{}. [{}] {} / {}".format(
                index,
                check["status"],
                display_value(category),
                display_value(check["target"]),
            ),
            "实际值：{}".format(display_value(check["value"])),
            "说明：{}".format(display_value(check["reason"])),
        ])

    lines.extend([
        "",
        "四、结果说明",
        "PASS：本次检查未触发对应规则。",
        "WARN：发现需要核对的情况，具体原因见检查明细。",
        "FAIL：检查不满足要求，或采集、数据处理发生错误。",
        "结果仅反映本次采集范围，不代表对系统全部情况的保证。",
    ])

    # 用真正的换行符连接每一行，并在文件末尾保留换行。
    return "\n".join(lines) + "\n"


# ---------- 5. 将报告保存为 UTF-8 文本文件 ----------

def write_report(system_info, checks, report_dir, demo=False):
    """写入报告，成功后返回正式文件的 Path 对象。"""

    # 使用带时区的本地时间，方便确定报告生成时间。
    now = datetime.now().astimezone()
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S %z")

    # 先生成全部文字。
    # 如果数据不合法，在创建文件前就会报错。
    content = render_report(
        system_info, checks, generated_at, demo=demo
    )

    # 相对目录统一按项目根目录解释。
    # 例如 reports 会定位到项目根目录下的 reports 文件夹。
    report_dir = Path(report_dir)

    if not report_dir.is_absolute():
        report_dir = PROJECT_ROOT / report_dir

    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    # 时间加随机标识，避免短时间内重复运行时覆盖旧报告。
    prefix = "demo_report" if demo else "inspection"
    filename = "{}_{}_{}.txt".format(
        prefix,
        now.strftime("%Y%m%d_%H%M%S"),
        uuid4().hex,
    )
    report_path = report_dir / filename

    temp_path = None

    try:
        # 临时文件与正式文件放在同一个文件夹。
        # 写完并关闭后，再重命名成正式报告。
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(report_dir),
            prefix=".report_",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)

        # 避免其他程序把还没写完的内容当成正式报告。
        os.replace(str(temp_path), str(report_path))
        temp_path = None

    finally:
        # 如果写入或重命名失败，清理本次留下的临时文件。
        # 原来的错误仍会继续向调用者传递。
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    return report_path


# ---------- 6. 使用模拟数据验证报告功能 ----------

def main():
    """直接运行时生成演示报告，不调用采集脚本。"""

    system_info = {
        "hostname": "demo-host",
        "os": "演示操作系统",
        "kernel": "演示内核",
        "time": "模拟采集时间",
        "uptime_seconds": "3600",
        "user": "demo-user",
    }

    # 故意放入两个 PASS 和一个 WARN，检查汇总和中文展示。
    checks = [
        {
            "category": "cpu",
            "target": "CPU",
            "status": "PASS",
            "value": 12.5,
            "reason": "使用率低于警告阈值 70%",
        },
        {
            "category": "service",
            "target": "sshd.service",
            "status": "PASS",
            "value": {
                "active_state": "active",
                "sub_state": "running",
            },
            "reason": "服务处于运行状态",
        },
        {
            "category": "security",
            "target": "ssh_permit_root_login",
            "status": "WARN",
            "value": "yes",
            "reason": "配置允许 root 登录；这是模拟告警",
        },
    ]

    try:
        report_path = write_report(
            system_info,
            checks,
            PROJECT_ROOT / "reports",
            demo=True,
        )
    except (OSError, ValueError, TypeError) as exc:
        print("演示报告生成失败：{}".format(exc))
        return 2

    print("演示报告已生成：{}".format(report_path))
    print("报告使用模拟数据，不代表当前服务器状态。")

    # 这里的 0 表示“演示报告生成成功”。
    # 将来真实巡检的退出码，由 main.py 按真实总体状态决定。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())