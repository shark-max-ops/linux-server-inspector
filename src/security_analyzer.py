# -*- coding: utf-8 -*-
# ============================================================
# 文件名称：security_analyzer.py
#
# 文件用途：
#   调用 security.sh，分析 firewalld 和 SSH 配置。
#   根据 config.yaml 中的安全开关，生成检查结果。
#   不修改配置，不启停服务。
"""
整体执行流程：
开始
  ↓
读取 config.yaml，检查三个安全告警开关
  ├─ 配置错误 → 输出错误，退出码 2
  ↓
调用 security.sh，获取 firewalld 状态和 SSH 全局配置
  ↓
检查采集状态和数据
  ├─ 采集失败 → 生成 FAIL 检查结果
  ↓
检查 firewalld 状态、SSH 配置范围及两项 SSH 配置值
  ├─ 字段缺失、值异常或 SSH 范围不受支持 → 对应检查 FAIL
  ↓
对有效值应用当前告警规则
  │
  ├─ firewalld 未运行：开启对应告警时 WARN
  ├─ PermitRootLogin=yes：开启对应告警时 WARN
  ├─ PasswordAuthentication=yes：开启对应告警时 WARN
  └─ 未触发规则或对应告警关闭 → PASS
  ↓
在 SSH 结果中注明磁盘全局配置及运行中配置、Match 条件的限制
  ↓
合并检查结果，计算总状态：FAIL 优先，其次 WARN，最后 PASS
  ↓
打印 JSON
  ↓
返回退出码：PASS 为 0，WARN 为 1，FAIL 为 2
"""
#
# 主要变量：
#   config             全部项目配置
#   security_config    配置中的 security 部分
#   collected          security.sh 返回的完整采集结果
#   data               采集字段字典
#   checks             安全检查结果列表
#   raw_value          某项采集值，例如 running 或 yes
#   valid_values       该字段允许出现的值
#   warning_values     需要触发警告的值
#   warning_enabled    配置是否开启该项告警
#   descriptions       每种采集值对应的中文说明
#   scope              SSH 配置的采集范围
#
# 本次安全规则：
#   firewalld 未运行：根据配置决定是否 WARN。
#   PermitRootLogin=yes：根据配置决定是否 WARN。
#   PasswordAuthentication=yes：根据配置决定是否 WARN。
#   采集失败、字段缺失或值异常：FAIL。
#
# 范围限制：
#   SSH 结果来自磁盘配置的全局解析值。
#   不代表运行中的服务已经加载最新配置。
#   不包含特定用户或地址的 Match 条件结果。
#
# 退出码：
#   0：按当前规则没有发现告警
#   1：存在 WARN，没有 FAIL
#   2：存在 FAIL，或配置错误
#
# 运行环境：
#   Linux 项目虚拟环境，Python 3.6。
# ============================================================


# ---------- 1. 导入已有工具 ----------
import json

import yaml

from collector import collect

# 复用已有函数，保证结果格式和整体状态规则一致。
from analyzer import get_data, load_config, make_check, summarize


# ---------- 2. 校验安全配置中的三个开关 ----------
def validate_security_config(config):
    """确保三个告警开关都明确写成 true 或 false。"""
    security_config = config.get("security")

    if not isinstance(security_config, dict):
        raise ValueError("security 必须是字典")

    # 这些名称与 config.yaml 中已经写好的字段一致。
    fields = (
        "warn_if_firewalld_not_running",
        "warn_if_root_login_yes",
        "warn_if_password_authentication_yes",
    )

    for field in fields:
        # 不接受字符串 "true"，也不接受数字 1。
        # 这样可以发现配置写错，而不是误用错误的规则。
        if type(security_config.get(field)) is not bool:
            raise ValueError(
                "security.{} 必须为 true 或 false".format(field)
            )

    return security_config


# ---------- 3. 统一判断一项安全配置 ----------
def check_setting(
    target,
    raw_value,
    valid_values,
    warning_values,
    warning_enabled,
    descriptions,
):
    """先检查采集值，再根据告警开关判断状态。"""

    # 例如 firewalld 只接受 running 和 not_running。
    # 不能把缺失值、unknown 或其他文本默认当作正常。
    if not isinstance(raw_value, str) or raw_value not in valid_values:
        return make_check(
            "security",
            target,
            "FAIL",
            raw_value,
            "采集值缺失或无法识别",
        )

    # 找到当前值对应的中文解释。
    # 例如 raw_value="running"，说明为“firewalld 正在运行”。
    description = descriptions[raw_value]

    if raw_value in warning_values:
        if warning_enabled:
            status = "WARN"
            reason = "{}；触发已开启的告警规则".format(description)
        else:
            # 关闭规则后仍保留真实配置值。
            # PASS 仅表示通过当前规则，不能写成“配置安全”。
            status = "PASS"
            reason = "{}；该项告警已关闭，不代表风险已消除".format(
                description
            )
    else:
        status = "PASS"
        reason = "{}；未触发本项告警条件".format(description)

    return make_check(
        "security", target, status, raw_value, reason
    )


