# Linux Server Inspector

基于 Shell 采集和 Python 分析的 Linux 服务器巡检工具。项目面向单台 Linux 主机：定时采集系统信息，生成中文 TXT 报告，按配置发送钉钉故障告警，并在固定时间清理过期报告和单次运行日志。

本项目不自动修复系统，不修改 SSH、防火墙、端口或服务配置。

## 1. 当前功能

| 模块 | 实际职责 |
|---|---|
| 系统信息 | 主机名、操作系统、内核、运行时长、采集时间和用户 |
| 资源检查 | CPU、内存、文件系统使用率和阈值判断 |
| 进程与服务 | 检查配置中的必需进程和 systemd 服务 |
| 网络 | 网卡、地址、路由、TCP 监听、必需端口和允许列表外端口 |
| 安全 | firewalld 状态、SSH 全局磁盘配置 |
| 日志 | 系统错误、明确的 SSH 认证失败和配置服务错误日志 |
| 报告 | 原子保存 UTF-8 中文 TXT 报告 |
| 钉钉告警 | Webhook 加签、FAIL 告警、重复抑制、状态持久化和测试入口 |
| 清理 | 预览或删除过期正式报告、单次运行日志，并可发送资料清理汇总 |
| 调度 | 每分钟巡检、每日 20:40 清理、flock 防止重复运行 |

检查结果统一使用 `PASS`、`WARN`、`FAIL`，总体状态优先级为：

```text
FAIL > WARN > PASS
```

## 2. 目录和调用关系

```text
server_check.sh
  └─ src/main.py
      ├─ src/collector.py
      │   └─ scripts/*.sh
      ├─ src/analyzer.py
      ├─ src/network_analyzer.py
      ├─ src/security_analyzer.py
      ├─ src/log_analyzer.py
      ├─ src/reporter.py       → reports/inspection_*.txt
      └─ src/notifier.py       → 钉钉 Webhook、runtime 状态

config/inspector.cron
  └─ src/cleanup.py            → logs/cleanup_launcher.log
                                  └─ 可发送资料清理汇总
```

主要文件：

| 路径 | 用途 |
|---|---|
| `server_check.sh` | 统一入口、运行锁、单次运行日志和退出码传递 |
| `config/config.yaml` | 阈值、检查对象、告警和输出目录配置 |
| `config/inspector.cron` | `/etc/cron.d/` 的部署源配置 |
| `config/inspector.logrotate` | `/etc/logrotate.d/` 的部署源配置 |
| `scripts/*.sh` | Linux 原始数据采集 |
| `src/main.py` | 组织采集、分析、报告、快照和通知 |
| `src/notifier.py` | 钉钉告警、加签、去重、状态文件和测试入口 |
| `src/cleanup.py` | 过期文件预览、删除和资料清理通知 |
| `tests/` | 自动化测试；通知测试不连接真实钉钉 |

运行时目录：

| 路径 | 内容 |
|---|---|
| `reports/inspection_*.txt` | 真实巡检报告 |
| `reports/demo_report_*.txt` | `reporter.py` 直接运行生成的模拟报告 |
| `logs/run_*.log` | 单次巡检输出 |
| `logs/cron_launcher.log` | 每分钟 cron 巡检累计输出 |
| `logs/cleanup_launcher.log` | 清理任务累计输出 |
| `runtime/latest_inspection.json` | 最近一次报告成功后的原子巡检快照 |
| `runtime/dingtalk_alert_state.json` | 活动故障和最近成功发送时间 |

`runtime/`、日志、报告、虚拟环境和本地凭据已由 `.gitignore` 排除。

## 3. 一次巡检怎样执行

