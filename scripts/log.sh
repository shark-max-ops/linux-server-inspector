#!/bin/bash
# ============================================================
# 文件名称：log.sh
# 文件用途：
#   采集最近 24 小时的系统错误日志、SSH 日志，
#   以及命令行参数指定的服务错误日志。
#   每类最多返回最新 50 条，供后续 Python 分析。
#
# 使用示例：
#   bash scripts/log.sh
#   bash scripts/log.sh crond
#
# 主要变量：
#   lookback_seconds     查询时间跨度，单位秒
#   max_entries          每类最多保留的记录数
#   end_epoch            查询结束时间，Unix 时间戳
#   start_epoch          查询开始时间，Unix 时间戳
#   command_name         当前检查的依赖命令
#   service_arg          用户传入的服务名称
#   service_name         补全后的服务名称
#   service_names        待查询的服务名称数组
#   system_records       系统错误日志
#   ssh_records          SSH 服务日志
#   service_records      当前指定服务的错误日志
#   output_blocks        暂存格式化结果的数组
#   service_count        已查询的指定服务数量
#
# emit_entries 函数局部变量：
#   prefix / content     输出前缀和日志内容
#   line / count         当前记录和记录数量
#   limit_reached        是否达到本次返回条数上限
#
# 输出约定：
#   每个 entry 字段保存一条原始 JSON 日志。
#   entry_count 是本次返回数量，不是全天日志总量。
#   limit_reached=1 表示达到上限，可能还有未返回的日志。
#   查询成功但无日志时，entry_count=0。
#
# 权限约定：
#   当前阶段使用 root 读取系统日志，避免权限受限时
#   把只能看到的部分日志误认为完整查询结果。
#
# 退出码：
#   0：查询成功，包括没有匹配日志的情况。
#   1：参数、权限、命令或查询异常。
# ============================================================


# ---------- 1. 设置语言、时间范围和记录上限 ----------
export LC_ALL=C

lookback_seconds=86400
max_entries=50


# ---------- 2. 定义统一错误处理 ----------
fail() {
    printf 'log.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 检查权限和依赖 ----------
# EUID 是 Bash 提供的有效用户 ID，root 的值为 0。
if (( EUID != 0 )); then
    fail "当前版本请使用 root 执行，以读取系统日志"
fi
#不显示错误
for command_name in journalctl timeout date; do
    command -v "$command_name" >/dev/null 2>&1 || fail "找不到命令：$command_name"
done


# ---------- 4. 整理可选服务名称 ----------
service_names=()

for service_arg in "$@"; do
    if [[ ! "$service_arg" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]]; then
        fail "服务名称格式不支持：$service_arg"
    fi

    case "$service_arg" in
        *.service)
            service_name="$service_arg"
            ;;
        *)
            service_name="${service_arg}.service"
            ;;
    esac

    service_names+=("$service_name")
done


# ---------- 5. 固定本次查询的开始和结束时间 ----------
# 所有查询共用同一时间范围，避免查询期间范围不断变化。
end_epoch=$(date +%s) || fail "无法获取当前时间"  #当前时间为结束时间来往前截取
start_epoch=$((end_epoch - lookback_seconds))


# ---------- 6. 定义统一日志查询函数 ----------
# "$@" 接收额外筛选条件，例如 -u sshd.service。
# --since / --until：设置查询时间范围。
# -n：最多返回最新多少条。
# -o json：每条日志输出为一行 JSON。
# --no-pager：直接输出，不进入交互式翻页。
# --quiet：隐藏提示性表头，真正的错误仍会输出。
query_journal() {
    #journalctl：查询 systemd 日志。
    timeout -k 2s 15s journalctl \
        --no-pager \
        --quiet \
        --since "@$start_epoch" \
        --until "@$end_epoch" \
        -n "$max_entries" \
        -o json \
        "$@"
}


# ---------- 7. 定义日志编号输出函数 ----------
emit_entries() {
    local prefix="$1"
    local content="$2"
    local line
    local count=0
    local limit_reached=0

    if [[ -n "$content" ]]; then
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue

            count=$((count + 1))
            printf '%s_entry_%d=%s\n' "$prefix" "$count" "$line"
        done <<< "$content"
    fi

    # 达到上限时，只能说明可能还有更多记录。
    if (( count >= max_entries )); then
        limit_reached=1
    fi

    printf '%s_entry_count=%d\n' "$prefix" "$count"
    printf '%s_limit_reached=%d\n' "$prefix" "$limit_reached"
}


# ---------- 8. 采集系统错误日志 ----------
# -p err 包含 err 及更严重的 crit、alert、emerg 级别。
system_records=$(query_journal -p err) || fail "系统错误日志查询失败或超时"


# ---------- 9. 采集 SSH 服务日志 ----------
# 保留所有严重级别，因为登录失败消息不一定属于 err。
# 登录失败的具体识别由后续 Python 完成。
ssh_records=$(query_journal -u sshd.service) || fail "SSH 日志查询失败或超时"


# ---------- 10. 缓存系统和 SSH 日志输出 ----------
output_blocks=()

output_blocks+=("$(emit_entries "system_error" "$system_records")")
output_blocks+=("$(emit_entries "ssh" "$ssh_records")")


# ---------- 11. 采集指定服务的错误日志 ----------
service_count=0

for service_name in "${service_names[@]}"; do
    service_records=$(query_journal -u "$service_name" -p err) || fail "服务日志查询失败：$service_name"

    service_count=$((service_count + 1))

    output_blocks+=("service_log_${service_count}_name=$service_name")
    output_blocks+=("$(emit_entries "service_log_${service_count}" "$service_records")")
done


# ---------- 12. 全部采集成功后统一输出 ----------
printf 'log_source=systemd_journal\n'
printf 'log_start_epoch=%s\n' "$start_epoch"
printf 'log_end_epoch=%s\n' "$end_epoch"
printf 'log_lookback_seconds=%s\n' "$lookback_seconds"
printf 'log_max_entries=%s\n' "$max_entries"
printf '%s\n' "${output_blocks[@]}"
printf 'service_log_count=%s\n' "$service_count"

exit 0