# DogOS：双 Vbot 轻量记忆与交互闭环

已实现的 Python + SQLite 原型，包含独立记忆模块与一个有边界的双狗会话调度器，
借鉴 Generative Agents 的 memory stream。不需要启动 AI 小镇、ROS、模型服务或向量数据库。

首版软件目标：**确认身份 → 记忆决定两步动作 → 完成后分别记账 → 程序退出 →
重启后因历史改变发起者。** 当前动作由模拟 Adapter 完成；已增加双真机的只读连接、
硬件身份核对和防串路由门控。现场设备 ID、SSH 配置和运行数据库只保存在忽略的 `data/`
目录中，不随仓库发布；这也不代表真机动作已经验收。

若要接入队友的 Agent、控制层或 UI，请先看 [Agent 接入说明](docs/AGENT_INTEGRATION.md)。

## 已确定的设计

每只狗一个数据库文件，分别保存自己的视角。首版采用四张表：

| 表 | 作用 |
|---|---|
| `profile` | 单一狗身份、固定性格、熟悉阈值、真机/模拟模式 |
| `memories` | `event` 事件、`chat` 对话、`thought` 印象，保留来源与结果 |
| `memory_evidence` | 印象所依据的原始事件/对话 ID |
| `relationships` | 当前狗对某个对象的好感度，范围 0–100 |

相对此前 v2 方案：`events` 扩充为 `memories`，仅增加一张证据关联表；不恢复六层记忆系统。

完整字段与约束：[SQL 设计](docs/SQL设计.md)。可直接执行的建表文件：[schema.sql](dogos_memory/schema.sql)。
上游版本、源码路径与保留/裁剪映射：[来源记录](docs/上游来源与取舍.md)。

## 两台 Vbot 先连起来（只读）

首版不是让两只狗互相桥接整套 ROS 图，而是让同一台电脑分别连接两只狗：

```text
Vbot A 127.0.0.1:9092 ← SSH 隧道 → 电脑 127.0.0.1:19091 ┐
                                                               ├→ DogOS 配对门控
Vbot B 127.0.0.1:9092 ← SSH 隧道 → 电脑 127.0.0.1:19092 ┘
```

每台狗运行独立的 9092 身份桥，只接受无参数的 `/datou/identity` 查询。桥启动时读取设备树
序列号和 `eth2` MAC，生成 `vbot-` 加 16 位 SHA-256 摘要的稳定硬件 ID；同时以
`operation=0` 读取 `/x5/efuse`，但 PN 仅作诊断，因为已测设备返回全 0。两个端点均在线、
硬件 ID 与固定配置分别一致且彼此不同，状态才是 `ready`。该端口不会调用动作、导航、
语音或写 eFuse，也不替代密码、证书或加密认证。SSH 使用 keepalive，隧道断开后脚本会
重连；每次正式 encounter 前重新运行一次 `connect-check`。

1. 两台狗先接入同一可达网络并取得两个**不同 IP**。若两台仍使用相同出厂 IP，先由厂家
   支持的方式改为不同地址或使用两条可明确绑定的独立网络路线，不能把两个本地端口都转发
   到同一个地址后假定它们是两只狗。
2. 准备 SSH 别名：

   ```bash
   mkdir -p data
   cp examples/two_vbots.ssh_config.example data/two_vbots.ssh_config
   # 编辑 HostName，分别填 dog A 与 dog B 的真实 IP
   ```

