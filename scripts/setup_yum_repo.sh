#!/bin/bash
# ============================================================
# 文件名称：setup_yum_repo.sh
# 文件用途：
#   创建项目使用的 CentOS 7.9 归档源配置。
#   若目标配置已存在，先备份再覆盖。
#
# 主要变量：
#   repo_file    仓库配置文件路径
#   backup_file  原配置备份路径
#
# 配置中的 $basearch：
#   由 yum 替换为系统架构，不由本脚本展开。
# ============================================================


# ---------- 1. 检查 root 权限 ----------
if (( EUID != 0 )); then
    echo "请使用 root 运行此脚本。" >&2
    exit 1
fi


# ---------- 2. 设置目标路径并备份已有配置 ----------
repo_file=/etc/yum.repos.d/inspector-vault.repo

if [[ -e "$repo_file" ]]; then
    backup_file=$(mktemp "${repo_file}.bak.XXXXXX") || exit 1
    cp -p "$repo_file" "$backup_file" || exit 1
    printf '原配置已备份至：%s\n' "$backup_file"
fi


# ---------- 3. 写入归档源配置 ----------
# EOF 使用单引号，确保 $basearch 原样写入。
# 结束标记 EOF 必须独占一行，前后不要加空格或注释。
cat > "$repo_file" <<'EOF'
# 文件用途：为巡检项目提供 CentOS 7.9 归档软件源。
# $basearch 由 yum 自动替换为系统架构。
# 默认禁用，使用时通过 --enablerepo 明确启用。

[inspector-base]
name=Inspector CentOS 7.9 Base
baseurl=https://vault.centos.org/7.9.2009/os/$basearch/
enabled=0
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7

[inspector-updates]
name=Inspector CentOS 7.9 Updates
baseurl=https://vault.centos.org/7.9.2009/updates/$basearch/
enabled=0
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7

[inspector-extras]
name=Inspector CentOS 7.9 Extras
baseurl=https://vault.centos.org/7.9.2009/extras/$basearch/
enabled=0
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
EOF


# ---------- 4. 检查写入是否成功 ----------
if (( $? != 0 )); then
    echo "仓库配置写入失败。" >&2
    exit 1
fi

printf '仓库配置已创建：%s\n' "$repo_file"
exit 0