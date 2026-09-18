<!--
文件用途：
记录 Linux Server Inspector 的环境准备、部署、验证、
暂停任务、代码升级与恢复运行的方法。

整体执行流程：
准备 Linux 环境
  ↓
上传项目代码，创建项目虚拟环境
  ↓
安装依赖，检查配置
  ↓
运行自动化测试和手动巡检
  ├─ 验证失败 → 排查后重新验证
  ↓
部署 cron 和 logrotate 配置
  ↓
确认自动巡检生效
  ↓
后续升级时暂停调度，等待正在运行的任务结束
  ↓
备份、上传修改、重新测试
  ├─ 验证失败 → 保持暂停，修复或恢复备份
  ↓
恢复调度，确认自动运行

主要路径：
项目目录：/opt/linux-server-inspector
虚拟环境：/opt/linux-server-inspector/.venv
cron 配置：/etc/cron.d/linux-server-inspector
轮转配置：/etc/logrotate.d/linux-server-inspector
-->

# 部署与维护说明

## 1. 操作端与适用范围

| 操作 | 所在端 |
|---|---|
| 编辑代码和配置 | Windows：VSCode |
| 上传、下载文件 | Windows：Xftp |
| 执行命令和检查运行结果 | Linux：通过 Xshell |
| 自动执行巡检 | Linux：crond |

当前部署边界：

- Linux 发行版、Bash 和 Python 以目标机实际结果为准。
- Python 依赖以 `requirements.txt` 为准。
- root 身份运行完整巡检。
- 项目安装在 `/opt/linux-server-inspector`。

下文命令均在 Linux 端执行，除非明确标注 Windows。

当前 Windows 工作区只用于编辑和上传，不能代替目标 Linux 验证。更换目标环境后需要重新执行本文件中的检查。

## 2. 上传文件的格式要求

Windows VSCode 中统一使用：

- UTF-8 编码。
- LF 换行。
- 文件末尾保留换行。

特别注意以下文件：

- `*.sh`
- `config/inspector.cron`
- `config/inspector.logrotate`

检查 Linux 文件的换行：

```bash
# 显示行尾；^M$ 表示 CRLF，正常 LF 行尾显示 $。
cat -A /opt/linux-server-inspector/config/inspector.cron
```

`cat -A` 可能把中文显示成特殊字符，不代表中文已经损坏。

如果需要在 Linux 修复某个文件的 CRLF：

```bash
# 仅删除每行末尾的回车字符。
sed -i 's/\r$//' /opt/linux-server-inspector/config/inspector.cron
```

Windows 中的源文件也要同步改成 LF，避免下次上传时再次出现问题。

## 3. 准备项目 Python 环境

本节只在新部署时执行。

不要删除或替换系统自带的 Python 2，也不要把 `/usr/bin/python` 改成 Python 3。

### 3.1 检查 Python 3

```bash
# 查看 Python 3 是否存在及其版本。
python3 --version
```

本项目实验环境使用独立的 CentOS 归档仓库配置安装 Python 3。

如果新机器尚未准备软件源，应先检查
`scripts/setup_yum_repo.sh` 的内容，确认适用于目标环境后再执行。
该脚本不是日常巡检的一部分。

已经准备好 inspector 仓库后，可使用：

```bash
# 仅从项目指定的仓库安装 Python 3 和 pip。
yum --disablerepo='*' --enablerepo='inspector-base,inspector-updates,inspector-extras' install python3.x86_64 python3-pip
```

### 3.2 创建虚拟环境

先使用 Xftp 将项目上传到：

`/opt/linux-server-inspector`

不要把 Windows 的虚拟环境上传给 Linux 使用。

```bash
# 在 Linux 本机创建项目虚拟环境；已有环境无需重复创建。
python3 -m venv /opt/linux-server-inspector/.venv
```

```bash
# 检查项目实际使用的 Python。
/opt/linux-server-inspector/.venv/bin/python --version
```

### 3.3 安装依赖