```text
cron 或手动启动
  ↓
server_check.sh 获取 .server_check.lock
  ↓
main.py 读取并验证 config/config.yaml
  ↓
collector.py 调用 scripts/*.sh，并限制单个采集超时
  ↓
分析模块生成 checks，汇总 PASS/WARN/FAIL
  ↓
reporter.py 先原子保存 TXT 报告
  ↓
main.py 原子保存 latest_inspection.json
  ↓
notifier.py 使用内存中的 system_info 和 checks 尝试通知
  ↓
记录结果、释放锁、返回巡检退出码
```

通知模块不读取 TXT 报告，不重新采集数据，也不重新计算检查阈值。报告保存或快照保存失败时，程序记录明确错误；通知失败不会覆盖原始检查结果或删除报告。

## 4. 配置

配置文件：`config/config.yaml`。

当前仓库配置中的资源阈值为：

| 资源 | WARN | FAIL |
|---|---:|---:|
| CPU | 70% | 90% |
| 内存 | 70% | 90% |
| 磁盘 | 75% | 90% |

实际生效内容以 Linux 端上传后的 YAML 为准，不能只看本 README。主要配置组：

- `processes`：必须存在的进程名称。
- `services`：必须处于 active 的 systemd 服务。
- `network.required_ports`：缺失时 FAIL 的 TCP 端口。
- `network.allowed_ports`：列表之外的监听通常 WARN。
- `security`：firewalld、root 登录和密码认证规则。
- `logs`：服务日志及日志告警开关。
- `collector.timeout_seconds`：单个采集脚本超时。
- `output.report_dir`、`output.log_dir`：报告和单次日志目录。

### 钉钉配置

当前配置项位于 `alerts.dingtalk`：

| 配置 | 作用 |
|---|---|
| `enabled` | 是否处理真实巡检告警；当前仓库值需以 YAML 为准 |
| `statuses` | 默认只处理 `FAIL` |
| `repeat_interval_minutes` | 同一活动故障的重复提醒间隔，当前为 30 分钟 |
| `request_timeout_seconds` | HTTPS 请求超时，当前为 10 秒 |
| `max_details`、`max_message_chars` | 故障消息明细和长度上限 |
| `at_all` | 是否 @ 所有人，默认关闭 |
| `state_file` | 活动故障状态文件 |
| `latest_snapshot_file` | 最近巡检快照路径 |
| `snapshot_max_age_minutes` | 清理通知可接受的快照时效 |
| `cleanup_notification_enabled` | 是否允许清理任务发送汇总 |
| `cleanup_max_details` | 清理消息中的检查项数量上限 |
| `credential_file` | 仓库外凭据文件 |

Webhook 和 Secret 只从以下环境变量或仓库外受限文件读取：

```text
DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=真实Token
DINGTALK_WEBHOOK_SECRET=真实加签Secret
```

不要把真实值写入 YAML、源码、测试、cron 或聊天。cron 不会自动继承交互终端临时 `export` 的变量，正式调度使用权限为 `600` 的外部凭据文件。

故障标识使用真实主机名、`category` 和 `target`；不使用容易变化的数值和原因。只有 HTTP 与钉钉业务结果同时成功，才更新最近成功发送时间。网络超时可能存在消息已送达但响应未返回的情况，程序不做无限重试，也不承诺严格只送达一次。

## 5. 手动运行

以下命令在 Linux 端执行：

```bash
# 使用统一入口执行真实巡检。
bash /opt/linux-server-inspector/server_check.sh
echo "巡检退出码：$?"
```

直接运行 `src/main.py` 仅适合调试，会绕过启动脚本的运行锁和单次日志。

### 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 总体状态 PASS |
| 1 | 总体状态 WARN |
| 2 | 总体状态 FAIL，或启动、配置、报告保存等执行错误 |
| 3 | 已有巡检持有运行锁，本次跳过 |

通知失败保留上述巡检退出码，不把外部接口结果混入原有巡检状态。

## 6. 钉钉预览和显式测试

```bash
# 模拟数据，只打印预览，不发送请求，不更新状态。
/opt/linux-server-inspector/.venv/bin/python /opt/linux-server-inspector/src/notifier.py --dry-run
```

