#!/bin/bash
# ============================================================
# 文件名称：network.sh
# 文件用途：
#   采集网络接口、IPv4/IPv6 地址、默认路由和 TCP 监听记录。
#   保留每行原始信息，供后续 Python 解析与判断。
#   不修改网络配置，不主动连接外部服务器。
#
# 主要变量：
#   command_name       当前检查的依赖命令名称
#   interface_raw      网络接口原始数据
#   address_raw        IP 地址原始数据
#   route4_raw         IPv4 默认路由原始数据
#   route6_raw         IPv6 默认路由原始数据
#   tcp_raw            TCP 监听原始数据，包含表头
#   tcp_records        去掉表头后的 TCP 监听记录
#
# emit_records 函数的局部变量：
#   prefix             输出字段的前缀
#   content            待输出的多行文本
#   line               当前处理的一行文本
#   count              当前这类数据的记录数量
#
# 输出格式：
#   interface_1=...
#   interface_count=...
#   address_1=...
#   address_count=...
#   default_ipv4_route_1=...
#   default_ipv4_route_count=...
#   default_ipv6_route_1=...
#   default_ipv6_route_count=...
#   tcp_listener_1=...
#   tcp_listener_count=...
#
# 数据约定：
#   每个等号右侧保留原始记录。
#   Python 解析时只按第一个等号拆分键和值。
#   查询成功但没有记录时，输出对应 count=0。
#   查询失败时返回非零，不把失败伪装成零条记录。
#
# 退出码：
#   0：全部采集成功
#   1：命令缺失、查询失败、超时或输出格式异常
# ============================================================


# ---------- 1. 统一语言环境 ----------
# 固定命令输出语言，便于后续识别表头和解析数据。
export LC_ALL=C


# ---------- 2. 定义统一错误处理函数 ----------
# $1 是调用函数时传入的错误说明。
fail() {
    printf 'network.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 检查所需命令 ----------
# ip：查询网络接口、地址和路由。
# ss：查询 TCP 监听情况。
# timeout：限制单条查询的执行时间。
# awk：去掉 ss 表头并校验记录格式。
for command_name in ip ss timeout awk; do
    command -v "$command_name" >/dev/null 2>&1 || fail "找不到命令：$command_name"
done


# ---------- 4. 定义带编号的输出函数 ----------
# $1 为字段前缀，$2 为原始多行文本。
# local 声明局部变量，仅在本次函数调用中使用。
emit_records() {
    local prefix="$1"
    local content="$2"
    local line
    local count=0

    # 空内容表示查询成功但没有记录。
    # 不进入循环，直接输出 count=0。
    if [[ -n "$content" ]]; then   #content 里面确实有内容。
        while IFS= read -r line; do      #从 content 中一次读取一整行，放到变量 line。
            # 忽略空行
            [[ -z "$line" ]] && continue

            count=$((count + 1))
            printf '%s_%d=%s\n' "$prefix" "$count" "$line"
        done <<< "$content"
    fi

    printf '%s_count=%d\n' "$prefix" "$count"
}


# ---------- 5. 采集网络接口 ----------
# -o 表示每条记录使用一行显示。
# 每条查询最多等待 10 秒，必要时再等待 2 秒强制终止。
interface_raw=$(timeout -k 2s 10s ip -o link show) || fail "网络接口查询失败或超时"

# 正常情况下至少应存在 lo 回环接口。
if [[ -z "$interface_raw" ]]; then   
    fail "网络接口查询结果为空"         
fi


# ---------- 6. 采集 IPv4 和 IPv6 地址 ----------
# 不限定 -4 或 -6，保留两种地址。
# 也保留 lo 回环地址，后续由 Python 区分用途。
address_raw=$(timeout -k 2s 10s ip -o addr show) || fail "IP 地址查询失败或超时"


# ---------- 7. 采集默认路由 ----------
# 没有默认路由不等于命令失败。
# 例如未配置 IPv6 默认路由时，route6_raw 可以为空。
route4_raw=$(timeout -k 2s 10s ip -4 route show default) || fail "IPv4 默认路由查询失败或超时"
route6_raw=$(timeout -k 2s 10s ip -6 route show default) || fail "IPv6 默认路由查询失败或超时"


# ---------- 8. 采集 TCP 监听记录 ----------
# -l：只显示监听中的套接字。
# -n：地址和端口使用数字，不转换为名称。
# -t：只查询 TCP。
# 暂不使用 -p，避免依赖读取进程归属信息的额外权限。
tcp_raw=$(timeout -k 2s 10s ss -lnt) || fail "TCP 监听查询失败或超时"


# ---------- 9. 去掉 TCP 表头并校验记录 ----------
# 第一行应以 State 开头。
# 后续每条记录应是 LISTEN，且至少有五个字段：
# 状态、接收队列、发送队列、本地地址、对端地址。
#
# awk 内部变量：
#   invalid：是否发现格式异常。
tcp_records=$(awk '
    NR == 1 {
        if ($1 != "State") {
            invalid = 1
            exit 1
        }
        next
    }

    NF == 0 { next }

    {
        if ($1 != "LISTEN" || NF < 5) {
            invalid = 1
            exit 1
        }

        print $0
    }

    END {
        if (invalid || NR == 0) {
            exit 1
        }
    }
' <<< "$tcp_raw") || fail "TCP 监听输出格式异常"


# ---------- 10. 全部采集成功后统一输出 ----------
# 各类记录分别编号，并输出各自数量。
emit_records "interface" "$interface_raw"
emit_records "address" "$address_raw"
emit_records "default_ipv4_route" "$route4_raw"
emit_records "default_ipv6_route" "$route6_raw"
emit_records "tcp_listener" "$tcp_records"

exit 0