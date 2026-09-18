# -*- coding: utf-8 -*-
# ============================================================
# 文件名称：collector.py
# 文件用途：
#   调用项目中的 Shell 采集脚本，将 key=value 输出解析为字典。

#（collector.py 调用 Shell 的核心是：
# 用 Python 的 subprocess.Popen() 启动 /bin/bash，
# 让 Bash 去执行指定的 .sh 脚本，再把输出收回 Python。）
#
#   处理脚本失败、超时、编码异常和输出格式错误。
#   兼容 Python 3.6，不依赖第三方库。
"""
整体执行流程：
开始（直接运行本文件）
  ↓
解析命令行中的脚本名称和脚本参数
  ├─ 命令行参数错误 → 提示用法，退出码 2
  ↓
调用 collect（其他模块也可直接调用此函数）
  ↓
初始化采集结果，检查脚本允许列表、参数、超时值和文件
  ├─ 检查失败 → 保存错误原因，返回失败结果
  ↓
通过 /bin/bash 启动脚本，读取标准输出和标准错误
  ├─ 超时 → 终止进程组，保存错误原因
  ├─ 启动失败或退出码非零 → 保存错误原因
  ↓
成功时严格按 UTF-8 解码，将 key=value 输出解析为字典
  ├─ 编码或格式错误 → 保存错误原因
  └─ 解析成功 → 标记 ok=True
  ↓
记录耗时，返回采集结果
  ↓
命令行入口打印 JSON
  ↓
返回退出码：采集成功为 0，失败为 1
"""
#
# 主要变量：
#   PROJECT_ROOT       根据当前文件位置确定的项目根目录
#   SCRIPTS_DIR        Shell 脚本目录
#   ALLOWED_SCRIPTS    允许调用的采集脚本名称
#
# collect 函数主要变量：
#   script_name        脚本文件名，例如 cpu.sh
#   script_args        传给 Shell 的参数列表
#   timeout_seconds    整个脚本的超时秒数
#   script_path        脚本绝对路径
#   command            传给 subprocess 的命令参数列表
#   process            子进程对象
#   stdout / stderr    子进程的原始字节输出
#   result             本次采集结果字典
#   started            开始时的单调时钟值，用于计算耗时
#
# parse_output 函数主要变量：
#   text               解码后的 Shell 标准输出
#   data               解析得到的字典
#   line_number / line 当前行号和行内容
#   key / value        按第一个等号拆分得到的字段名和值
#
# 命令行用法：
#   python src/collector.py system_info.sh
#   python src/collector.py process.sh sshd crond
#
# 结果说明：
#   ok=True 仅表示采集成功，不表示所有检查项都是 PASS。
#   data 中暂时保留字符串，由 analyzer.py 负责进一步解析。
# ============================================================


# ---------- 1. 导入标准库 ----------
import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path


# ---------- 2. 确定项目路径和允许调用的脚本 ----------
# __file__ 是当前 Python 文件的位置。
# 从 collector.py 向上两级得到项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# 不包含 setup_yum_repo.sh，避免巡检时执行环境修改操作。
ALLOWED_SCRIPTS = (
    "system_info.sh",
    "cpu.sh",
    "memory.sh",
    "disk.sh",
    "process.sh",
    "service.sh",
    "network.sh",
    "security.sh",
    "log.sh",
)


# ---------- 3. 解析 Shell 的 key=value 输出 ----------
def parse_output(text):
    """将非空输出行解析为字典；格式错误时抛出异常。"""
    data = {}

    #按行拆开，逐行编号
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue

        if "=" not in line:
            raise ValueError(
                "第 {} 行缺少等号".format(line_number)
            )

        # 只按照第一个等号拆成两部分。
        key, value = line.split("=", 1)
        key = key.strip()   #去掉字段名前后的空格。

        if not key:
            raise ValueError(
                "第 {} 行字段名为空".format(line_number)
            )

        # 重复字段不能直接覆盖，否则会悄悄丢失数据。
        if key in data:
            raise ValueError(
                "发现重复字段：{}".format(key)
            )

        data[key] = value

    if not data:
        raise ValueError("脚本没有输出有效数据")

    return data