```bash
# 从 requirements.txt 安装项目依赖。
/opt/linux-server-inspector/.venv/bin/python -m pip install --only-binary=:all: -r /opt/linux-server-inspector/requirements.txt
```

如果目标解释器与依赖不兼容，应选择项目目标环境可用的解释器或调整依赖声明并重新验证；不要在文档中另写一套固定版本。

验证：

```bash
# 检查 YAML 模块是否能正常导入。
/opt/linux-server-inspector/.venv/bin/python -c "import yaml; print(yaml.__version__)"
```

使用绝对路径启动虚拟环境 Python，不必提前执行 activate。

## 4. 部署前检查

### 4.1 检查项目配置

在 Windows VSCode 中检查 `config/config.yaml`：

- CPU、内存、磁盘阈值。
- 必需进程和服务。
- 必需端口、允许端口。
- 安全与日志告警开关。
- 钉钉告警开关、重复间隔、请求超时、消息限制和状态路径。
- 报告和日志目录。
- 采集超时。

上传后，主程序会在正式采集前验证配置。

不要根据示例盲目添加当前主机未部署的服务。

### 4.2 检查 Shell 语法

```bash
# 用途：批量检查 Shell 语法，不执行脚本内容。
# 变量：script_file 为当前文件；failed 为整批检查的失败标记。
# 流程：逐个检查 → 输出结果 → 返回整批检查状态。
(
    failed=0

    for script_file in /opt/linux-server-inspector/server_check.sh /opt/linux-server-inspector/scripts/*.sh; do
        if bash -n "$script_file"; then
            printf '语法通过：%s\n' "$script_file"
        else
            printf '语法失败：%s\n' "$script_file" >&2
            failed=1
        fi
    done

    exit "$failed"
)
```

```bash
# 紧接上一条命令，读取检查结果。
echo "Shell 检查退出码：$?"
```

外层括号创建子 Shell，最后的 exit 不会关闭 Xshell 会话。

### 4.3 运行自动化测试

```bash
# 自动运行 tests 下的 test_*.py。
/opt/linux-server-inspector/.venv/bin/python -m unittest discover -s /opt/linux-server-inspector/tests -p 'test_*.py' -v
```

```bash
# 预期为 0，测试输出末尾应显示 OK。
echo "测试退出码：$?"
```

测试数量以自动发现命令的实际输出为准。通知测试全部使用模拟传输，不连接真实钉钉。

### 4.4 手动运行完整巡检

```bash
# 通过统一入口运行，以获得运行锁和运行日志。
bash /opt/linux-server-inspector/server_check.sh
```

```bash
# 紧接着查看退出码。
echo "巡检退出码：$?"
```

| 退出码 | 含义 |
|---:|---|
| 0 | PASS |
| 1 | WARN |
| 2 | FAIL 或执行错误 |
| 3 | 已有巡检运行，本次跳过 |

WARN 不等于部署失败，但必须核对报告中的具体告警。

FAIL 需要区分检查发现的问题与采集、配置、程序错误。

## 5. 部署自动任务

### 5.1 准备入口日志目录

```bash
# cron 打开重定向文件之前，目录必须已经存在。
mkdir -p /opt/linux-server-inspector/logs
```

### 5.2 检查定时服务

```bash
# 查看服务运行状态。
systemctl is-active crond
```

```bash
# 查看是否配置开机启动。
systemctl is-enabled crond
```

若未运行或未启用，按需要执行：

```bash
# 启动 crond，并配置开机启动。
systemctl enable --now crond
```

### 5.3 安装任务配置

```bash
# 部署项目定时任务。
install -o root -g root -m 644 /opt/linux-server-inspector/config/inspector.cron /etc/cron.d/linux-server-inspector
```

当前约定：

- 每分钟执行一次巡检。
- 每天 20:40 清理超过 7 天的正式报告与单次运行日志，并发送钉钉资料清理汇总。

不需要把同样的任务再次添加到 `crontab -e`，避免重复调度。

### 5.4 验证自动执行

等待一到两分钟，不手动启动巡检，然后查看：

