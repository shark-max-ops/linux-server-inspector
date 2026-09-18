#!/bin/bash

# 文件用途：
# 使用项目虚拟环境启动完整巡检，阻止同一项目重复运行，
# 同时把运行过程显示在终端并保存到日志文件。

: <<'FLOW'
整体执行流程：
开始
  ↓
设置命令搜索路径，定位项目根目录
  ↓
检查 root 身份、Python 环境和必要文件
  ├─ 检查失败 → 输出错误，退出码 2
  ↓
尝试获取本项目的运行锁
  ├─ 已有巡检运行 → 跳过本次执行，退出码 3
  ├─ 获取锁发生错误 → 输出错误，退出码 2
  ↓
读取 config.yaml 中的日志目录
  ├─ 配置错误或目录无法创建 → 输出错误，退出码 2
  ↓
创建本次独立的运行日志
  ↓
启动 main.py，输出同时显示在终端并写入日志
  ↓
分别取得 Python 和日志写入命令的退出码
  ├─ 日志写入失败 → 退出码 2
  ├─ Python 返回非约定状态 → 记录异常，退出码 2
  ↓
记录结束时间和最终退出码
  ↓
脚本结束，自动释放运行锁
  ↓
返回退出码：PASS 为 0，WARN 为 1，FAIL 为 2，重复运行跳过为 3
FLOW

# 主要变量：
# PROJECT_ROOT：项目根目录。
# PYTHON_BIN：项目虚拟环境中的 Python。
# MAIN_FILE：真实巡检入口 main.py。
# CONFIG_FILE：项目配置文件。
# LOCK_FILE：本项目的运行锁文件。
# LOCK_STATUS：获取运行锁的结果。
# LOG_DIR：从配置读取的运行日志目录。
# LOG_FILE：本次运行日志文件。
# PIPE_CODES：Python 和 tee 各自的退出码。
# PYTHON_STATUS：main.py 的退出码。
# TEE_STATUS：日志输出命令的退出码。
# FINAL_STATUS：启动脚本最终返回的退出码。


# ---------- 1. 设置基础环境 ----------

# 使用未定义变量时直接报错。
# 不使用 set -e，因为巡检返回 WARN=1 是正常业务结果，
# 后面仍需要继续记录结束时间和退出码。
set -u
set -o pipefail

# 定时任务的 PATH 可能很短，这里明确设置常用命令目录。
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Python 输出统一使用 UTF-8，避免定时任务环境下中文输出失败。
export PYTHONIOENCODING=utf-8

# 新创建的日志和锁文件默认仅允许当前用户访问。
umask 077


# ---------- 2. 提供统一的报错函数 ----------

fail() {
    # $1 是调用本函数时传入的错误说明。
    printf 'server_check.sh: %s\n' "$1" >&2
    exit 2
}


# ---------- 3. 定位项目并检查运行条件 ----------

# 根据当前脚本所在位置寻找项目根目录。
# 因此可以在 /root、/tmp 等任意目录启动它。
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || fail "无法定位项目目录"

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
MAIN_FILE="$PROJECT_ROOT/src/main.py"
CONFIG_FILE="$PROJECT_ROOT/config/config.yaml"
LOCK_FILE="$PROJECT_ROOT/.server_check.lock"

# 当前 security.sh 和 log.sh 的完整采集需要 root 权限。
if [[ "$EUID" -ne 0 ]]; then
    fail "请使用 root 运行完整巡检"
fi

[[ -x "$PYTHON_BIN" ]] || fail "找不到可执行的项目 Python：$PYTHON_BIN"
[[ -r "$MAIN_FILE" ]] || fail "无法读取入口文件：$MAIN_FILE"
[[ -r "$CONFIG_FILE" ]] || fail "无法读取配置文件：$CONFIG_FILE"

command -v flock >/dev/null 2>&1 || fail "找不到 flock 命令"
command -v tee >/dev/null 2>&1 || fail "找不到 tee 命令"
command -v mktemp >/dev/null 2>&1 || fail "找不到 mktemp 命令"

cd -- "$PROJECT_ROOT" || fail "无法进入项目目录"


# ---------- 4. 获取运行锁，防止同时启动多个巡检 ----------
# 保证同一时间只有一个巡检脚本在跑，避免两个巡检同时启动、互相干扰、重复写日志。
# 文件描述符 9 可以理解为：给这个打开的锁文件一个编号。
# 使用追加方式打开，不需要向里面写入任何内容。
exec 9>>"$LOCK_FILE" || fail "无法打开运行锁文件"

# -n：锁已被占用时立即返回，不排队等待。
# -E 3：把“锁被占用”的退出码指定为 3。
flock -n -E 3 9
LOCK_STATUS=$?

if [[ "$LOCK_STATUS" -eq 3 ]]; then
    printf '已有巡检正在运行，本次跳过。\n' >&2
    exit 3
elif [[ "$LOCK_STATUS" -ne 0 ]]; then
    fail "获取运行锁失败，退出码：$LOCK_STATUS"
fi

# 不删除锁文件。
# 是否加锁由内核管理，不是根据这个文件是否存在判断。
# 本脚本及持有该描述符的子进程结束后，锁会自动释放。


