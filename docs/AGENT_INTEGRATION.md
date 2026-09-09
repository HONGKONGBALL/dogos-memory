# DogOS Agent 接入说明

这份仓库提供两个独立能力：

1. `dogos_memory`：每只狗一份 SQLite 记忆，提供幂等写入、按对象召回和关系分数。
2. `dogos_demo`：双狗身份门控、确定性两步策略、固定动作路由、完成门控和双库记账。

当前真实 Vbot 接入只覆盖只读硬件身份检查；默认动作执行器是
`SimulationAdapter`。队友接入 Agent 时不要把模拟回执描述成真实动作完成。

## 1. 安装和基线验证

```bash
uv sync --locked
uv run python -m pytest -q
uv run ruff check .
uv run basedpyright
```

要求 Python 3.10+。运行数据库、SSH 配置、设备 ID 和日志都应放在 `data/`，该目录不会提交。

## 2. Agent 读取记忆

Agent 应调用 `MemoryStore.recall()` 获取结构化上下文，不应直接执行 SQL：

```python
from pathlib import Path

from dogos_memory.models import DogId, RecallRequest
from dogos_memory.store import MemoryStore

store = MemoryStore(Path("data/live/dog_a.db"), DogId("dog_a"))
context = store.recall(RecallRequest(peer_id=DogId("dog_b"), limit=3))
```

`context` 包含 `profile`、`affinity`、`familiar`、已完成事实 `facts` 和有证据的印象
`thoughts`。建议只把这份结构化结果交给 LLM；动作选择、路由和关系加分仍由确定性层控制。

## 3. Agent 写入边界

- 已完成事件只能来自可信的 `robot_feedback` 或现场 `operator` 确认。
- 服务只返回 accepted/success 时不得写成 `completed`。
- LLM 只能生成 `thought`，并必须引用更早、属于同一对象的已完成事实 ID。
- 相同物理完成回调必须复用同一个 `memory_id`，防止重试重复加分。
- 不要让 Agent 获得数据库文件、通用 SQL 或不受限 `append` 的直接工具权限。

## 4. 接入队友的动作执行层

为每台狗各实现一个 `ActionAdapter`，并永久绑定不同的 `adapter_id`：

```python
class ActionAdapter(Protocol):
    @property
    def adapter_id(self) -> AdapterId: ...

    def execute(self, command: ActionCommand) -> ActionReceipt: ...
```

执行器只映射四个白名单语义动作：

- `outgoing_greeting`
- `cautious_reply`
- `familiar_greeting`
- `warm_reply`

回执必须原样返回 `command.command_id` 和绑定的 `adapter_id`。只有收到真实完成反馈后才返回
`ReceiptStatus.COMPLETED` 与 `CompletionSource.ROBOT_FEEDBACK`；只有受控演示中人工确认时才使用
`CompletionSource.OPERATOR`。普通服务 ACK 应返回 `SERVICE_ACKNOWLEDGED`，协调器会安全中止后续步骤。

## 5. 双机身份与连接

复制示例文件到忽略目录，再填写现场值：

```bash
cp examples/two_vbots.ssh_config.example data/two_vbots.ssh_config
cp examples/two_vbots.connections.json data/two_vbots.connections.json
```

两条连接必须具有不同 URL 和不同硬件 ID。启动隧道后运行：

```bash
uv run python -m dogos_demo connect-check --config data/two_vbots.connections.json
```

只有顶层 `state=ready` 才表示两条路线分别到达两台已绑定设备。`routing_conflict`、
`duplicate_device` 或 `degraded` 都不能进入真机动作联调。

## 6. 当前可运行演示

```bash
uv run python -m dogos_demo encounter \
  --directory ./data/rehearsal --session-id encounter-1 --observed-at-ms 1000
uv run python -m dogos_demo status --directory ./data/rehearsal
```

该命令使用模拟动作，但会运行真实的身份规则、会话编排和 SQLite 持久化。接入真实 Adapter
后，应新建 `mode="live"` 的配置和全新数据库目录，不能把模拟数据库升级成真机证据。