```bash
# 查看自动巡检输出。
tail -n 40 /opt/linux-server-inspector/logs/cron_launcher.log
```

```bash
# 查看系统是否调度了项目任务。
tail -n 30 /var/log/cron
```

应看到新的开始时间、报告路径及结束信息。

## 6. 部署日志轮转

```bash
# 安装两个入口日志的轮转配置。
install -o root -g root -m 644 /opt/linux-server-inspector/config/inspector.logrotate /etc/logrotate.d/linux-server-inspector
```

```bash
# 检查配置，不实际轮转。
logrotate -d /etc/logrotate.d/linux-server-inspector
```

必须同时查看输出中是否存在 `error:`，不能只看退出码。

当前规则：

- 按天判断是否轮转。
- 每个入口日志最多保留 7 份历史文件。
- 压缩历史日志，最近一份延迟压缩。
- 缺失或空日志跳过。

由系统现有的 logrotate 调度执行，不需要再新增一条相同的 cron 任务。

调试检查通过，不等于已验证实际自动轮转。
后续需要查看历史日志文件及系统调度记录。

## 7. 验证和执行过期清理

预览：

```bash
# 只显示待删除文件。
/opt/linux-server-inspector/.venv/bin/python /opt/linux-server-inspector/src/cleanup.py --keep-days 7
```

实际执行：

```bash
# 实际删除符合规则且超过 7 天的文件。
# 使用与定时清理相同的独立锁。
/usr/bin/flock -n /opt/linux-server-inspector/.cleanup.lock /opt/linux-server-inspector/.venv/bin/python /opt/linux-server-inspector/src/cleanup.py --keep-days 7 --apply
```

```bash
# 查看处理结果。
echo "退出码：$?"
```

检查范围由 YAML 中的报告目录和日志目录决定。

当前清理工具不会删除：

- 演示报告。
- cron_launcher.log。
- cleanup_launcher.log。
- 子目录中的文件。
- 符号链接。
- 不符合命名规则的文件。

## 8. 升级前暂停项目任务

本节只在需要更新代码时执行。

不要停止整个 crond 服务，否则会影响主机上的其他定时任务。

### 8.1 将项目配置移出调度目录

```bash
# 创建项目维护期间存放配置的目录。
install -d -o root -g root -m 700 /opt/linux-server-inspector/maintenance
```

```bash
# 暂停本项目的巡检和清理调度。
# -i：目标文件已存在时先询问，避免覆盖以前保存的配置。
mv -i /etc/cron.d/linux-server-inspector /opt/linux-server-inspector/maintenance/inspector.cron.paused
```

配置移出后，等待至少一个调度周期，让 crond 发现变化。

这只停止后续调度，不会终止已经启动的任务。

### 8.2 等待正在执行的巡检和清理结束

```bash
# 等待现有巡检释放锁，最长等待 180 秒。
# 获得锁后 true 立即结束，本命令不会启动新巡检。
flock -w 180 /opt/linux-server-inspector/.server_check.lock true
```

```bash
# 预期为 0；非 0 时不要开始上传代码。
echo "巡检等待退出码：$?"
```

```bash
# 等待现有清理任务结束。
flock -w 180 /opt/linux-server-inspector/.cleanup.lock true
```

```bash
# 预期为 0。
echo "清理等待退出码：$?"
```

等待命令结束后不会继续持有锁。
维护期间不要在其他终端手动启动巡检或清理。

锁文件保留即可，不要删除。

## 9. 备份与更新代码

确认定时任务已暂停、现有任务已结束后再执行。

### 9.1 备份当前代码

```bash
# 创建备份目录。
install -d -o root -g root -m 700 /opt/linux-server-inspector/backups
```

```bash
# 备份核心代码和配置，不打包报告、日志及虚拟环境。
# 日期和时间用于区分每次备份。
tar -czf "/opt/linux-server-inspector/backups/code_$(date +%Y%m%d_%H%M%S).tar.gz" -C /opt/linux-server-inspector server_check.sh src scripts config tests requirements.txt README.md docs
```

