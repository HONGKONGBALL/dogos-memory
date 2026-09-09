# DogOS 记忆数据库设计 v1

状态：已实现、可执行、有自动化测试。数据库：SQLite；schema 版本：`PRAGMA user_version = 1`。

## 1. 数据边界

每份文件只属于一只狗：`dog_a.db` 保存 A 看到/确认的经历和 A 对 B 的关系；`dog_b.db` 反之。
`MemoryStore(path, owner_id)` 每次操作检查文件中的身份，防止 A 的读取接口误用 B 文件。
这是应用层逻辑隔离，不是操作系统权限隔离；管理员仍能访问本机两个文件。

同一文件只属于一个实验模式：

- `live`：原始事件/对话只能来自 `robot_feedback` 或 `operator`。
- `simulation`：原始事件/对话只能来自 `simulation`。
- 两种模式的 `thought` 可来自 `llm` 或 `operator`，但其证据必须存在于当前文件。

## 2. 表结构

### profile：当前狗的固定身份

| 字段 | 类型 | 约束/含义 |
|---|---|---|
| `singleton` | INTEGER | 主键，固定 1，保证最多一行 |
| `dog_id` | TEXT | 非空、唯一，例如 `dog_a` |
| `name` | TEXT | 非空，展示名 |
| `personality` | TEXT | 固定性格描述 |
| `familiar_threshold` | INTEGER | 1–100；好感达到此值才算熟悉 |
| `mode` | TEXT | `live` / `simulation` |

实验期间不更新/删除 profile。修改性格、阈值或模式要使用新实验目录。

### memories：统一记忆流

| 字段 | 类型 | 约束/含义 |
|---|---|---|
| `memory_id` | TEXT | 主键、非空；**幂等键**，作用域为当前数据库 |
| `session_id` | TEXT | 互动会话 ID；一轮可包含多个不同事件 |
| `peer_id` | TEXT | 关于谁的记忆，不得等于当前狗 |
| `kind` | TEXT | `event` / `chat` / `thought` |
| `event_type` | TEXT | 规则层事件名，如 `greeting_completed`、`utterance`、`impression` |
| `content` | TEXT | 文字描述；对话可以直接保留一段文本 |
| `occurred_at_ms` | INTEGER | 发生/形成时间，UTC Unix 毫秒；检索按此排序 |
| `recorded_at_ms` | INTEGER | 系统实际写入时间，UTC Unix 毫秒；重放不更新 |
| `source` | TEXT | `robot_feedback` / `operator` / `simulation` / `llm` |
| `result` | TEXT | `completed` / `failed` / `unconfirmed` / `derived` |
| `importance` | INTEGER | 1–10，默认 5；保留设计接口，**当前不用于排序** |
| `affinity_delta` | INTEGER | 0–100，默认 0；仅已完成的 event 可非零 |
| `evidence_anchor_id` | TEXT/NULL | 自引用外键；thought 必须有首条证据，其他类型必须 NULL |

`evidence_anchor_id` 是数据库内部字段；API 用 `evidence_ids` 数组表示全部证据。保留首条证据字段，是为了让“每个 thought 至少有一条证据”在 SQL 层也能强制执行。

暂不增加：嵌入向量、最后访问时间、过期时间、记忆深度、多级压缩、关键词强度、任意大 JSON 扩展字段。

### memory_evidence：印象的证据集合

| 字段 | 类型 | 约束/含义 |
|---|---|---|
| `thought_id` | TEXT | 外键 → `memories.memory_id`，必须为 thought |
| `evidence_id` | TEXT | 外键 → `memories.memory_id`，必须为完成的 event/chat |

联合主键 `(thought_id, evidence_id)`；不允许自引用。
证据必须与印象指向同一 `peer_id`，发生时间不能晚于印象形成时间。
创建 thought 时触发器自动建立首条证据关联，应用事务再写其他证据。
任何证据不合法，整次写入回滚。当前不支持“印象引用印象”的递归反思。