# ---------- 4. 调用一个采集脚本 ----------
def collect(script_name, script_args=None, timeout_seconds=120):
    """执行脚本并返回结构统一的采集结果。"""
    started = time.monotonic()

    result = {
        "script": script_name,
        "ok": False,
        "returncode": None,
        "data": {},
        "stderr": "",
        "error": "",
        "duration_seconds": 0.0,
    }

    try:
        # 检查名称、参数与超时值。
        if script_name not in ALLOWED_SCRIPTS:
            raise ValueError("不允许调用该脚本：{}".format(script_name))

        if script_args is None:
            script_args = []

        if not isinstance(script_args, (list, tuple)):
            raise ValueError("script_args 必须是列表或元组")

        if not all(isinstance(arg, str) for arg in script_args):
            raise ValueError("脚本参数必须全部为字符串")

        if timeout_seconds <= 0:
            raise ValueError("超时秒数必须大于 0")

        script_path = SCRIPTS_DIR / script_name

        if not script_path.is_file():
            raise ValueError("脚本不存在：{}".format(script_path))

        # 参数分别传递，不拼接成 Shell 命令字符串。
        command = ["/bin/bash", str(script_path)] + list(script_args)

        # 使用独立进程组，便于超时时终止脚本及其派生进程。
        # stdin 使用 DEVNULL，防止采集脚本等待用户输入。
        #启动采集脚本，不让它交互输入，同时把正常输出和错误输出都交给 Python 管理，并方便超时时整体终止。
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        # 同时读取 stdout 和 stderr，避免管道缓冲区阻塞。
        try:
            stdout, stderr = process.communicate(
                timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # 进程可能恰好已经结束。
                pass

            stdout, stderr = process.communicate()

            result["returncode"] = process.returncode
            result["stderr"] = stderr.decode(
                "utf-8", errors="replace"
            ).strip()

            raise ValueError(
                "脚本执行超过 {} 秒，已终止".format(timeout_seconds)
            )

        # 先保存退出码和诊断信息。
        result["returncode"] = process.returncode
        result["stderr"] = stderr.decode(
            "utf-8", errors="replace"
        ).strip()

        if process.returncode != 0:
            raise ValueError(
                "脚本执行失败，退出码为 {}".format(process.returncode)
            )

        # 采集数据严格按 UTF-8 解码，不悄悄替换损坏的数据。
        text = stdout.decode("utf-8")
        result["data"] = parse_output(text)
        result["ok"] = True

    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # 将失败原因保留在结果中，让主程序能够继续其他检查。
        result["error"] = str(exc)

    finally:
        result["duration_seconds"] = round(
            time.monotonic() - started, 3
        )

    return result


# ---------- 5. 提供单脚本验证入口 ----------
def main():
    """从命令行选择一个脚本，打印采集结果。"""
    parser = argparse.ArgumentParser(
        description="运行一个 Linux 巡检采集脚本"
    )

    parser.add_argument(
        "script",
        choices=ALLOWED_SCRIPTS,
        help="需要运行的脚本文件名",
    )

    parser.add_argument(
        "script_args",
        nargs="*",
        help="传给采集脚本的参数，例如 sshd crond",
    )

    #就是正式读取并解析命令行参数。
    args = parser.parse_args()
    result = collect(args.script, args.script_args)

    # 使用 JSON 展示字典；转义非 ASCII 字符以适应终端编码。
    print(json.dumps(result, ensure_ascii=True, indent=2))

    return 0 if result["ok"] else 1


# ---------- 6. 直接运行时执行，导入时不执行 ----------
if __name__ == "__main__":
    raise SystemExit(main())
