#!/bin/bash
# ============================================================
# 文件名称：cpu.sh
# 文件用途：
#   采集 CPU 总体使用率、在线逻辑 CPU 数量和系统平均负载。
#   只输出采集数据，不判断 PASS / WARN / FAIL。
#
# 采集方式：
#   两次读取 /proc/stat，中间等待 1 秒。
#   CPU 使用率 = (总时间增量 - 空闲时间增量) / 总时间增量 × 100。
#   本项目将 idle + iowait 计入空闲时间。
#
# 主要 Shell 变量：
#   sample_seconds             两次采样之间的等待秒数
#   first_sample               第一次采样的总时间和空闲时间
#   second_sample              第二次采样的总时间和空闲时间
#   total_before / idle_before  第一次采样的两个累计值
#   total_after / idle_after    第二次采样的两个累计值
#   total_delta / idle_delta    两次采样之间的增量
#   cpu_usage_percent          CPU 总体使用率，保留两位小数
#   cpu_cores                  在线逻辑 CPU 数量
#   load_1 / load_5 / load_15   最近 1、5、15 分钟平均负载
#   load_extra                 /proc/loadavg 剩余字段，本脚本不使用
#   load_value                 校验平均负载时使用的循环变量
#
# awk 内部变量：
#   i / total                  字段序号与累计总时间
#   total_diff / idle_diff     传给 awk 的时间增量
#
# 输出格式：
#   stdout：每行一个 key=value。
#   stderr：错误说明。
#
# 退出码：
#   0：采集成功
#   1：采集失败
# ============================================================


# ---------- 1. 设置语言环境和采样间隔 ----------
# 固定小数点格式，避免不同语言环境影响数值解析。
export LC_ALL=C

sample_seconds=1


# ---------- 2. 定义统一错误处理 ----------
# $1 是调用函数时传入的错误说明。
#编号	名称	默认指向
#0	标准输入 stdin	键盘
#1	标准输出 stdout	终端屏幕
#2	标准错误 stderr	终端屏幕
fail() {
    printf 'cpu.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 定义 CPU 累计时间读取函数 ----------
# /proc/stat 第一行以 cpu 开头，代表全部逻辑 CPU 的汇总。
#
# 前八个数值字段：
# user、nice、system、idle、iowait、irq、softirq、steal。
#
# guest 和 guest_nice 已包含在前面的部分字段中，不重复相加。
# awk 中 $1 是 cpu 标签，所以数值从 $2 开始。
read_cpu_times() {
    awk '
        NR == 1 {
            # 确认读取到的是 CPU 汇总行，且字段数量足够。
            if ($1 != "cpu" || NF < 9) {
                exit 1
            }

            # 校验并累加前八个 CPU 时间字段。
            total = 0
            for (i = 2; i <= 9; i++) {
                if ($i !~ /^[0-9]+$/) {        #整个字段必须全部由数字组成，不是的话就exit 1；
                    exit 1
                }
                total += $i
            }

            # $5 是 idle，$6 是 iowait。
            # 输出两个整数：总时间、空闲时间。
            printf "%.0f %.0f\n", total, $5 + $6
            exit
        }
    ' /proc/stat
}


# ---------- 4. 检查需要读取的系统文件 ----------
if [[ ! -r /proc/stat ]]; then
    fail "无法读取 /proc/stat"
fi

if [[ ! -r /proc/loadavg ]]; then
    fail "无法读取 /proc/loadavg"
fi


# ---------- 5. 第一次采样 ----------
first_sample=$(read_cpu_times) \
    || fail "第一次 CPU 采样失败"

# <<< 把变量中的字符串交给 read，拆分到两个变量。
read -r total_before idle_before <<< "$first_sample"

if [[ ! "$total_before" =~ ^[0-9]+$ ||
      ! "$idle_before" =~ ^[0-9]+$ ]]; then
    fail "第一次 CPU 采样数据格式异常"
fi


# ---------- 6. 等待后进行第二次采样 ----------
sleep "$sample_seconds" \
    || fail "采样等待失败"

second_sample=$(read_cpu_times) \
    || fail "第二次 CPU 采样失败"

read -r total_after idle_after <<< "$second_sample"

if [[ ! "$total_after" =~ ^[0-9]+$ ||
      ! "$idle_after" =~ ^[0-9]+$ ]]; then
    fail "第二次 CPU 采样数据格式异常"
fi


# ---------- 7. 计算时间增量并检查有效性 ----------
# $((表达式)) 用于 Bash 整数运算。
total_delta=$((total_after - total_before))
idle_delta=$((idle_after - idle_before))

# 总时间必须增长，空闲增量应在 0 到总时间增量之间。
# 遇到异常计数时报告失败，不编造一个正常使用率。
if (( total_delta <= 0 ||
      idle_delta < 0 ||
      idle_delta > total_delta )); then
    fail "CPU 时间增量异常，请重新采样"
fi


# ---------- 8. 计算 CPU 总体使用率 ----------
# Bash 不直接支持小数运算，所以使用 awk。
# -v 用于把 Shell 变量传入 awk。
cpu_usage_percent=$(awk \
    -v total_diff="$total_delta" \
    -v idle_diff="$idle_delta" \
    'BEGIN {
        #CPU 使用率 = 忙碌时间 ÷ 总时间 × 100%
        printf "%.2f", (total_diff - idle_diff) / total_diff * 100
    }') || fail "CPU 使用率计算失败"


# ---------- 9. 获取在线逻辑 CPU 数量 ----------
# 这里统计的是系统在线的逻辑 CPU，不是物理 CPU 插槽数量。
cpu_cores=$(getconf _NPROCESSORS_ONLN) \
    || fail "无法获取在线逻辑 CPU 数量"

if [[ ! "$cpu_cores" =~ ^[0-9]+$ ]]; then
    fail "在线逻辑 CPU 数量格式异常"
fi

if (( cpu_cores <= 0 )); then
    fail "在线逻辑 CPU 数量必须大于 0"
fi


# ---------- 10. 获取系统平均负载 ----------
# /proc/loadavg 的前三列依次是 1、5、15 分钟平均负载。
read -r load_1 load_5 load_15 load_extra < /proc/loadavg \
    || fail "无法读取系统平均负载"

# 检查三个负载值是否都是合法的非负数字。
for load_value in "$load_1" "$load_5" "$load_15"; do
    if [[ ! "$load_value" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        fail "系统平均负载格式异常"
    fi
done


# ---------- 11. 所有采集成功后统一输出 ----------
# 百分比只输出数值，不附加 %，方便后续 Python 转换。
printf 'cpu_usage_percent=%s\n' "$cpu_usage_percent"
printf 'cpu_cores=%s\n' "$cpu_cores"
printf 'load_1=%s\n' "$load_1"
printf 'load_5=%s\n' "$load_5"
printf 'load_15=%s\n' "$load_15"
printf 'sample_seconds=%s\n' "$sample_seconds"

exit 0