3. 把专用只读身份桥部署到两台狗。它使用独立目录和 9092 端口，不覆盖已有的
   `/userdata/vbot/agenticros-adapter/bridge.py` 或 9091 控制桥：

   ```bash
   for HOST_ALIAS in vbot-a vbot-b; do
     ssh -F data/two_vbots.ssh_config "$HOST_ALIAS" \
       'mkdir -p /userdata/vbot/dogos-identity/release-identity-v1/dogos_demo'
     scp -F data/two_vbots.ssh_config robot_side/vbot_identity_bridge.py \
       "$HOST_ALIAS:/userdata/vbot/dogos-identity/release-identity-v1/"
     scp -F data/two_vbots.ssh_config \
       dogos_demo/__init__.py dogos_demo/vbot_identity_core.py \
       dogos_demo/vbot_identity_protocol.py \
       "$HOST_ALIAS:/userdata/vbot/dogos-identity/release-identity-v1/dogos_demo/"
     ssh -F data/two_vbots.ssh_config "$HOST_ALIAS" \
       'cd /userdata/vbot/dogos-identity && chmod 755 release-identity-v1/vbot_identity_bridge.py && ln -sfn release-identity-v1 current'
   done
   ```

4. 开两个终端，分别保持一条固定隧道：

   ```bash
   bash scripts/connect-vbot-tunnel.sh \
     --ssh-config data/two_vbots.ssh_config --host vbot-a --local-port 19091

   bash scripts/connect-vbot-tunnel.sh \
     --ssh-config data/two_vbots.ssh_config --host vbot-b --local-port 19092
   ```

5. 复制连接配置并做第一次探测：

   ```bash
   cp examples/two_vbots.connections.json data/two_vbots.connections.json
   uv run python -m dogos_demo connect-check --config data/two_vbots.connections.json
   ```

   模板硬件 ID 是占位值，因此第一次会显示每条路线真实读到的 `observed_robot_id`，并返回
   `routing_conflict`。按 `vbot-a → dog_a`、`vbot-b → dog_b` 写回两个
   `expected_robot_id` 后重跑；只有退出码 `0` 且顶层 `state=ready` 才可进入后续真机动作
   联调。退出码 `3` 表示离线、协议错误、接反或两个隧道指向同一机身；退出码 `2` 表示
   配置/文件错误。

现场曾完成单台 Vbot 的 `19091 → SSH → 127.0.0.1:9092` 只读身份握手；真实硬件 ID、
诊断值和第二台接入状态不会写入公开源码。测试过程没有发送运动、导航或语音指令，原
9091 控制桥不会被身份桥覆盖。

## 三轮双狗软件演示

需要 Python 3.10+ 和 uv。ZIP 不包含虚拟环境；首次运行 `uv sync` 需要联网安装锁定的依赖。
在 `dogos-memory` 目录中运行。每条 `encounter` 都是一个独立进程；前两轮把好感写到
70/50，`status` 证明新进程能恢复，第三轮会由 A 主动发起熟人问候：

```bash
cd dogos-memory
uv sync --locked
uv run python -m dogos_demo encounter \
  --directory ./data/live-rehearsal --session-id encounter-1 --observed-at-ms 1000
uv run python -m dogos_demo encounter \
  --directory ./data/live-rehearsal --session-id encounter-2 --observed-at-ms 3000
uv run python -m dogos_demo status --directory ./data/live-rehearsal
uv run python -m dogos_demo encounter \
  --directory ./data/live-rehearsal --session-id encounter-3 --observed-at-ms 5000
```

输出是可供 UI 直接读取的 JSON，其中包括：身份来源、发起者、两条固定路由、动作名、
完成来源、是否为重放、两库分别写入结果、动作前后好感及引用的记忆 ID。
这是明确标记为 `simulation` 的闭环；`outgoing_greeting` 等名称只是语义白名单，尚未映射
到 Vbot 的头部/声音参数。

旧的纯记忆示例仍可运行：

```bash
uv run python -m dogos_memory demo-seed --directory ./data/demo
uv run python -m dogos_memory demo-recall --directory ./data/demo
```

`demo-seed` 写入两轮**模拟互动**并退出；`demo-recall` 启动全新的进程，只读回数据库，不初始化、不预填事件。
预期输出：`mode=simulation`，A 对 B 的好感为 70，B 对 A 为 50，两者 `familiar=true`。
每只狗各有两条事件、一条人工编写且有证据的模拟印象。这里没有调用 LLM，也没有操作真机。

