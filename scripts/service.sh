#!/bin/bash
# ============================================================
# 文件名称：service.sh
# 文件用途：
#   查询指定 systemd 服务的加载、活动、子状态和启用配置。
#   只读取状态，不启停服务，不判断 PASS / WARN / FAIL。
#
# 使用示例：
#   bash scripts/service.sh sshd firewalld nginx
#   bash scripts/service.sh sshd.service crond.service
#
# 参数约定：
#   至少传入一个服务名称。
#   未带 .service 后缀时自动补全。
#   支持常见名称和实例名称，例如 sshd、example@instance。
#   暂不支持包含反斜杠转义等特殊写法的服务名称。
#
# 主要变量：
#   service_arg       用户传入的服务名称
#   service_name      补全 .service 后缀后的名称
#   service_names     待检查的服务名称数组
#   service_raw       systemctl 返回的原始属性
#   query_rc          systemctl 查询命令的退出码
#   property / value  当前解析的属性名和属性值
#   load_state        服务加载状态
#   active_state      服务活动状态
#   sub_state         服务具体子状态
#   unit_file_state   服务启用配置状态
#   service_count     已完成采集的服务数量
#   output_lines      暂存全部输出行的数组
#
# 输出约定：
#   使用 service_1_、service_2_ 等前缀区分服务。
#   unknown 表示未获得对应属性值，不代表正常。
#   stdout 输出数据，stderr 输出错误说明。
#
# 退出码：
#   0：查询成功；服务停止或不存在也属于采集结果。
#   1：参数错误、查询失败、超时或必要属性缺失。
# ============================================================


# ---------- 1. 统一语言环境 ----------
export LC_ALL=C


# ---------- 2. 定义统一错误处理 ----------
# $1 是调用函数时传入的错误说明。
fail() {
    printf 'service.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 检查命令与参数 ----------
# command -v 检查命令是否存在。
# >/dev/null 2>&1 隐藏检查输出。
# || 表示前面的命令执行失败时，调用 fail。
command -v systemctl >/dev/null 2>&1 || fail "找不到 systemctl 命令"
command -v timeout >/dev/null 2>&1 || fail "找不到 timeout 命令"

# $# 表示命令行参数数量。
if (( $# == 0 )); then
    fail "请指定服务名称，例如：bash scripts/service.sh sshd crond"
fi


# ---------- 4. 校验并整理服务名称 ----------
service_names=()

for service_arg in "$@"; do
    # 限制为常见服务名字符，并禁止以连字符开头。
    if [[ ! "$service_arg" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]]; then
        fail "服务名称格式不支持：$service_arg"
    fi

    # 已有 .service 后缀则保留，否则补全。
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


# ---------- 5. 初始化输出缓存 ----------
# 先保存全部结果，全部查询成功后再输出。
service_count=0
output_lines=()


# ---------- 6. 逐个查询服务属性 ----------
for service_name in "${service_names[@]}"; do
    # 每个服务最多等待 10 秒，超时后尝试终止查询。
    # --all 让空值属性也显示出来，方便区分和处理。
    # 不使用较新版本才支持的 --value，兼容 CentOS 7。
    service_raw=$(timeout -k 2s 10s \
        systemctl --no-pager --all show \
        -p LoadState \
        -p ActiveState \
        -p SubState \
        -p UnitFileState \
        "$service_name")

    # 立即保存查询命令的退出码，避免被后续命令覆盖。
    query_rc=$?

    if (( query_rc == 124 || query_rc == 137 )); then
        fail "查询服务超时：$service_name"
    fi


    # ---------- 7. 解析属性名和属性值 ----------
    # 每轮重新初始化，防止沿用上一个服务的数据。
    load_state=""
    active_state=""
    sub_state=""
    unit_file_state=""

    # IFS="=" 表示按等号拆分。
    # 例如 ActiveState=active，拆成属性名和属性值。
    while IFS="=" read -r property value; do
        case "$property" in
            LoadState)
                load_state="$value"
                ;;
            ActiveState)
                active_state="$value"
                ;;
            SubState)
                sub_state="$value"
                ;;
            UnitFileState)
                unit_file_state="$value"
                ;;
        esac
    done <<< "$service_raw"  #把 service_raw 里的整段文本交给 while 循环读取。


    # ---------- 8. 区分服务不存在与查询失败 ----------
    # 部分版本查询不存在的服务时可能返回非零。
    # 只有明确得到 not-found，才记录为“服务未找到”。
    # 其他非零退出码按查询失败处理，不伪装成服务停止。
    if (( query_rc != 0 )) && [[ "$load_state" != "not-found" ]]; then
        fail "查询服务失败：$service_name，退出码：$query_rc"
    fi

    if [[ -z "$load_state" ]]; then
        fail "缺少 LoadState 属性：$service_name"
    fi

    if [[ "$load_state" == "not-found" ]]; then
        # 不存在的服务可能不返回其余属性。
        # 缺失值标为 unknown，不编造运行状态。
        active_state="${active_state:-unknown}"
        sub_state="${sub_state:-unknown}"
    elif [[ -z "$active_state" || -z "$sub_state" ]]; then
        fail "缺少必要运行状态属性：$service_name"
    fi

    # 部分单元没有可用的启用配置属性，显式标记未知。
    unit_file_state="${unit_file_state:-unknown}"


    # ---------- 9. 将当前服务结果加入数组 ----------
    service_count=$((service_count + 1))

    output_lines+=(
        "service_${service_count}_name=$service_name"
        "service_${service_count}_load_state=$load_state"
        "service_${service_count}_active_state=$active_state"
        "service_${service_count}_sub_state=$sub_state"
        "service_${service_count}_unit_file_state=$unit_file_state"
    )
done


# ---------- 10. 输出完整采集结果 ----------
printf '%s\n' "${output_lines[@]}"
printf 'service_count=%s\n' "$service_count"

exit 0