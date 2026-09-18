#!/bin/bash
# ============================================================
# 文件名称：system_info.sh
# 文件用途：
#   采集 Linux 系统基本信息，供后续 Python 程序读取。
#   本脚本只采集数据，不进行 PASS / WARN / FAIL 判断。
#
# 输出格式：
#   每行输出一个 key=value，等号左侧为固定字段名。
#   正常数据输出到 stdout，错误信息输出到 stderr。
#默认有 3 个标准文件描述符
#每个程序一启动，通常都会打开三个“通道”：
#编号	名称	默认指向
#0	标准输入 stdin	键盘
#1	标准输出 stdout	终端屏幕
#2	标准错误 stderr	终端屏幕
#
# 主要变量：
#   current_time    当前系统时间，包含时区
#   host_name       主机名
#   os_version      CentOS 版本
#   kernel_version  Linux 内核版本
#   uptime_raw      /proc/uptime 中的原始运行秒数
#   idle_seconds    /proc/uptime 中的累计空闲秒数，本脚本不使用
#   uptime_seconds  取整数后的系统运行秒数
#   current_user    执行脚本的用户
#
# 退出码：
#   0：采集成功
#   1：采集失败
# ============================================================


# ---------- 1. 统一命令输出语言 ----------
# 使用 C 语言环境，减少系统语言差异对后续解析的影响。
export LC_ALL=C


# ---------- 2. 定义统一的错误处理函数 ----------
# $1 表示调用函数时传入的第一条参数，即错误说明。
# >&2 表示将错误信息输出到 stderr。
fail() {
    printf 'system_info.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 采集当前时间与主机名 ----------
# $(命令) 表示执行命令，并将输出保存到变量。
# || 表示前面的命令失败时，执行后面的错误处理。
current_time=$(date '+%Y-%m-%d %H:%M:%S %z') \
    || fail "无法获取当前时间"

host_name=$(hostname) \
    || fail "无法获取主机名"


# ---------- 4. 采集 CentOS 版本 ----------
# -r 用于检查文件是否存在且可读取。
if [[ ! -r /etc/centos-release ]]; then
    fail "无法读取 /etc/centos-release"
fi

os_version=$(cat /etc/centos-release) \
    || fail "无法获取 CentOS 版本"


# ---------- 5. 采集 Linux 内核版本 ----------
kernel_version=$(uname -r) \
    || fail "无法获取 Linux 内核版本"


# ---------- 6. 采集系统运行时间 ----------
# /proc/uptime 第一列是系统启动至今的秒数，通常带小数。
# read 从该文件读取两个字段，分别保存到两个变量。
if [[ ! -r /proc/uptime ]]; then
    fail "无法读取 /proc/uptime"
fi

read -r uptime_raw idle_seconds < /proc/uptime \
    || fail "无法读取系统运行时间"

# 检查第一列是否为合法的非负数字，避免输出错误数据。
if [[ ! "$uptime_raw" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    fail "系统运行时间格式异常"
fi

# %%.* 删除小数点及后面的内容，保留整数秒。
uptime_seconds=${uptime_raw%%.*}


# ---------- 7. 采集当前执行用户 ----------
current_user=$(whoami) \
    || fail "无法获取当前用户"


# ---------- 8. 输出统一格式的数据 ----------
# 固定字段名便于后续 Python 解析。
# 所有采集成功后才输出，避免失败时留下不完整的数据。
printf 'time=%s\n' "$current_time"
printf 'hostname=%s\n' "$host_name"
printf 'os=%s\n' "$os_version"
printf 'kernel=%s\n' "$kernel_version"
printf 'uptime_seconds=%s\n' "$uptime_seconds"
printf 'user=%s\n' "$current_user"

exit 0