这能保证引用结构正确，**不能自动证明模型的主观结论符合原文含义**；使用时仍需把印象标为派生内容。

### relationships：当前狗的单向关系

| 字段 | 类型 | 约束/含义 |
|---|---|---|
| `peer_id` | TEXT | 主键，关系对象 |
| `affinity` | INTEGER | 0–100，未建立关系时接口返回 0 |
| `updated_at_ms` | INTEGER | 最近计分事件的发生时间；延迟事件不会让该时间倒退 |

`relationships` 是计分事件的汇总状态。应用不提供直接改分 API。
正增量事件由 SQL 触发器执行 `min(100, 原值 + 增量)`；事件插入和关系更新属于同一事务。
`affinity_delta` 保存规则提出的增量；达到 100 后实际增加量可能更小。
当前没有负分、衰减或删除历史后的重算需求。

## 3. 写入约束与事务

写入路径：解析输入 → `BEGIN IMMEDIATE` → 验证 schema/owner → 检查 ID → 写记忆及证据 → 触发关系更新 → `COMMIT`。

1. 相同 ID + 完全相同业务载荷：返回 `inserted=false`，不再更新分数。
2. 相同 ID + 不同载荷：返回 `idempotency_conflict`，保留原文。
3. 文本相同但 ID 不同：视为不同互动，保留两条。
4. thought：`result=derived`、增量 0、至少一条有效证据。
5. event/chat：不能以 `llm` 作为实际观察来源；chat 不计分。
6. failed/unconfirmed：增量必须 0，保留审计记录，不作为成功经历返回。
7. memories、memory_evidence 只追加；禁止 UPDATE/DELETE。profile 固定。

幂等依赖控制层复用稳定 ID，数据库无法辨别“换了 ID 的同一物理动作”。推荐：`会话ID/动作ID/completed`。
不要先用该 ID 写入 `unconfirmed`，再尝试覆盖为 `completed`。如需保存中间过程，使用独立的审计事件 ID；后来真实确认完成时，新增稳定的完成事件 ID。

SQLite 每连接启用 `foreign_keys=ON`，持久文件启用 WAL，使用 FULL 同步和 5 秒锁等待。
普通连接采用 `mode=rw`，路径错误时直接报错，避免悄悄创建空库冒充“失忆”。
新建/重开实验时拒绝未知 schema 和不一致 profile；没有清空数据库或自动覆盖迁移的入口。

## 4. 检索规则

首版只做确定性检索：

```sql
SELECT memory_id, content, source, result, occurred_at_ms
FROM memories
WHERE peer_id = :peer_id
  AND kind IN ('event', 'chat')
  AND result = 'completed'
ORDER BY occurred_at_ms DESC, memory_id DESC
LIMIT :limit;
```

thought 独立查询，返回其证据 ID。一次 `recall` 在同一读事务中取得身份、关系、经历、印象，避免并发写入导致半新半旧的上下文。
默认各取 3 条，上限各 20 条；同毫秒以 memory_id 降序打破平局，不依赖网络到达顺序。
索引：`(peer_id, occurred_at_ms DESC, memory_id DESC)`、`session_id`、证据反向引用 `evidence_id`。

没有语义相似度检索，也没有复制上游的三因素排序；后续真正出现跨主题检索需求时再加。

## 5. 真机接入时必须补的工作

- controller 负责身份确认、事件 ID、经过验证的完成结果和规则增量。
- adapter 必须分辨“接收命令”和“完成动作”；不能因为 SDK 返回成功就写成物理完成。
- A/B 各自的事件来源与观察内容分别建立，不能把 B 的全部私有记忆复制给 A。
- 不把数据库写权限暴露给 LLM。`source` 字段本身不是认证机制。
- 当前两库事务相互独立；网络/进程失败后可按相同 ID 重试尚未写入的一侧，没有分布式事务承诺。
- 本模块不含持久命令队列，也不负责恢复未完成机器人动作。启动时只恢复记忆。
