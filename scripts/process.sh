#!/bin/bash
# ============================================================
# 文件名称：process.sh
# 文件用途：
#   统计进程数量，找出 CPU 和内存占用最高的进程，
#   并检查命令行参数指定的关键进程是否存在。
#   本脚本只采集数据，不判断 PASS / WARN / FAIL。
#
# 使用示例：
#   bash scripts/process.sh
#   bash scripts/process.sh sshd crond
#
# 匹配约定：
#   按 ps 的 comm 字段进行名称精确匹配，不匹配完整启动命令。
#   当前接受 1～15 个 ASCII 字符的进程名：
#   字母、数字、下划线、点、连字符。
#   僵尸进程不计入关键进程的有效匹配数量。
#
# 主要 Shell 变量：
#   process_name    当前校验的关键进程名称
#   required_names  用空格分隔的关键进程名称列表
#   process_raw     ps 采集的进程快照
#   process_result  awk 生成的采集结果
#
# 主要 awk 变量：
#   required / names / required_count  关键进程列表、数组与数量
#   count / invalid                   进程总数与数据异常标记
#   pid / cpu / memory / state / name  当前行的进程信息
#   cpu_pid / cpu_name / cpu_max       CPU 占用最高的进程信息
#   mem_pid / mem_name / mem_max       内存占用最高的进程信息
#   matches                           各进程名的有效匹配数量
#   i                                 循环下标
#
# 输出约定：
#   CPU 和内存使用率单位为百分比，数值不附带百分号。
#   required_N_present：1 表示存在，0 表示不存在。
#   stdout 输出数据，stderr 输出错误说明。
#
# 退出码：
#   0：采集成功；关键进程不存在也属于成功采集到的结果。
#   1：采集失败或参数不合法。
# ============================================================


# ---------- 1. 统一语言环境 ----------
export LC_ALL=C


# ---------- 2. 定义统一错误处理 ----------
# $1 是调用函数时传入的错误说明。
fail() {
    printf 'process.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 校验关键进程名称 ----------
# "$@" 表示用户传入的全部参数，每个参数独立处理。
# 限制名称长度，避免旧版 Linux 进程名截断造成匹配歧义。
for process_name in "$@"; do
    if [[ ! "$process_name" =~ ^[a-zA-Z0-9_.-]{1,15}$ ]]; then
        fail "进程名必须为 1～15 个字母、数字、下划线、点或连字符"
    fi
done

# 把已经校验的名称拼成空格分隔的字符串，交给 awk。
required_names=""
for process_name in "$@"; do
    required_names="${required_names}${process_name} "
done


# ---------- 4. 获取一次进程快照 ----------
# -e：显示当前可见的所有进程。
# -ww：使用宽输出，减少名称被显示宽度截断的影响。
# -o：指定字段；字段后的 = 用于取消表头。
#
# 字段顺序：
# PID、CPU 百分比、内存百分比、状态、进程名称。
# comm 只包含进程名称，不采集可能含敏感参数的完整命令行。
process_raw=$(ps -e -ww \
    -o pid= -o pcpu= -o pmem= -o stat= -o comm=) \
    || fail "ps 执行失败，无法获取进程快照"


# ---------- 5. 解析快照并统计结果 ----------
process_result=$(awk -v required="$required_names" '
    BEGIN {
        # 按空白拆分关键进程名称。
        required_count = split(required, names, " ")

        # 从 -1 开始，确保即使使用率都是 0，也能选出一条记录。
        cpu_max = -1
        mem_max = -1
    }

    # 跳过空行。
    NF == 0 { next }

    {
        # 至少需要五个字段。
        if (NF < 5 ||
            $1 !~ /^[0-9]+$/ ||
            $2 !~ /^[0-9]+([.][0-9]+)?$/ ||
            $3 !~ /^[0-9]+([.][0-9]+)?$/) {
            invalid = 1
            exit 1
        }

        pid = $1
        cpu = $2 + 0    #+0强制 awk 把它当数字。
        memory = $3 + 0
        state = $4

        # 去掉前四个字段，保留完整进程名称。
        name = $0    #获取完整行
        sub(/^[[:space:]]+/, "", name)

        for (i = 1; i <= 4; i++) {
            sub(/^[^[:space:]]+[[:space:]]+/, "", name)
        }

        count++        #每成功解析一行进程：

        # 保存 CPU 占用最高的进程。
        # 数值相同时保留快照中先遇到的一条。
        if (cpu > cpu_max) {
            cpu_max = cpu
            cpu_pid = pid
            cpu_name = name
        }

        # 保存内存占用最高的进程。
        if (memory > mem_max) {
            mem_max = memory
            mem_pid = pid
            mem_name = name
        }

        # Z 表示僵尸状态，不视为关键进程正常存在。
        # 按完整名称作为数组键，统计有效匹配数量。
        if (state !~ /^Z/) {
            matches[name]++         #不只是记录有没有，还记录有几个
        }
    }

    END {
        # 解析异常或没有进程数据时，不输出正常结果。
        if (invalid || count == 0) {
            exit 1
        }

        # 输出总数与资源占用最高的进程。
        printf "process_count=%d\n", count

        printf "top_cpu_pid=%s\n", cpu_pid
        printf "top_cpu_name=%s\n", cpu_name
        printf "top_cpu_percent=%.2f\n", cpu_max

        printf "top_memory_pid=%s\n", mem_pid
        printf "top_memory_name=%s\n", mem_name
        printf "top_memory_percent=%.2f\n", mem_max

        # 输出每个指定进程的匹配数量和是否存在。
        for (i = 1; i <= required_count; i++) {
            printf "required_%d_name=%s\n", i, names[i]
            printf "required_%d_count=%d\n", i, matches[names[i]]
            printf "required_%d_present=%d\n",
                i, (matches[names[i]] > 0 ? 1 : 0)
        }

        printf "required_count=%d\n", required_count
    }
' <<< "$process_raw") \
    || fail "进程数据格式异常，或没有可读取的进程记录"


# ---------- 6. 输出完整结果 ----------
printf '%s\n' "$process_result"

exit 0