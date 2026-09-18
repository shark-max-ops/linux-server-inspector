#!/bin/bash
# ============================================================
# 文件名称：disk.sh
# 文件用途：
#   采集本地磁盘文件系统的容量、使用率与挂载点。
#   排除 tmpfs、devtmpfs，以及 iso9660、udf 光盘文件系统。
#   不检查网络文件系统。
#   本脚本只采集数据，不判断 PASS / WARN / FAIL。
#
# 主要 Shell 变量：
#   disk_raw       df 输出的原始磁盘数据
#   disk_result    awk 解析后的 key=value 数据
#
# awk 内部变量：
#   count          已解析的文件系统数量
#   invalid        是否遇到异常数据
#   device         文件系统设备名称
#   total_kib      总容量，单位 KiB
#   used_kib       已使用容量，单位 KiB
#   available_kib  可用容量，单位 KiB，空间耗尽时可能为负
#   usage          磁盘使用率，去掉百分号后的整数
#   mount_point    挂载点
#   i              去除前五个字段时的循环变量
#
# 输出格式：
#   使用 disk_1_、disk_2_ 等前缀区分不同文件系统。
#   disk_count 表示本次采集的文件系统数量。
#   stdout 输出数据，stderr 输出错误说明。
#
# 退出码：
#   0：采集成功
#   1：采集失败
# ============================================================


# ---------- 1. 统一输出语言 ----------
# 确保 df 输出和数值格式不受中文环境影响。
export LC_ALL=C


# ---------- 2. 定义统一错误处理函数 ----------
# $1 是调用函数时传入的错误说明。
fail() {
    printf 'disk.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 获取本地磁盘数据 ----------
# -P：使用便于解析的格式，每个文件系统占一行。
# -k：容量统一使用 1024 字节为单位，即 KiB。
# -l：只显示本地文件系统。
# -x：排除指定的文件系统类型。
#
# tmpfs、devtmpfs 不属于本次检查的持久化磁盘空间。
# iso9660、udf 属于光盘文件系统，排除后避免空间告警误报。
disk_raw=$(df -P -k -l \
    -x tmpfs \
    -x devtmpfs \
    -x iso9660 \
    -x udf) \
    || fail "df 执行失败，无法完整获取本地磁盘数据"


# ---------- 4. 解析 df 输出 ----------
# 第一行是表头，跳过；其余每行对应一个文件系统。
# 先把解析结果保存到变量，全部成功后才对外输出。
disk_result=$(awk '
    # 跳过表头与空行。
    NR == 1 { next }
    NF == 0 { next }

    {
        # 每行至少包含设备、总量、已用、可用、使用率、挂载点。
        if (NF < 6) {
            invalid = 1
            exit 1
        }

        device = $1
        total_kib = $2
        used_kib = $3
        available_kib = $4
        usage = $5

        # 校验容量字段。
        # 可用空间允许为负，以保留空间耗尽时的真实信息。
        if (total_kib !~ /^[0-9]+$/ ||
            used_kib !~ /^[0-9]+$/ ||
            available_kib !~ /^-?[0-9]+$/) {
            invalid = 1
            exit 1
        }

        # 使用率应为整数加百分号，例如 25%。
        if (usage !~ /^[0-9]+%$/) {
            invalid = 1
            exit 1
        }

        # 去掉末尾百分号，便于后续 Python 转换为数字。
        sub(/%$/, "", usage)

        # 从原始行中去除前五个字段，剩余部分就是挂载点。
        # 不直接使用 $6，避免截断包含空格的挂载点。
        mount_point = $0
        sub(/^[[:space:]]+/, "", mount_point)

        for (i = 1; i <= 5; i++) {
            sub(/^[^[:space:]]+[[:space:]]+/, "", mount_point)
        }

        # Linux 挂载点应为绝对路径。
        if (mount_point !~ /^\//) {
            invalid = 1
            exit 1
        }

        # 给每个文件系统分配编号，防止同名字段互相覆盖。
        count++

        printf "disk_%d_device=%s\n", count, device
        printf "disk_%d_mount=%s\n", count, mount_point
        printf "disk_%d_total_kib=%s\n", count, total_kib
        printf "disk_%d_used_kib=%s\n", count, used_kib
        printf "disk_%d_available_kib=%s\n", count, available_kib
        printf "disk_%d_usage_percent=%s\n", count, usage
    }

    END {
        # 即使前面 exit，awk 仍会执行 END，因此要检查 invalid。
        if (invalid || count == 0) {
            exit 1
        }

        printf "disk_count=%d\n", count
    }
' <<< "$disk_raw") \
    || fail "磁盘数据格式异常，或没有可检查的本地文件系统"


# ---------- 5. 输出完整采集结果 ----------
# 只有 df 和解析都成功，才输出数据。
printf '%s\n' "$disk_result"

exit 0