备份压缩包不包含已经部署到 `/etc` 的文件。
日常维护应保持项目中的配置副本与已部署配置一致。

### 9.2 Windows 端上传修改

使用 VSCode 编辑并保存为 UTF-8、LF。

使用 Xftp 上传修改后的源代码和配置。

不要上传 Windows `.venv` 覆盖 Linux 虚拟环境。

如果依赖发生变化，应先确认新版本兼容当前 Python，再在 Linux 环境安装和验证。

本次钉钉告警功能需要用 Xftp 上传或同步这些文件：

- `src/main.py`
- `src/notifier.py`
- `src/cleanup.py`
- `tests/test_notifier.py`
- `tests/test_cleanup.py`
- `config/config.yaml`
- `config/inspector.cron`
- `.gitignore`
- `README.md`
- `docs/architecture.md`
- `docs/deployment.md`

`requirements.txt` 没有新增依赖，无需上传 Windows `.venv`。如果服务器采用整目录同步，仍应排除本地虚拟环境、`runtime/`、日志、报告和凭据。

### 9.3 Linux 端配置钉钉凭据并预览

本节在项目调度保持暂停、现有任务已经结束后执行。不要把真实 Webhook 或 Secret 发到聊天中，也不要写入仓库内的 YAML。

先创建仓库外受限目录和文件：

```bash
# 只允许 root 访问凭据目录和文件。
install -d -o root -g root -m 700 /etc/linux-server-inspector
install -o root -g root -m 600 /dev/null /etc/linux-server-inspector/dingtalk.env
```

使用 Linux 端编辑器打开：

```bash
vi /etc/linux-server-inspector/dingtalk.env
```

按下面格式填写实际值；这里仅为占位示例：

```text
DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=请替换为真实Token
DINGTALK_WEBHOOK_SECRET=SEC请替换为真实加签密钥
```

保存后再次确认权限：

```bash
chown root:root /etc/linux-server-inspector/dingtalk.env
chmod 600 /etc/linux-server-inspector/dingtalk.env
```

项目也支持同名环境变量，但交互终端中临时 `export` 的变量不会自动进入 cron。定时运行建议使用上述仓库外受限文件，不要把凭据直接写进 cron 文件。

先执行 dry-run。它使用模拟故障，只打印预览，不读取凭据、不发送请求、不更新状态：

```bash
/opt/linux-server-inspector/.venv/bin/python /opt/linux-server-inspector/src/notifier.py --dry-run
```

检查输出中应包含模拟主机、带时区时间、FAIL 明细和“测试告警”说明。

只有在人工确认群、机器人和凭据无误后，才显式执行一次测试发送：

```bash
/opt/linux-server-inspector/.venv/bin/python /opt/linux-server-inspector/src/notifier.py --test-send
```

该命令只发送标注“测试告警”的模拟数据，不制造磁盘满、不停止 SSH 或其他服务，也不更新真实故障状态。网络超时的送达结果可能不确定，应先查看钉钉群再决定是否重试。

### 9.4 Linux 端重新验证

按照第 4 节：

1. 检查 Shell 语法。
2. 运行自动化测试。
3. 执行钉钉 dry-run。
4. 经人工确认后执行一次显式 test-send，并确认群内收到“测试告警”。
5. 按准备启用的实际配置手动执行完整巡检，检查报告、快照和原有退出码。
6. 检查报告和日志。

验证失败时保持调度暂停，先修复问题。
不要在新旧代码混合或测试失败的状态下恢复自动运行。

## 10. 恢复定时任务

如果轮转配置有变化，先重新部署并验证：

```bash
# 更新日志轮转配置。
install -o root -g root -m 644 /opt/linux-server-inspector/config/inspector.logrotate /etc/logrotate.d/linux-server-inspector
```

```bash
# 确认没有配置解析错误。
logrotate -d /etc/logrotate.d/linux-server-inspector
```