重复执行 `demo-seed` 不重复计分。想做新实验，换一个新目录；原数据库不会清空。

已在本机 Python 3.12 验证；身份桥另已在一台 Vbot 的 ROS 2 Humble 环境实测。SQLite
记忆模块尚未部署到两台 Vbot。SQLite 需要 JSON 函数与 UPSERT 支持；标准现代 Python
SQLite 构建通常具备，部署时仍应运行本项目测试。

## 压缩包内容

```text
dogos-memory/
├── README.md          # 本说明与接入示例
├── pyproject.toml     # 项目和依赖配置
├── uv.lock            # 依赖锁定文件
├── dogos_memory/      # Python 存储模块、CLI、schema.sql
├── dogos_demo/        # 身份门控、规则、双狗调度、完成门控与安全 Adapter
├── examples/          # 模拟输入、双连接配置和 SSH 别名模板
├── scripts/           # 单狗固定 SSH 隧道与自动重连
├── tests/             # 自动化测试
└── docs/              # SQL 设计、上游来源与取舍
```

没有打包虚拟环境、缓存、运行时数据库或上游小镇仓库；示例数据库由上述命令生成。
软件会话已接入 `MemoryStore`；双连接的只读握手与门控已接入，现场双机握手、真实动作
Adapter、物理完成反馈和网页操作台仍未验收/接入。

## 用 JSON 单独操作一只狗

以下样例全部标记为模拟数据，不用于证明真机互动：

```bash
uv run python -m dogos_memory init \
  --database ./data/example/dog_a.db --profile-file ./examples/dog_a.simulation.json

uv run python -m dogos_memory record \
  --database ./data/example/dog_a.db --owner dog_a --input-file ./examples/greeting.simulation.json

uv run python -m dogos_memory recall \
  --database ./data/example/dog_a.db --owner dog_a --peer dog_b
```

`record` 只存储记录，不会执行记录中描述的动作。把未经确认的内容标为 `completed` 并不能让它成为真实反馈。

## Python 接入点

```python
from pathlib import Path
from dogos_memory.models import DogId, MemoryInput, Profile, RecallRequest
from dogos_memory.store import MemoryStore

# 新的真机实验用独立目录；初始化后身份、模式和阈值固定。
memory = MemoryStore.initialize(
    Path("data/live-01/dog_a.db"),
    Profile(
        dog_id=DogId("dog_a"),
        name="A",
        personality="谨慎",
        familiar_threshold=60,
        mode="live",
    ),
)

# controller_json 必须由可信控制程序构造：已核实对象、动作结果及来源。
# 输入格式见 SQL 设计与 JSON 样例；真机模式拒绝 simulation 来源。
# result = memory.append(MemoryInput.model_validate_json(controller_json))

context = memory.recall(RecallRequest(peer_id=DogId("dog_b"), limit=3))
# context.facts 是确认完成的经历，context.thoughts 是有证据的主观印象。
# 把 context.model_dump_json() 交给表达层，或直接使用 context.familiar 做规则决策。
```

最少四个 API：

- `MemoryStore.initialize(path, profile)`：创建或按完全一致的身份配置恢复。
- `append(memory)`：事务写入，返回是否新插入及当前好感度。
- `get(memory_id)`：按 ID 查看完整记录，包括失败和未确认结果。
- `recall(request)`：按对象取最近经历与印象，各最多 `limit` 条，默认 3、上限 20。
- `list_session(session_id)`：为会话幂等恢复读取该 ID 的全部记录，不执行旧动作。

所有 ID 必须稳定。例如 `session_id/action_id/completed`；同一完成回调重试时复用同一 `memory_id`。
相同 ID + 相同载荷是幂等重放；相同 ID + 不同载荷返回 `idempotency_conflict`。
同一句话在不同互动中发生，应使用不同 ID，都会保存。

