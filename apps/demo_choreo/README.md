# 固定演示编排

这是一条独立的 hackathon 演示路径：固定触发映射到固定动作流程。dry-run 与受限 live driver 都已实现；当前设备正在充电且没有已认证会话，只运行了离线验证，尚未执行新的实机 routine。

```bash
./scripts/run-python.sh -m demo_choreo validate
./scripts/run-python.sh -m demo_choreo list
./scripts/run-python.sh -m demo_choreo list --persona xiaoman
./scripts/run-python.sh -m demo_choreo match '我回来了！' --persona xiaoman
./scripts/run-python.sh -m demo_choreo run xiaoman_owner_returns --battery 80 --operator-present
./scripts/run-python.sh -m demo_choreo.operator_console
./scripts/run-python.sh -m demo_choreo.operator_console --once 4
```

默认 `run` 的电量是 0 且操作员不在场，因此会被前置条件阻断。即使所有参数满足，结果仍是 `simulated`。

## 当前流程

同一个语句会按照当前狗的 persona 路由到不同流程。没有指定 persona 且存在两个匹配时，matcher 返回 `null`，不会自行猜测。

| 情境 | 小满 | 布丁 |
|---|---|---|
| 介绍自己 | `xiaoman_show_identity` | `buding_show_identity` |
| 主人回来 | `xiaoman_owner_returns` | `buding_owner_returns` |
| 主人疲惫 | `xiaoman_comfort_tired` | `buding_comfort_tired` |
| 两狗见面 | `xiaoman_meets_buding` | `buding_meets_xiaoman` |

角色规则与逐步动作见 [ROLE-MAPPING.md](ROLE-MAPPING.md)。

`native_dag` 同时记录原厂动作 `source_file` 和真实 `task_id`。两者并不总相同；当前 live driver 将它们明确标记为 `skipped`，不会发送。头部、表情、灯光和 TTS 继续完成角色表达。

触发词采用轻度标点/空格归一化后的精确匹配，不用模糊关键词，避免普通对话误触。当前可开跑版本以 Mac 舞台按键为实际触发；原厂 Agent/ASR 接入属于后续增强。

## 接实机后的首次确认

1. 每个 step 分别确认原厂资源名称和实物效果。
2. 将现场成功观察到的 `service_only` step 记录为 `physically_verified`。
3. 先用 `--once` 跑自我介绍，再进入完整按键控制。
4. 先单狗逐步验收，再排两狗时序。

live driver 已只实现固定白名单 action，不接受任意 ROS 服务或 DAG 名。它会在开场和每个实际输出前检查电量、充电、静止、故障与操作员在场；失败后不会继续后续 step。

它是确定性舞台编排，不单独证明 Agent 能自主规划。后续可以让原厂 Agent 在几个已验收 routine 中选择，但 routine 内部仍保持固定、可回放。

## 不用手机的舞台控制

`operator_console` 是 Mac 终端里的八键舞台控制器，不需要手机页面。默认命令做离线模拟；连接
设备需要按根目录 README 提供的受信 SSH 配对流程显式完成。数字键按场景成对排列：1/2 自我介绍、
3/4 主人回来、5/6 安慰主人、7/8 两狗见面；在终端中按一下即可触发，不需要回车。两狗见面启动时
显式增加 `--peer-present`，再先按 7 让布丁发起、按 8 让小满回应。

live driver 会在每个实际输出前重新检查 2 秒内的状态；按 `x` 可停止当前编排并清除短时表达输出，
但它不是整机急停。实机连接与现场验收目前仍是人工 Gate，不由这个仓库的离线演示代码自动完成。