# ---------- 5. 读取配置中的日志目录 ----------

# 使用项目已有的 Python 和 PyYAML 读取 YAML。
# heredoc 中的内容作为 Python 程序执行。
# 两个路径作为命令行参数传入，避免直接拼接进 Python 代码。
LOG_DIR="$(
    "$PYTHON_BIN" - "$PROJECT_ROOT" "$CONFIG_FILE" <<'PY'
# 文件用途：
# 本段内嵌 Python 从项目配置读取日志目录，创建目录并输出绝对路径。

"""
整体执行流程：
开始
  ↓
接收项目根目录和配置文件路径
  ↓
读取 YAML，检查 output.log_dir
  ├─ 配置不合法 → 向标准错误输出说明，退出码 2
  ↓
把相对路径转换为项目根目录下的路径
  ↓
创建日志目录，输出绝对路径
  ↓
结束
"""

# 主要变量：
# project_root：项目根目录。
# config_file：配置文件路径。
# config：读取的配置字典。
# output：输出相关配置。
# raw_dir：配置中的日志目录字符串。
# log_dir：转换后的绝对目录路径。

import sys
from pathlib import Path

import yaml

try:
    project_root = Path(sys.argv[1])
    config_file = Path(sys.argv[2])

    # 使用 UTF-8 读取文件，再交给 YAML 解析器。
    config = yaml.safe_load(
        config_file.read_text(encoding="utf-8")
    )

    if not isinstance(config, dict):
        raise ValueError("配置文件内容必须是字典")

    output = config.get("output")

    if not isinstance(output, dict):
        raise ValueError("output 必须是配置字典")

    raw_dir = output.get("log_dir")

    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise ValueError("output.log_dir 必须是非空字符串")

    # 日志路径需要通过一行文字返回给 Shell。
    # 因此不允许换行、回车或空字符。
    if any(char in raw_dir for char in ("\n", "\r", "\x00")):
        raise ValueError("output.log_dir 包含不允许的控制字符")

    log_dir = Path(raw_dir)

    if not log_dir.is_absolute():
        log_dir = project_root / log_dir

    log_dir = log_dir.resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

except (OSError, ValueError, yaml.YAMLError) as exc:
    print("日志目录配置错误：{}".format(exc), file=sys.stderr)
    sys.exit(2)

print(str(log_dir))
PY
)" || fail "无法准备运行日志目录"


# ---------- 6. 为本次运行创建独立日志 ----------

# mktemp 在文件名末尾生成随机字符，避免重复运行覆盖旧日志。
LOG_FILE="$(mktemp "$LOG_DIR/run_$(date +%Y%m%d_%H%M%S)_XXXXXX.log")" || fail "无法创建运行日志"

# 大括号把几条输出命令作为一组，统一传给 tee。
# tee 会同时将文字显示在终端、追加到日志文件。
{
    printf '开始时间：%s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
    printf '项目目录：%s\n' "$PROJECT_ROOT"
    printf '运行日志：%s\n' "$LOG_FILE"
    printf '\n'
} | tee -a -- "$LOG_FILE"

if [[ "$?" -ne 0 ]]; then
    fail "无法写入运行日志"
fi


# ---------- 7. 执行真实巡检，并保存全部输出 ----------

# -u：让 Python 及时输出，避免日志长时间停留在缓冲区。
# 2>&1：把错误输出也合并进来，便于日志保留报错信息。
"$PYTHON_BIN" -u "$MAIN_FILE" 2>&1 | tee -a -- "$LOG_FILE"

# 必须紧接着保存 PIPESTATUS。
# 如果先执行 echo 等其他命令，原来的状态就会被覆盖。
# 第 0 项属于 Python，第 1 项属于 tee。
PIPE_CODES=("${PIPESTATUS[@]}")
PYTHON_STATUS="${PIPE_CODES[0]}"
TEE_STATUS="${PIPE_CODES[1]}"


# ---------- 8. 确定最终退出码 ----------

# 正常情况下保留 main.py 的退出码。
case "$PYTHON_STATUS" in
    0|1|2)
        FINAL_STATUS="$PYTHON_STATUS"
        ;;
    *)
        # 例如程序被信号终止，返回了其他数字。
        # 对统一启动入口来说，这属于执行异常。
        FINAL_STATUS=2
        ;;
esac

# 即使分析成功，只要运行日志写入失败，也不能报告整体执行成功。
if [[ "$TEE_STATUS" -ne 0 ]]; then
    FINAL_STATUS=2
    printf '运行日志写入失败，tee 退出码：%s\n' "$TEE_STATUS" >&2
fi


# ---------- 9. 记录结束信息，并退出 ----------

{
    printf '\n'
    printf '结束时间：%s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
    printf 'Python 退出码：%s\n' "$PYTHON_STATUS"
    printf '启动脚本退出码：%s\n' "$FINAL_STATUS"
} | tee -a -- "$LOG_FILE"

if [[ "$?" -ne 0 ]]; then
    printf '无法完整写入日志结束信息。\n' >&2
    FINAL_STATUS=2
fi

exit "$FINAL_STATUS"