```bash
# 发送明确标注“测试告警”的模拟消息；不制造磁盘满、不停止 SSH，不更新真实故障状态。
/opt/linux-server-inspector/.venv/bin/python /opt/linux-server-inspector/src/notifier.py --test-send
```

测试消息送达后，再根据当前 YAML 的 `enabled` 值和实际巡检结果决定是否继续启用真实告警。测试入口不会连接真实接口的自动化测试；只有显式 `--test-send` 才会发出测试请求。

## 7. 资料清理

清理只扫描报告和单次运行日志目录第一层，按文件修改时间判断是否超过保留天数，不处理演示报告、累计入口日志、子目录或符号链接。

```bash
# 默认预览，不删除。
/opt/linux-server-inspector/.venv/bin/python /opt/linux-server-inspector/src/cleanup.py --keep-days 7
```

```bash
# 实际删除；--notify 必须与 --apply 同时使用。
/usr/bin/flock -n /opt/linux-server-inspector/.cleanup.lock \
  /opt/linux-server-inspector/.venv/bin/python \
  /opt/linux-server-inspector/src/cleanup.py --keep-days 7 --apply --notify
```

`--notify` 会在实际清理后读取最近巡检 JSON 快照，发送“资料清理”汇总，包含候选、删除、释放空间、跳过、失败和 PASS/WARN/FAIL。它不解析 TXT 报告、不重新计算阈值、不修改故障告警状态。删除数量为零也会发送结果；通知失败会记录并返回清理错误码，不回滚已完成删除。

## 8. 定时任务

项目源配置：`config/inspector.cron`。

| 任务 | 调度 |
|---|---|
| 完整巡检 | 每分钟 |
| 过期文件清理和资料清理通知 | 每天 20:40 |
| 入口日志轮转 | 系统 logrotate 调度 |

部署到 Linux：

```bash
install -o root -g root -m 644 \
  /opt/linux-server-inspector/config/inspector.cron \
  /etc/cron.d/linux-server-inspector
```

普通 cron 不会补执行关机期间错过的任务。不要把同一任务重复添加到 `crontab -e`。

## 9. 自动化测试

```bash
/opt/linux-server-inspector/.venv/bin/python -m unittest discover \
  -s /opt/linux-server-inspector/tests -p 'test_*.py' -v
echo "测试退出码：$?"
```

通知测试使用临时状态文件和模拟 HTTPS，不连接真实钉钉。覆盖范围包括开关关闭、无 FAIL、首次告警、重复抑制、间隔后提醒、故障再次出现、业务失败、超时、状态文件异常、脱敏、dry-run、test-send 和清理行为。

## 10. 维护边界

- 修改代码、配置或文档：先在 Windows VSCode 完成，再用 Xftp 覆盖 Linux。
- 更新多个关联代码文件前：暂停本项目 cron，等待 `.server_check.lock` 和 `.cleanup.lock` 释放。
- 不停止整台主机的 crond，不删除锁文件，不自动改实际 cron。
- 不上传 Windows `.venv`、真实凭据、`runtime/`、日志和报告。
- 更换 Linux 发行版、解释器或依赖后，重新执行测试、Shell 语法检查和手动巡检。
- 目标 Linux 验证结果不能由 Windows 本地运行结果替代。

部署、暂停、凭据和恢复调度的具体步骤见 [`docs/deployment.md`](docs/deployment.md)；模块调用和数据结构见 [`docs/architecture.md`](docs/architecture.md)。

## 11. 当前限制

- 只检查本机，不提供多主机集中管理。
- 定时采样不是连续监控。
- 不发送恢复通知。
- 不保证网络超时情况下严格只送达一次。
- 当前只分析 TCP，不验证外部可达性，也不分析 UDP。
- 不审查全部防火墙规则，不验证 SSH `Match` 条件。
- 不自动修复检查发现的问题。
