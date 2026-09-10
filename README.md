# DogOS：Vbot 机械狗 Agent 扩展

DogOS 是面向 Vbot 机械狗的本地原型仓库：用 SQLite 保存可审计的社交互动，用受限的
MCP 端点把两只狗的消息和记忆隔离开，并提供原厂 Agent 的身份导入、设备管理和安全 Gate。

当前架构是一只狗一个 Agent：Vbot 原厂 `vbot-agent-harness` 负责理解、规划和长期身份；
Vbot 原厂 ROS/RCP/DAG 负责身体执行；DogOS 只保存有来源的互动事件，不是第二个规划 Agent。
`native_agent` 的动作策略目前是本地安全模块，尚未成为原厂工具调用的不可旁路 hook。

## 仓库结构

```text
dogos_memory/      SQLite 记忆存储、模型和 CLI
dogos_demo/        双狗身份门控、配对调度和模拟闭环
native_agent/      身份包、设备管理、策略、DogOS 适配器和 MCP 端点
companion/         独立的确定性陪伴状态机原型
demo_choreo/       固定动作演示与离线回退路径
robot_side/        Vbot 侧只读身份桥
scripts/           本地验证、配对和受信设备管理脚本
tests/             DogOS 核心测试
```

第三方 AgenticROS 源码、构建目录、依赖缓存、运行数据库、实机日志、SSH 主机密钥和私人
身份文件不在仓库中。MCP 服务默认只绑定 `127.0.0.1`。

## 快速开始：DogOS 核心

需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --locked
uv run pytest
uv run python -m dogos_demo encounter \
  --directory ./data/live-rehearsal --session-id encounter-1 --observed-at-ms 1000
```

`encounter` 和示例数据库明确使用 `simulation` 模式，不会控制真实机器人。完整字段约束见
[`docs/SQL设计.md`](docs/SQL设计.md)，Agent 接入边界见
[`docs/AGENT_INTEGRATION.md`](docs/AGENT_INTEGRATION.md)。

## 本地验证：Native Agent 与 MCP

需要 Node.js 20+：

```bash
npm install
npm run test:mcp
npm run test:dogos-mcp
PYTHONDONTWRITEBYTECODE=1 uv run pytest native_agent/tests companion/tests demo_choreo/tests -q
```

MCP 双端点模拟：

```bash
./scripts/demo-dogos-interconnect.sh simulation
./native_agent/dogos_mcp/start_pair.sh
```

默认会在 `data/` 创建被忽略的 SQLite 文件。只有在两台实体设备完成盘点、路线和硬件身份
核对后，才可以考虑物理配对；脚本会拒绝占位配置、相同 HostName 和未通过的身份检查。

## 设备部署边界

一次性管理流程由电脑上的 `scripts/start-native-agent.sh` 负责。使用前必须显式设置设备
地址，并提供通过可信渠道获得的 SSH 主机密钥文件：

```bash
VBOT_HOST=<device-address> \
VBOT_SSH_KNOWN_HOSTS=/path/to/verified/known_hosts \
./scripts/start-native-agent.sh \
  --identity-dir /path/to/identity --dog-id dog_a
```

身份目录至少包含 `soul.md`，可选包含 `user.md` 和 `memory.md`。脚本会校验文件、生成 hash
绑定的部署包，并在设备侧执行固定的安装/回滚流程；不会把密码写入参数或仓库，也不会在
启动阶段发送身体动作。真实 X5 安装、原厂自定义 MCP 注册、策略 hook 强制接入和完整的
身体动作验收仍需按设备 Gate 逐项完成，不能用本地测试结果替代。

更多实现说明见 [`native_agent/README.md`](native_agent/README.md)。

## 状态说明

- 已实现：DogOS 持久化、幂等和身份门控；身份包和假设备 apply/rollback；固定-owner 的
  DogOS MCP；确定性策略；陪伴和演示回退的本地测试。
- 仍需实机验证：真实身份部署、原厂 Agent 的 MCP 注册、不可旁路动作策略、私人数据流、
  双狗原生 Agent 对话和任何新的身体动作。
- 模拟、服务 ack、测试 fixture 或历史设备日志都不等同于真实动作完成。