# ---------- 4. 分析安全采集结果 ----------
def analyze_security(collected, security_config):
    """分别生成防火墙、root 登录和密码认证检查结果。"""

    # 如果整个脚本采集失败，三个检查都没有有效依据。
    try:
        data = get_data(collected)
    except ValueError as exc:
        return [
            make_check("security", target, "FAIL", None, str(exc))
            for target in (
                "firewalld",
                "ssh_permit_root_login",
                "ssh_password_authentication",
            )
        ]

    checks = []

    # ---------- 4.1 判断 firewalld 状态 ----------
    # 此项只能说明 firewalld 是否运行，
    # 不代表已经检查了所有防火墙规则是否合理。
    checks.append(
        check_setting(
            target="firewalld",
            raw_value=data.get("firewalld_state"),
            valid_values=("running", "not_running"),
            warning_values=("not_running",),
            warning_enabled=security_config[
                "warn_if_firewalld_not_running"
            ],
            descriptions={
                "running": "firewalld 正在运行，未审查具体规则",
                "not_running": "firewalld 未运行",
            },
        )
    )

    # ---------- 4.2 检查 SSH 数据属于哪个采集范围 ----------
    # 当前 security.sh 明确输出 global_disk_config。
    # 如果范围字段不对，就不继续猜测这两个 SSH 值的含义。
    scope = data.get("ssh_config_scope")

    if scope != "global_disk_config":
        for target in (
            "ssh_permit_root_login",
            "ssh_password_authentication",
        ):
            checks.append(
                make_check(
                    "security",
                    target,
                    "FAIL",
                    None,
                    "SSH 采集范围缺失或不受支持：{}".format(scope),
                )
            )

        # 防火墙结果仍保留，只将 SSH 两项标记为失败。
        return checks

    # ---------- 4.3 判断 root 登录策略 ----------
    # 只有 yes 会触发当前这条告警规则。
    # without-password 和 prohibit-password 不是完全禁止 root，
    # 它们限制密码和键盘交互认证，仍可能允许密钥登录。
    root_check = check_setting(
        target="ssh_permit_root_login",
        raw_value=data.get("ssh_permit_root_login"),
        valid_values=(
            "yes",
            "no",
            "without-password",
            "prohibit-password",
            "forced-commands-only",
        ),
        warning_values=("yes",),
        warning_enabled=security_config["warn_if_root_login_yes"],
        descriptions={
            "yes": "配置允许 root 登录，仍需满足认证等条件",
            "no": "配置禁止 root 通过 SSH 登录",
            "without-password": (
                "禁止 root 使用密码和键盘交互认证，仍可能允许密钥登录"
            ),
            "prohibit-password": (
                "禁止 root 使用密码和键盘交互认证，仍可能允许密钥登录"
            ),
            "forced-commands-only": (
                "root 公钥登录仅允许执行密钥中指定的强制命令"
            ),
        },
    )

    # ---------- 4.4 判断密码认证策略 ----------
    # PasswordAuthentication=no 不等于所有密码式交互都被禁用。
    # 此处只判断这一个配置项，不扩大结论。
    password_check = check_setting(
        target="ssh_password_authentication",
        raw_value=data.get("ssh_password_authentication"),
        valid_values=("yes", "no"),
        warning_values=("yes",),
        warning_enabled=security_config[
            "warn_if_password_authentication_yes"
        ],
        descriptions={
            "yes": "PasswordAuthentication 允许密码认证",
            "no": "PasswordAuthentication 禁止密码认证",
        },
    )

    # 在 SSH 判断说明中附上范围限制，防止报告说得过满。
    for check in (root_check, password_check):
        check["reason"] += (
            "；依据磁盘全局配置，未验证运行中配置及 Match 条件"
        )
        checks.append(check)

    return checks


# ---------- 5. 提供安全分析的独立验证入口 ----------
def main():
    """读取配置、采集安全数据、分析并输出结果。"""

    try:
        config = load_config()
        security_config = validate_security_config(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps(
            {"error": "配置读取或校验失败：{}".format(exc)},
            ensure_ascii=True,
            indent=2,
        ))
        return 2

    # 通过已有采集器执行 security.sh。
    # SSH 配置检查可能需要读取主机密钥，当前使用 root 验证。
    collected = collect(
        "security.sh",
        timeout_seconds=config["collector"]["timeout_seconds"],
    )

    checks = analyze_security(collected, security_config)
    overall_status = summarize(checks)

    output = {
        "scope": "firewalld_and_ssh_config",
        "checks": checks,
        "overall_status": overall_status,
    }

    print(json.dumps(
        output,
        ensure_ascii=True,
        indent=2,
    ))

    return {"PASS": 0, "WARN": 1, "FAIL": 2}[overall_status]


# ---------- 6. 直接执行时启动安全检查 ----------
# 被其他文件导入时，只加载函数，不自动执行采集。
if __name__ == "__main__":
    raise SystemExit(main())
