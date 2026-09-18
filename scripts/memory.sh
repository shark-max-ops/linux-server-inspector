#!/bin/bash
# ============================================================
# 文件名称：memory.sh
# 文件用途：
#   读取 /proc/meminfo，采集总内存、可用内存，
#   并计算已使用内存和内存使用率。
#   本脚本不判断 PASS / WARN / FAIL。
#
# 计算口径：
#   已使用内存 = MemTotal - MemAvailable。
#   内存使用率 = 已使用内存 / MemTotal × 100。
#
# 单位约定：
#   /proc/meminfo 中标为 kB 的内存值实际按 1024 字节计。
#   本脚本使用 kib 字段名表示 KiB，避免单位混淆。
#
# 主要 Shell 变量：
#   memory_sample         从系统文件中提取的两个内存值
#   memory_total_kib      总内存，单位 KiB
#   memory_available_kib 可用内存，单位 KiB
#   memory_used_kib       已使用内存，单位 KiB
#   memory_usage_percent 内存使用率，保留两位小数
#
# awk 内部变量：
#   total / available     提取的总内存和可用内存
#   total_found           是否已找到 MemTotal
#   available_found       是否已找到 MemAvailable
#   invalid               是否发现字段格式异常
#   used_kib / total_kib  用于计算使用率的输入变量
#
# 输出格式：
#   stdout：每行一个 key=value。
#   stderr：错误说明。
#
# 退出码：
#   0：采集成功
#   1：采集失败
# ============================================================


# ---------- 1. 统一命令输出语言 ----------
# 确保小数使用小数点，便于后续 Python 解析。
export LC_ALL=C


# ---------- 2. 定义统一错误处理函数 ----------
# $1 是调用函数时传入的错误说明。
fail() {
    printf 'memory.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 检查系统内存信息文件 ----------
if [[ ! -r /proc/meminfo ]]; then
    fail "无法读取 /proc/meminfo"
fi


# ---------- 4. 一次读取总内存和可用内存 ----------
# awk 默认按照空格或制表符拆分字段。
# 例如 MemTotal:  8000000 kB：
# $1 是 MemTotal:，$2 是数值，$3 是单位。
memory_sample=$(awk '
    # 找到总内存字段，检查数值与单位。
    $1 == "MemTotal:" {
        if ($2 !~ /^[0-9]+$/ || $3 != "kB") {
            invalid = 1
        }
        total = $2
        total_found = 1
    }

    # 找到可用内存字段，检查数值与单位。
    $1 == "MemAvailable:" {
        if ($2 !~ /^[0-9]+$/ || $3 != "kB") {
            invalid = 1
        }
        available = $2
        available_found = 1
    }

    # END 在文件读取完毕后执行。
    # 字段缺失时返回失败，不将缺失值当作 0。
    END {
        if (invalid || !total_found || !available_found) {
            exit 1
        }

        printf "%s %s\n", total, available
    }
' /proc/meminfo) \
    || fail "内存字段缺失或格式异常，需要有效的 MemTotal 和 MemAvailable"


# ---------- 5. 将采集结果拆分到 Shell 变量 ----------
# <<< 将 memory_sample 的内容作为 read 的输入。
read -r memory_total_kib memory_available_kib <<< "$memory_sample"

# 再检查一次变量，确保后面可以安全进行整数运算。
if [[ ! "$memory_total_kib" =~ ^[0-9]+$ ||
      ! "$memory_available_kib" =~ ^[0-9]+$ ]]; then
    fail "内存采集结果不是有效的整数"
fi


# ---------- 6. 检查内存数值范围 ----------
# 总内存必须大于 0，可用内存不能超过总内存。
# 前面的数字格式检查已经排除了负数。
if (( memory_total_kib <= 0 ||
      memory_available_kib > memory_total_kib )); then
    fail "内存数值范围异常"
fi


# ---------- 7. 计算已使用内存 ----------
# $((表达式)) 用于 Bash 整数运算。
memory_used_kib=$((memory_total_kib - memory_available_kib))


# ---------- 8. 计算内存使用率 ----------
# 使用 awk 完成小数运算，结果保留两位小数。
# -v 将 Shell 变量传入 awk。
memory_usage_percent=$(awk \
    -v used_kib="$memory_used_kib" \
    -v total_kib="$memory_total_kib" \
    'BEGIN {
        printf "%.2f", used_kib / total_kib * 100
    }') || fail "内存使用率计算失败"


# ---------- 9. 所有采集成功后统一输出 ----------
# 使用率只输出数字，不附加 %。
# 保留 KiB 原始精度，后续由报告模块转换为 MiB 或 GiB。
printf 'memory_total_kib=%s\n' "$memory_total_kib"
printf 'memory_available_kib=%s\n' "$memory_available_kib"
printf 'memory_used_kib=%s\n' "$memory_used_kib"
printf 'memory_usage_percent=%s\n' "$memory_usage_percent"

exit 0