## 重要边界

- `dog_id` 只能使用安全的短标识；`session_id` 最长 100 字符，只允许字母、数字、点、
  下划线和连字符，避免数据库路径越界与稳定 ID 解析歧义。
- AprilTag 使用配置中的 `family + integer id → dog_id` 固定映射，不是哈希。软件门控已经
  拒绝未知/自身 Tag、低置信度、左右目冲突、乱序帧和超时帧，并要求连续 3 帧；尚无
  Vbot ROS 订阅器，默认 family/ID 只是测试值。
- 两个 Adapter ID 必须唯一并与两只狗固定绑定；回执中的 `command_id` 或 `adapter_id`
  不一致时整轮中止。软件已验证路由，尚未验证两台真机会不会串控。
- 双连接配置还强制 `dog_id`、Adapter ID、WebSocket URL 和预期硬件 ID 全部唯一。
  可达但硬件 ID 不符返回 `routing_conflict`，两条路线读到同一硬件 ID 返回
  `duplicate_device`；
  任一情况都不能进入动作联调。
- 每轮最多两步并串行执行。服务只返回 accepted/success 时状态是
  `service_acknowledged`，立即停止后续步骤，不写 completed 记忆、不加好感。
- 每个已确认动作会用同一 `memory_id` 写入 A/B 两库：执行者记录自身行为且增量为 0，
  接收者记录对方行为并按自身规则增加好感。单侧失败后重放同一 session，只补缺失侧，
  不再次调用已经完成的 Adapter。
- 已完成的旧 session 即使在熟悉度改变后重放，也从数据库恢复原发起者，不按当前关系
  重算成另一套动作。所有 session 仍必须由单个协调器串行处理；本版没有跨进程锁。
- `StopToken` 只在每次派发前检查，不能中断正在阻塞的 Adapter；实际 Adapter 必须自行
  设置超时并调用厂家停止能力。
- 进程若在真实物理完成后、第一份 completed 记忆落库前崩溃，没有 durable outbox 可
  自动判定动作是否已经发生。恢复前必须人工核对，不得盲目重放该 session。
- 好感增量由可信规则层提供；模型不应持有通用 `append`/SQL 写权限。对模型只暴露读取结果与受限的印象写入入口。
- `robot_feedback` 只用于已经确认完成的真实反馈；如果服务仅表示接收请求，不能当作完成。人工确认必须标记 `operator`。
- 来源字段是追踪标签，不是身份认证。当前硬件 ID 是设备树序列号与网卡 MAC 的确定性摘要；
  eFuse PN 仅作诊断。二者都不是密码、证书或抗伪造认证；本版没有登录、mTLS 或密钥轮换。
- 失败/未确认记录可以存储，但增量必须为 0，不进入默认成功经历检索。
- 记忆表只追加，不覆盖事实。此模块不是命令队列，重启不执行任何旧动作。
- 仅读写两份数据库；不承诺两只狗的写入跨库同时提交。当前调度器会保留成功侧并用
  相同 ID 补写失败侧。
- 尚未实现真实 ROS adapter、语音播放、网页双狗台、LLM 推理、自动反思、向量检索、
  旧版小镇 JSON 导入。

## 验证

```bash
uv run python -m pytest -q
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
```

当前共 81 项测试。测试使用真实临时 SQLite 文件和本地 WebSocket 服务，覆盖持久化、幂等、并发重复回调、
跨狗隔离、证据追溯、事务失败回滚、身份门控、固定路由、完成门控、单侧补写、旧会话
恢复、独立进程三轮演示、双端点并行探测、硬件 ID 接反/重复拒绝、WebSocket 协议错误和
隧道脚本。另已完成一台真机的只读身份 E2E，尚不能替代双机 `ready` 验收。
`uv.lock` 固定依赖；生产依赖为 Pydantic、Typer、typing-extensions 与 WebSockets，
没有导入上游小镇的依赖列表。
