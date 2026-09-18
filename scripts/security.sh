#!/bin/bash
# ============================================================
# 文件名称：security.sh
# 文件用途：
#   采集 firewalld 运行状态和 SSH 全局安全配置。
#   不修改系统配置，不重启服务，不判断 PASS / WARN / FAIL。
#
# 主要变量：
#   command_name             当前检查的依赖命令
#   firewall_raw             firewall-cmd 返回的状态或错误信息
#   firewall_rc              firewall-cmd 的退出码
#   firewall_state           已确认的 firewalld 状态
#   sshd_path                sshd 程序路径
#   ssh_raw                  sshd -T 输出的配置解析结果
#   option_name / option_value  SSH 配置项名称和值
#   permit_root_login        root 登录策略
#   password_authentication  是否允许密码认证
#
# SSH 采集范围：
#   读取默认配置文件，不传入特定连接条件。
#   结果反映磁盘配置的全局解析值。
#   不代表正在运行的服务已加载这些值。
#   不覆盖 Match 条件下的特定用户或地址配置。
#
# 输出格式：
#   stdout：每行一个 key=value。
#   stderr：错误说明。
#
# 退出码：
#   0：采集成功，防火墙未运行也属于有效采集结果。
#   1：命令缺失、权限不足、超时或配置解析失败。
# ============================================================


# ---------- 1. 统一语言环境 ----------
export LC_ALL=C


# ---------- 2. 定义统一错误处理 ----------
# $1 是调用函数时传入的错误说明。
fail() {
    printf 'security.sh: %s\n' "$1" >&2
    exit 1
}


# ---------- 3. 检查依赖命令 ----------
for command_name in timeout firewall-cmd; do
    command -v "$command_name" >/dev/null 2>&1 || fail "找不到命令：$command_name"
done

# 优先从 PATH 查找 sshd。
# 若 PATH 不包含 /usr/sbin，则检查 CentOS 常用安装位置。
sshd_path=$(command -v sshd)

if [[ -z "$sshd_path" && -x /usr/sbin/sshd ]]; then
    sshd_path=/usr/sbin/sshd
fi

if [[ -z "$sshd_path" ]]; then
    fail "找不到 sshd 程序"
fi


# ---------- 4. 查询 firewalld 状态 ----------
# 2>&1 将错误输出一并保存，以识别明确的 not running。
# 只查询运行状态，不修改防火墙。
firewall_raw=$(timeout -k 2s 10s firewall-cmd --state 2>&1)
firewall_rc=$?

if (( firewall_rc == 0 )) && [[ "$firewall_raw" == "running" ]]; then
    firewall_state=running
elif (( firewall_rc != 0 )) && [[ "$firewall_raw" == "not running" ]]; then
    firewall_state=not_running
else
    # 权限错误、超时等不能当成“防火墙未运行”。
    printf '%s\n' "$firewall_raw" >&2
    fail "无法确认 firewalld 状态，退出码：$firewall_rc"
fi


# ---------- 5. 解析 SSH 配置 ----------
# -T：检查配置并打印解析后的配置项，然后退出。
# 此命令不会启动一个 SSH 服务。
# 读取主机密钥等内容可能需要 root 权限。
ssh_raw=$(timeout -k 2s 10s "$sshd_path" -T) || fail "SSH 配置解析失败或超时，请检查上方错误"


# ---------- 6. 提取两个安全配置项 ----------
permit_root_login=""
password_authentication=""

# read 将每行拆分为配置项名称和配置值。
# 例如：passwordauthentication yes。
while read -r option_name option_value; do
    case "$option_name" in
        permitrootlogin)
            permit_root_login="$option_value"
            ;;
        passwordauthentication)
            password_authentication="$option_value"
            ;;
    esac
done <<< "$ssh_raw"


# ---------- 7. 校验采集值 ----------
# 不同 OpenSSH 版本可能使用不同的同义写法。
case "$permit_root_login" in
    yes|no|without-password|prohibit-password|forced-commands-only)
        ;;
    *)
        fail "PermitRootLogin 缺失或值无法识别：$permit_root_login"
        ;;
esac

case "$password_authentication" in
    yes|no)
        ;;
    *)
        fail "PasswordAuthentication 缺失或值无法识别：$password_authentication"
        ;;
esac


# ---------- 8. 全部成功后统一输出 ----------
printf 'firewalld_state=%s\n' "$firewall_state"
printf 'ssh_config_scope=global_disk_config\n'
printf 'ssh_permit_root_login=%s\n' "$permit_root_login"
printf 'ssh_password_authentication=%s\n' "$password_authentication"

exit 0