# Vbot Native Agent 扩展

`native_agent` 是围绕 Vbot 原厂 `vbot-agent-harness` 构建的本地扩展层。目标是让每只狗只保留一个开放式规划 Agent，同时把用户身份、动作约束、社交记忆和验证流程做成可检查的边界。

当前代码已经包含插线部署、X5 重启/回滚和原厂本体语音启停流程，并在本地假设备与纯逻辑测试中通过。它尚未在真实 X5 执行，也没有接管或执行机械狗动作。

## 架构原则

- 原厂 harness 是每只狗唯一的 Agent 主脑。
- `soul.md` 提供身份和稳定人格；`user.md`、`memory.md` 提供用户事实和一般长期记忆。
- `AGENTS.md` 只描述机械狗身体的使用纪律，不固定名字或覆盖 SOUL。
- 模型选择高层工具，确定性策略根据电量、故障、传感器新鲜度、冷却和人工授权决定是否允许。
- 原厂 ROS 2 与 RCP/DAG 负责身体执行；模型不生成关节轨迹。
- DogOS 只保存有来源的社交经历。失败、仅收到 ack 或未经确认的动作不能增加关系。

完整产品阶段和验收 Gate 见 [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)。
本轮自动化证据见 [`VALIDATION-REPORT.md`](VALIDATION-REPORT.md)，真实设备停止线见 [`SECURITY-READINESS.md`](SECURITY-READINESS.md)。

## 模块

| 路径 | 当前职责 | 明确边界 |
|---|---|---|
| `identity/` | 构建、校验、封装、解包、应用和回滚身份包 | 只接受固定文件名与固定目标；不会传输到设备、运行 shell 或重启服务 |
| `runtime/` | 直连 Gate、部署 artifact、S100 管理、X5 安装/重启/回滚和本体语音启停 | 只允许固定服务与生成命令；不注册为 Agent/MCP 工具，不发动作 |
| `identity/templates/AGENTS.md` | 中性的具身运行规则 | 身份与名字仍由 SOUL 决定 |
| `policy/` | 纯函数动作策略，覆盖 3 种模式、10 类风险和 28 个原厂工具 | 没有 I/O、隐式时钟或 ROS 调用；尚未挂到原厂工具调用路径 |
| `dogos_adapter/` | 有上限的社交记忆读取，以及可信完成事件写入 | Agent 只应获得 recall；数据库路径和 owner 必须由宿主预绑定；不暴露 SQL 或通用 append |
| `dogos_mcp/` | 每只狗一个 DogOS MCP 端点，以及固定 peer 的文字 relay | 只绑定本机；身份与数据库由宿主固定；不含 ROS 或身体动作工具 |
| `evals/` | 从去敏 River JSONL 检查身份、未知问题、工具使用和禁用动作 | 只读离线日志；不会向 Agent 发问题，也不证明本体入口或实机已经通过 |
| `mcp_probe/` | 本机 Streamable HTTP MCP `ping` 探针 | 只绑定 `127.0.0.1`，无机器人、文件、shell 或网络抓取工具；不证明 Vbot 已支持注册 |
| `tests/` | 上述边界的本地回归测试 | 测试和假设备结果不是 X5、本体语音或身体动作证据 |

## 身份包

输入目录支持：

- 必需：`soul.md`
- 可选：`user.md`
- 可选：`memory.md`

构建器会加入仓库中的中性 `AGENTS.md`，保留输入 Markdown 的原始字节，并生成带 SHA-256、目标路径、dog ID 和 revision 的 `manifest.json`。文件必须是非空 UTF-8 普通文件，符号链接和未声明内容会被拒绝。当前总 payload 上限为 64 KiB。

主要 CLI：

```text
python3 -m native_agent.identity build
python3 -m native_agent.identity validate
python3 -m native_agent.identity pack
python3 -m native_agent.identity unpack
python3 -m native_agent.identity apply
python3 -m native_agent.identity rollback
```

`apply` 和 `rollback` 默认只生成计划；只有显式传入 `--execute` 才会修改 `--device-root`。真实部署由 `runtime` 包装：Mac 先验证直连与固定 SSH host key，S100 再绑定 dog ID、revision 和 capsule hash，X5 只执行固定 importer 命令。harness 通过重启健康检查后才把 `restart_required` 标为 false；失败会恢复精确基线。

Importer 会在备份和替换前写入持久 journal，并在下一次持锁操作前恢复未完成的 apply/rollback。校验、安装与暂存都使用已经读入内存的同一份快照，再检查源是否被替换。当前仍使用路径级检查；若真实 X5 上存在不可信的并发文件写入者，部署包装层还需要使用受限服务账号和设备侧目录权限消除父目录替换竞争。

## 动作策略

`policy.evaluate_action()` 是无副作用的失败关闭判断。调用方必须显式提供动作意图、机器人状态、策略和当前时间。三种模式是：

