# -*- coding: utf-8 -*-

# 文件用途：
# 查找超过保留期限的巡检报告和单次运行日志。
# 默认只显示待清理文件；加上 --apply 后才实际删除。
# 只有同时指定 --apply --notify，才在清理后发送钉钉资料清理汇总。
# 只检查配置目录的第一层，不进入子目录，不删除符号链接。

"""
整体执行流程：
开始
  ↓
读取命令行参数：保留天数、是否实际删除
  ├─ 保留天数小于 1 → 参数错误，停止
  ↓
读取 config.yaml 中的报告目录和日志目录
  ├─ 配置错误或目录不存在 → 输出错误，退出码 2
  ↓
根据当前时间计算过期分界时间
  ↓
遍历两个目录中的文件
  ├─ 文件名不符合项目命名规则 → 跳过
  ├─ 是目录或符号链接 → 跳过
  ├─ 修改时间未超过保留期限 → 保留
  └─ 修改时间超过保留期限 → 加入待清理列表
  ↓
显示待清理文件及预计释放空间
  ↓
判断是否指定 --apply
  ├─ 没有指定 → 只预览，不删除
  └─ 已指定 → 再次检查文件，逐个删除并统计实际释放空间
                  └─ 删除失败 → 记录错误，继续处理其他文件
  ↓
判断是否显式指定 --notify
  ├─ 没有指定 → 只输出本地清理结果
  └─ 已指定 → 读取最近巡检 JSON 快照，发送资料清理汇总
                 └─ 通知失败 → 保留清理结果，记录错误并返回退出码 2
  ↓
输出处理结果
  ↓
返回退出码：成功为 0，发生错误为 2
"""

# 主要变量：
# PROJECT_ROOT：项目根目录。
# CONFIG_FILE：配置文件路径。
# REPORT_PATTERN：正式巡检报告的文件名规则。
# RUN_PATTERN：单次运行日志的文件名规则。
# args：命令行参数。
# keep_days：保留天数，默认 7 天。
# cutoff：过期分界时间，使用 Unix 时间戳。
# candidates：准备清理的文件及其扫描时的状态。
# total_bytes：待清理文件的总大小。
# deleted_count：实际成功删除的文件数量。
# error_count：处理过程中发生的错误数量。
# deleted_bytes：实际成功删除文件的总字节数。
# skipped_count：扫描后发生变化或已不存在而跳过的数量。

import argparse
import re
import stat
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from notifier import (
    NotificationError,
    redact_sensitive,
    send_cleanup_notification,
)


# ---------- 1. 定义路径和允许清理的文件名 ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"

# 与 reporter.py 生成的文件名对应。
# 日期 8 位、时间 6 位，最后是 32 位十六进制随机标识。
REPORT_PATTERN = re.compile(
    r"inspection_[0-9]{8}_[0-9]{6}_[0-9a-f]{32}\.txt"
)

# 与 server_check.sh 的 mktemp 命名方式对应。
# 最后的随机部分为 6 位字母或数字。
RUN_PATTERN = re.compile(
    r"run_[0-9]{8}_[0-9]{6}_[A-Za-z0-9]{6}\.log"
)


# ---------- 2. 读取保留天数和执行模式 ----------

def positive_days(raw):
    """把参数转换成整数，并拒绝零或负数。"""

    try:
        days = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("保留天数必须是整数")

    if days < 1:
        raise argparse.ArgumentTypeError("保留天数必须至少为 1")

    return days


def parse_args():
    """默认保留 7 天，默认不删除文件。"""

    parser = argparse.ArgumentParser(
        description="预览或清理过期巡检报告、单次运行日志"
    )

    parser.add_argument(
        "--keep-days",
        type=positive_days,
        default=7,
        help="保留最近多少天，默认 7",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际删除；不指定时只预览",
    )

    parser.add_argument(
        "--notify",
        action="store_true",
        help="实际清理后发送钉钉资料清理汇总；必须与 --apply 同用",
    )

    args = parser.parse_args()

    if args.notify and not args.apply:
        parser.error("--notify 必须与 --apply 同时使用")

    return args


# ---------- 3. 从配置中确定需要检查的目录 ----------