确认测试消息送达后，先在 Windows VSCode 中修改
`config/config.yaml`，只将：

```yaml
alerts:
  dingtalk:
    enabled: true
```

其余阈值和检查对象保持已核对的值，再通过 Xftp 覆盖 Linux 端对应文件。随后通过统一入口手动执行一次真实巡检：

```bash
bash /opt/linux-server-inspector/server_check.sh
```

确认报告先生成、`runtime/latest_inspection.json` 已更新、日志中的通知结果明确、退出码仍只表示原有巡检状态。若当前没有 FAIL，日志显示没有匹配或到期告警是正常结果。

确认代码、配置和手动巡检验证通过后，恢复项目每分钟调度：

```bash
# 以项目内已核对的配置恢复巡检与清理任务。
install -o root -g root -m 644 /opt/linux-server-inspector/config/inspector.cron /etc/cron.d/linux-server-inspector
```

维护目录中保存的旧配置留作核对，不会被 cron 执行。

等待一到两分钟，再查看：

```bash
# 确认恢复后出现新的自动运行记录。
tail -n 40 /opt/linux-server-inspector/logs/cron_launcher.log
```

同时确认：

- 巡检仍为每分钟一次。
- 报告路径正常。
- 没有 Python Traceback 或导入错误。
- 退出码与报告状态一致。

## 11. 升级失败时的处理

1. 保持项目 cron 配置处于暂停状态。
2. 查看单次运行日志和终端错误。
3. 从对应备份中恢复需要回退的代码和配置。
4. 如果升级引入了新文件，逐项核对是否需要移除；解压旧备份不会自动删除新增文件。
5. 如果修改过 Python 依赖，单纯恢复代码压缩包不会回退虚拟环境。
6. 重新运行测试和手动巡检。
7. 验证通过后再恢复调度。

不要未经核对就批量删除项目目录或覆盖 `.venv`。

## 12. 常见部署故障

| 现象 | 优先检查 |
|---|---|
| cron 报 bad minute | CRLF、末尾换行、时间字段和用户字段 |
| cron 入口日志不存在 | 是否已触发、目录是否存在、crond 状态 |
| Python 模块无法导入 | 是否使用项目虚拟环境、依赖是否安装 |
| 无法读取日志或安全配置 | 是否以 root 运行 |
| 退出码为 1 | 查看是否为正常 WARN |
| 退出码为 3 | 是否已有巡检持有运行锁 |
| logrotate 出现配置错误 | 换行格式、配置语法、报错行 |
| 报告目录修改后日志仍写在旧目录 | cron 和 logrotate 中存在固定路径，需要同步检查 |
| 关机后没有补执行 | 普通 cron 不补执行错过的任务 |
| dry-run 成功但 test-send 失败 | 凭据文件路径、600 权限、Webhook/Secret、DNS、TLS 和防火墙 |
| test-send 超时 | 先到钉钉群确认是否已送达；不要立即连续重试 |
| 每分钟重复同一告警 | 状态文件是否可写、业务响应是否成功、重复间隔配置是否被修改 |
| 通知失败但巡检退出码仍为 0/1/2 | 这是保留原退出码的设计；查看运行日志中的“钉钉通知失败” |

## 13. 当前部署验证记录

目标 Linux 主机已经验证：

- Python 自动化测试通过；数量以当次自动发现输出为准。
- Shell 文件语法检查通过。
- 手动完整巡检正常生成中文报告。
- 每分钟自动巡检连续触发。
- 运行锁能够阻止重复执行。
- 清理工具的预览、模拟删除测试和手动执行通过。
- logrotate 配置调试检查无解析错误。
- 钉钉 dry-run 正常，显式 test-send 退出码为 0，测试消息已人工确认送达。
- 最近巡检快照已生成并使用受限权限保存。

尚需通过实际运行记录确认：

- 每天 20:40 的清理与资料清理通知自动触发。
- 系统调度下的日志实际轮转和历史文件压缩。
- 真实 FAIL 告警、重复间隔和故障恢复后再次出现的生产运行效果。