- `identity-only`：只验证身份和非身体能力。
- `supervised-demo`：身体动作需要短时 HMAC 批准；批准绑定工具、模式和完整参数，并继续检查其余前置条件。
- `autonomous-safe`：当前不允许身体动作；要等原厂参数 schema、可信参数校验器和不可旁路的工具 hook 接通后，才逐项开放表达行为。

未知工具、未知模式、缺失的必要状态、低电、充电冲突、fault、动作忙碌、状态过期、缺少人在场或冷却未结束都会产生机器可读的拒绝原因。相机采集和网络外发有独立风险类，也需要批准。批准密钥和已用 nonce 集合必须由模型不可访问的控制器持有。

批准只证明操作者批准了这一组精确参数；当前还没有每个原厂工具的参数语义 schema。该模块也尚未成为原厂动作工具的强制代理，因此目前不能声称机械狗已经具备这层硬拦截。

## DogOS 适配器

可信宿主先调用 `bind_social_memory(database_path, owner_id)`，再只把返回对象的 `recall(peer_id, limit)` 注册给 Agent。公开的模型工具签名不包含数据库路径和 owner，并按单个 `peer_id` 返回有上限的关系、事实和印象。

`record_confirmed_interaction()` 是控制器侧接口，只接受 `completed` 或 `confirmed`：

- `completed` 记录为 `robot_feedback`；
- `confirmed` 记录为 `operator`；
- `failed`、`acknowledged` 及其他状态在写入前被拒绝。

DogOS 是可选依赖并采用懒加载。模块可在未安装 DogOS 时导入，但实际读取或写入会返回 `dependency_unavailable`。适配器尚未部署到 S100/X5，也没有注册为原厂 Agent 工具。

## 快速本地验证

从仓库根目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider native_agent/tests
node native_agent/mcp_probe/smoke_test.mjs
node native_agent/dogos_mcp/smoke_test.mjs
```

MCP 测试使用仓库根目录 `package.json` 中声明的 MCP SDK 与 Zod。缺少依赖时，探针会明确
失败，不会自动安装软件。

从合成 SOUL 开始运行完整假设备流程，见 [`DEPLOYMENT-RUNBOOK.md`](DEPLOYMENT-RUNBOOK.md)。

## 插线启动

身份目录至少包含 `soul.md`。连接恢复后运行：

```sh
./scripts/start-native-agent.sh --identity-dir /ABS/PATH/TO/identity --dog-id datou
```

该命令使用狗本体 `/agent/enable`、`/speech_control` 和原厂唤醒链，不需要手机。安装阶段可以在充电时运行；激活阶段默认要求至少 40%、拔掉充电器、无 fault、机器静止且云网络可用。详见 [`PLUG-AND-RUN.md`](PLUG-AND-RUN.md)。

## 状态边界

### 已实现并在本机验证

- 身份 bundle 的 build、validate、pack、unpack。
- 对假设备目录的 dry-run、原子 apply、备份和 rollback。
- apply/rollback 各阶段的崩溃恢复、重复执行和错误 dog ID 拒绝。
- 确定性动作策略及失败关闭规则。
- DogOS 的有界适配接口。
- 去敏 River 日志评估器。
- 仅回环地址的 MCP `vbot_extension_ping` 协议探针。
- 两个独立 DogOS MCP 端点完成 A 发信、B 收件/确认和双库持久化。
- 稳定 release 构建、S100/X5 capsule、固定命令模板和直连路由检查。
- X5 harness 重启成功标记、失败自动回滚和重复安装幂等。
- 原厂 Agent/闲聊/命令词/连续命令状态的保存、激活、停用与恢复流程。

### 尚未执行或尚未接通

- 真实 X5 身份部署、服务重启与设备上的权限检查。
- `/agent/enable`、`/speech_control` 设置与狗本体唤醒/ASR/TTS 回合。
- 原厂自定义 MCP/hook 注册。
- 将策略层强制置于原厂 Agent 与动作工具之间。
- DogOS 在 S100/X5 的安装、持久化和 Agent recall。
- 原厂云 Agent 的 MCP URL 注册、本体语音入口评估和任何新的身体动作验收。

私有 WebSocket 曾用于诊断原厂 Agent，但它不是本项目的产品接口，本目录也不提供依赖该协议的部署命令。

最后一次实机读到的电量为 13% 且未充电。在安全充电、现场清空并完成实机 Gate 前，不执行身体动作。

## 数据与隐私

- 不把账号、密码、API key、token、设备证书或 `.env` 内容写入仓库、命令历史、测试 fixture 或报告。
- 不把私人 SOUL、用户事实或原始 River 日志提交到仓库。测试使用 `evals/fixtures/identity/` 中的合成身份。
- 真实评估只接受先行去敏的 River JSONL；评估输出也不得回显私人内容。
- 原厂云模型、TTS、River 日志和遥测的数据边界仍需在真实导入前单独确认。