def load_project_config():
    """读取项目配置，供目录解析和清理通知共同使用。"""

    config = yaml.safe_load(
        CONFIG_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(config, dict):
        raise ValueError("配置文件内容必须是字典")

    return config


def load_directories(config=None):
    """返回报告目录和日志目录，以及各自的文件名规则。"""

    if config is None:
        config = load_project_config()

    output = config.get("output")

    if not isinstance(output, dict):
        raise ValueError("output 必须是配置字典")

    directories = []

    for key, pattern in (
        ("report_dir", REPORT_PATTERN),
        ("log_dir", RUN_PATTERN),
    ):
        raw_path = output.get(key)

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("output.{} 必须是非空字符串".format(key))

        if any(char in raw_path for char in ("\x00", "\n", "\r")):
            raise ValueError("output.{} 包含非法控制字符".format(key))

        directory = Path(raw_path)

        # 相对路径按项目根目录解释，与报告模块保持一致。
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory

        directory = directory.resolve()

        if not directory.is_dir():
            raise ValueError("目录不存在：{}".format(directory))

        directories.append((directory, pattern))

    return directories


# ---------- 4. 扫描符合命名规则且已经过期的普通文件 ----------

def find_candidates(directories, cutoff):
    """只扫描目录第一层，返回过期文件和扫描时的状态。"""

    candidates = []

    for directory, pattern in directories:
        for path in directory.iterdir():

            # fullmatch 要求整个文件名符合规则。
            # 演示报告和 cron_launcher.log 都不会被选中。
            if pattern.fullmatch(path.name) is None:
                continue

            try:
                # lstat 读取链接本身的信息，不跟随符号链接。
                file_stat = path.lstat()
            except FileNotFoundError:
                # 扫描过程中被其他操作移除的文件，直接跳过。
                continue

            # 只允许普通文件，目录和符号链接都跳过。
            if not stat.S_ISREG(file_stat.st_mode):
                continue

            # 依据文件修改时间判断，不依据文件名里的日期判断。
            if file_stat.st_mtime < cutoff:
                candidates.append((path, file_stat))

    # 按修改时间排序，让最旧的文件显示在前面。
    candidates.sort(key=lambda item: item[1].st_mtime)

    return candidates


# ---------- 5. 删除前确认文件没有发生变化 ----------

def same_file_state(before, after):
    """比较文件身份、大小和修改时间，避免删除扫描后变化的文件。"""

    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def delete_candidates_detailed(candidates, cutoff):
    """逐个删除并返回数量、字节、跳过和失败的完整统计。"""

    deleted_count = 0
    deleted_bytes = 0
    skipped_count = 0
    error_count = 0

    for path, before in candidates:
        try:
            after = path.lstat()

            # 扫描之后变成链接、被替换或者被写入的文件，不删除。
            if (
                not stat.S_ISREG(after.st_mode)
                or not same_file_state(before, after)
                or after.st_mtime >= cutoff
            ):
                skipped_count += 1
                print("跳过发生变化的文件：{}".format(path))
                continue

            path.unlink()
            deleted_count += 1
            deleted_bytes += before.st_size
            print("已删除：{}".format(path))

        except FileNotFoundError:
            skipped_count += 1
            print("文件已不存在，跳过：{}".format(path))

        except OSError as exc:
            error_count += 1
            print(
                "删除失败：{}；原因：{}".format(path, exc),
                file=sys.stderr,
            )

    return {
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "skipped_count": skipped_count,
        "error_count": error_count,
    }


def delete_candidates(candidates, cutoff):
    """保留原有二元返回接口，供现有调用者和测试继续使用。"""

    summary = delete_candidates_detailed(candidates, cutoff)
    return summary["deleted_count"], summary["error_count"]


# ---------- 6. 组织预览和实际清理过程 ----------

def main():
    """先显示清理范围，再根据参数决定是否删除。"""

    args = parse_args()
    keep_days = args.keep_days

    # 这里的“7 天”指连续 7 × 24 小时。
    cutoff = time.time() - keep_days * 24 * 60 * 60

    try:
        config = load_project_config()
        directories = load_directories(config)
        candidates = find_candidates(directories, cutoff)
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        print("清理准备失败：{}".format(exc), file=sys.stderr)
        return 2

    print("执行模式：{}".format(
        "实际删除" if args.apply else "仅预览，不删除"
    ))
    print("保留天数：{}".format(keep_days))
    print("过期分界：{}".format(
        datetime.fromtimestamp(cutoff).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S %z"
        )
    ))

    for directory, pattern in directories:
        print("检查目录：{}".format(directory))

    total_bytes = sum(
        file_stat.st_size for path, file_stat in candidates
    )

    print("待清理文件数：{}".format(len(candidates)))
    print("待清理文件总大小：{:.2f} MiB".format(
        total_bytes / (1024 * 1024)
    ))

    for path, file_stat in candidates:
        print("待清理：{}".format(path))

    if not args.apply:
        print("预览完成，没有删除任何文件。")
        return 0

    delete_summary = delete_candidates_detailed(
        candidates, cutoff
    )

    print("实际删除：{} 个文件".format(
        delete_summary["deleted_count"]
    ))
    print("实际释放：{:.2f} MiB".format(
        delete_summary["deleted_bytes"] / (1024 * 1024)
    ))
    print("跳过文件：{} 个".format(delete_summary["skipped_count"]))
    print("删除失败：{} 个文件".format(delete_summary["error_count"]))

    notification_failed = False

    if args.notify:
        cleanup_summary = dict(delete_summary)
        cleanup_summary.update({
            "candidate_count": len(candidates),
            "candidate_bytes": total_bytes,
            "keep_days": keep_days,
        })

        try:
            notification_result = send_cleanup_notification(
                config,
                cleanup_summary,
            )
            print("钉钉通知：{}".format(notification_result["message"]))
        except NotificationError as exc:
            notification_failed = True
            print(
                "钉钉资料清理通知失败：{}".format(
                    redact_sensitive(exc)
                ),
                file=sys.stderr,
            )
        except Exception as exc:
            notification_failed = True
            print(
                "钉钉资料清理通知发生未预期异常：{}".format(
                    type(exc).__name__
                ),
                file=sys.stderr,
            )

    return 2 if delete_summary["error_count"] or notification_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
