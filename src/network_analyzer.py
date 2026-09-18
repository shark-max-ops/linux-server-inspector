# -*- coding: utf-8 -*-
# ============================================================
# 文件名称：network_analyzer.py
#
# 文件用途：
#   调用 network.sh，解析 TCP 监听记录。
#   检查必需端口是否存在、是否出现允许列表之外的端口。
#   不连接端口，不修改防火墙，不停止服务。
"""
整体执行流程：
开始
  ↓
读取 config.yaml，检查端口列表和回环地址检查开关
  ├─ 配置错误 → 输出错误，退出码 2
  ↓
调用 network.sh，获取网络采集结果
  ↓
检查采集状态，解析 TCP 监听记录和本地地址、端口
  ├─ 采集失败或数据异常 → 生成 FAIL 检查结果
  ↓
按端口整理监听记录，检查必需端口
  ├─ 存在 → PASS
  └─ 缺失 → FAIL
  ↓
根据配置决定未知端口检查是否包含回环地址
  ↓
检查是否存在允许列表之外的端口
  ├─ 存在 → WARN
  └─ 不存在 → PASS
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
#   network_config     配置中的 network 部分
#   required_ports     必须监听的端口列表
#   allowed_ports      允许监听的端口集合
#   check_loopback     未知端口检查是否包含回环地址
#   collected          network.sh 的完整采集结果
#   data               采集结果中的字段字典
#   listeners          解析后的 TCP 监听记录列表
#   line / fields      一条原始记录、按空白拆分后的字段
#   endpoint           本地监听地址，例如 [::]:22
#   address / port     拆分后的绑定地址和端口号
#   ip                 ipaddress 解析出的地址对象
#   by_port            全部监听记录按端口号分组后的字典
#   inspected_ports    参与未知端口检查的记录分组
#   checks             本次网络检查结果列表
#
# 检查规则：
#   必需端口不存在：FAIL。
#   出现允许列表之外的端口：WARN。
#   查询失败或记录无法解析：FAIL，不误判为没有监听。
#
# 退出码：
#   0：全部 PASS
#   1：存在 WARN，没有 FAIL
#   2：存在 FAIL，或配置错误
#
# 运行环境：
#   Linux 项目虚拟环境，Python 3.6。
# ============================================================


# ---------- 1. 导入工具和已有函数 ----------
import ipaddress
import json
import re

import yaml

from collector import collect

# 复用已有函数，保证不同分析模块的结果格式一致。
# 导入 analyzer 不会执行它的 main()。
from analyzer import (
    get_data,
    load_config,
    make_check,
    read_count,
    summarize,
)


# ---------- 2. 检查网络配置是否有效 ----------
def validate_network_config(config):
    """检查端口列表和回环地址检查开关。"""
    network_config = config.get("network")

    if not isinstance(network_config, dict):
        raise ValueError("network 必须是字典")

    for field in ("required_ports", "allowed_ports"):
        ports = network_config.get(field)

        if not isinstance(ports, list):
            raise ValueError("{} 必须是列表".format(field))

        for port in ports:
            # YAML 中直接写 22，不要写成字符串 "22"。
            # type 检查也会排除 True、False。
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError(
                    "{} 中的端口必须是 1～65535 的整数".format(field)
                )

        # 重复端口通常是配置疏漏，直接提示修改。
        if len(ports) != len(set(ports)):
            raise ValueError("{} 中存在重复端口".format(field))

    # 必需端口也应该被允许。
    # 否则同一个端口会同时成为“必须存在”和“不被允许”。
    required_ports = set(network_config["required_ports"])
    allowed_ports = set(network_config["allowed_ports"])

    if not required_ports.issubset(allowed_ports):
        raise ValueError("required_ports 必须全部包含在 allowed_ports 中")

    if type(network_config.get("check_loopback_listeners")) is not bool:
        raise ValueError("check_loopback_listeners 必须为 true 或 false")

    return network_config


# ---------- 3. 将原始 TCP 行拆成地址和端口 ----------
def parse_listeners(data):
    """把 network.sh 返回的 TCP 记录转换为结构化数据。"""
    count = read_count(data, "tcp_listener_count")

    # 找出 tcp_listener_1、tcp_listener_2 等记录字段。
    # 不把 tcp_listener_count 本身算作监听记录。
    record_keys = [
        key for key in data
        if re.fullmatch(r"tcp_listener_[0-9]+", key)
    ]

    if len(record_keys) != count:
        raise ValueError("TCP 监听数量与实际记录数量不一致")

    listeners = []

    for index in range(1, count + 1):
        key = "tcp_listener_{}".format(index)
        line = data.get(key)

        if not isinstance(line, str):
            raise ValueError("{} 缺失或格式异常".format(key))

        # 当前 network.sh 使用 ss -lnt，数据列依次为：
        # LISTEN、接收队列、发送队列、本地地址、对端地址。
        fields = line.split()

        if len(fields) < 5 or fields[0] != "LISTEN":
            raise ValueError("{} 不是有效的 TCP 监听记录".format(key))

        endpoint = fields[3]

        # 从最后一个冒号拆分，因为 IPv6 地址内部也有冒号。
        # [::]:22 -> ["[::]", "22"]
        # 127.0.0.1:631 -> ["127.0.0.1", "631"]
        parts = endpoint.rsplit(":", 1)

        if len(parts) != 2:
            raise ValueError("无法拆分监听地址：{}".format(endpoint))

        address, port_text = parts

        if re.fullmatch(r"[0-9]+", port_text) is None:
            raise ValueError("监听端口不是数字：{}".format(endpoint))

        port = int(port_text)

        if not 1 <= port <= 65535:
            raise ValueError("监听端口超出范围：{}".format(endpoint))

        # 去掉 IPv6 地址外面的方括号，保留里面的地址。
        if address.startswith("[") and address.endswith("]"):
            address = address[1:-1]

        # * 表示通配绑定，不属于回环地址。
        if address == "*":
            is_loopback = False
        else:
            # IPv6 可能带 %ens33 这样的接口标识。
            # 只在判断地址类型时去掉，输出仍保留原地址。
            address_without_zone = address.split("%", 1)[0]

            try:
                ip = ipaddress.ip_address(address_without_zone)
            except ValueError:
                raise ValueError(
                    "无法识别监听 IP 地址：{}".format(address)
                )

            # 同时识别 ::ffff:127.0.0.1 这种映射形式。
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                is_loopback = ip.ipv4_mapped.is_loopback
            else:
                is_loopback = ip.is_loopback

        listeners.append({
            "port": port,
            "address": address,
            "endpoint": endpoint,
            "is_loopback": is_loopback,
        })

    return listeners


# ---------- 4. 按端口号合并监听记录 ----------
def group_by_port(listeners):
    """同一端口的多个绑定地址放在一起，重复地址只保留一次。"""
    grouped = {}

    for listener in listeners:
        port = listener["port"]

        if port not in grouped:
            grouped[port] = set()

        grouped[port].add(listener["endpoint"])

    return grouped


# ---------- 5. 分析必需端口和未知端口 ----------
def analyze_network(collected, network_config):
    """返回网络检查结果；无法采集或解析时明确报告失败。"""
    try:
        data = get_data(collected)
        listeners = parse_listeners(data)
    except ValueError as exc:
        return [
            make_check(
                "network", "TCP 监听采集", "FAIL", None, str(exc)
            )
        ]

    checks = []
    by_port = group_by_port(listeners)

    required_ports = network_config["required_ports"]
    allowed_ports = set(network_config["allowed_ports"])
    check_loopback = network_config["check_loopback_listeners"]

    # 先检查必须存在的端口。
    # 无论配置是否检查回环上的未知端口，这里都查看全部监听。
    for port in required_ports:
        endpoints = sorted(by_port.get(port, set()))

        if endpoints:
            status = "PASS"
            reason = "必需端口存在监听；未验证外部可达性"
        else:
            status = "FAIL"
            reason = "未发现必需端口的 TCP 监听"

        checks.append(
            make_check(
                "network",
                "required_tcp_{}".format(port),
                status,
                {"port": port, "endpoints": endpoints},
                reason,
            )
        )

    # 根据配置决定未知端口检查是否包含回环地址。
    inspected_listeners = []

    for listener in listeners:
        if check_loopback or not listener["is_loopback"]:
            inspected_listeners.append(listener)

    inspected_ports = group_by_port(inspected_listeners)

    # 用集合差集找出“不在允许列表中的端口”。
    # 例如 {22, 111} - {22, 80, 443} 得到 {111}。
    unknown_ports = sorted(set(inspected_ports) - allowed_ports)

    scope_text = (
        "包含回环监听" if check_loopback else "不包含回环监听"
    )

    if unknown_ports:
        for port in unknown_ports:
            checks.append(
                make_check(
                    "network",
                    "unexpected_tcp_{}".format(port),
                    "WARN",
                    {
                        "port": port,
                        "endpoints": sorted(inspected_ports[port]),
                    },
                    "端口不在允许列表中，请核对用途；{}".format(
                        scope_text
                    ),
                )
            )
    else:
        checks.append(
            make_check(
                "network",
                "unexpected_tcp_ports",
                "PASS",
                [],
                "未发现允许列表之外的端口；{}".format(scope_text),
            )
        )

    return checks


# ---------- 6. 提供网络分析的独立验证入口 ----------
def main():
    """读取现有配置，运行网络采集并显示分析结果。"""
    try:
        config = load_config()
        network_config = validate_network_config(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps(
            {"error": "配置读取或校验失败：{}".format(exc)},
            ensure_ascii=True,
            indent=2,
        ))
        return 2

    collected = collect(
        "network.sh",
        timeout_seconds=config["collector"]["timeout_seconds"],
    )

    checks = analyze_network(collected, network_config)
    overall_status = summarize(checks)

    # 当前入口只验证 TCP 端口规则。
    # 网卡、IP 和默认路由原始数据仍由 network.sh 采集，
    # 后续用于报告展示，这里不擅自加入新告警规则。
    output = {
        "scope": "tcp_ports_only",
        "checks": checks,
        "overall_status": overall_status,
    }

    print(json.dumps(
        output,
        ensure_ascii=True,
        indent=2,
    ))

    return {"PASS": 0, "WARN": 1, "FAIL": 2}[overall_status]


# ---------- 7. 直接执行时运行验证入口 ----------
if __name__ == "__main__":
    raise SystemExit(main())
