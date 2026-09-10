# DogOS 架构与状态边界

## 当前实现

```text
用户 / 原厂唤醒
        |
Vbot 原厂 Agent（每只狗一个）
        |  高层工具意图
        +--> 原厂 ROS / RCP / DAG --> 身体、头部、灯光、声音
        |
        +--> DogOS recall（固定 owner、固定 peer、有界、不可信历史）

受信电脑 CLI --> 身份包 / 设备 Gate / 回滚
受信控制器   --> 已确认完成事件 --> DogOS SQLite
```

`native_agent/policy` 目前实现确定性失败关闭判断，但还没有接入原厂 Agent 的不可旁路
工具 hook。因此它是安全设计和本地验证资产，不是已经在设备上生效的动作防护承诺。

## 模块职责

- `dogos_memory`：单狗数据库、关系值、事件/对话/印象和证据关联。
- `dogos_demo`：身份、双端点连接、模拟 Adapter、会话幂等和只读硬件身份门控。
- `native_agent/identity`：对 `soul.md` 等文件做严格校验、打包、暂存和事务回滚。
- `native_agent/runtime`：固定的 S100/X5 管理命令、状态 Gate 和 artifact 构建。
- `native_agent/dogos_adapter`：只暴露绑定后的 recall；完成事件写入留在受信控制器。
- `native_agent/dogos_mcp`：每只狗一个固定 owner/peer 的本机 MCP 端点，不能控制身体。
- `companion` 与 `demo_choreo`：确定性陪伴/舞台回退，不与原厂 Agent 并行充当规划者。

## 证据标签

本仓库刻意区分 `LOCAL PASS`、`SIMULATION`、`NOT_RUN` 和 `UNVERIFIED`。本地单元测试只能
证明代码路径和边界；不能证明真实 X5 权限、云服务数据流、身体动作效果或两只狗的原生
Agent 已经互相调用。
