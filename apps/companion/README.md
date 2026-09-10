# 大头陪伴循环 · 第一版

> 2026-09-09 更新：实机模式已改为真实话题输入，以下早期“人工 live 输入/仅灯光”说明已被本节替代。
> `./scripts/run-python.sh apps/companion/server.py --live --enable-head --port 8767` 使用原生电池、上下文、人物姿态和触摸订阅。
> 实机需 `python3 -m pip install -r apps/companion/requirements-live.txt`。
> `--enable-head` 启用已预检查的小角度头部请求；物理转动尚未确认。默认声音关闭，`--enable-sound` 可选择短音效，实际可听性仍待验证。
> 手工 observation/respond 在实机模式返回 409；网页显示真实数据来源、年龄、UWB 与地图就绪条件。
> 摸头原生汪汪是厂商已有能力；当前订阅到的 touch_node 事件尚未证明对应机头触摸。
> 当前 19 项测试通过；跟随/召回/散步只开放预检查，UWB state=0、地图名为空，未进行实机运动。
> 详见 `../INTEGRATION-RESULTS.md` 与 `../INTEGRATION-PLAN.md`，旧版 ZIP 不含本轮修改。

独立运行在 Mac 的 Python 标准库服务，无额外依赖。默认模拟，不连接机械狗。

```sh
cd /path/to/dogos
./scripts/run-python.sh apps/companion/server.py
```

打开 http://127.0.0.1:8766 。提交环境场景，启动陪伴；之后无需点击动作，策略持续运行。
页面默认每 5 秒续报模拟场景。12 秒没有回应则进入安静状态，至少 120 秒后再邀请；连续忽略进一步延长至最多 600 秒。
邀请期间点“模拟回应 / 抚摸”会触发一次回应，10 秒后安静下来。忙碌、充电、低电量或 30 秒没有更新的输入会阻止行为。
按钮模拟传感输入，不代表已接入摄像头、人脸身份识别或触摸传感器。

## 结构和实现边界

- `engine.py`：可注入时间的状态机；关注、邀请、互动、安静、暂停、故障；全局阻断条件优先。
- `server.py`：定时循环、事件入口、有限队列、状态记录、原子写入互动次数和忽略次数。
- `adapter.py`：复用现有 AgenticROS MCP 调用，当前仅映射 1.5 秒原厂灯光；模拟执行不创建网络或子进程。
- `index.html`：运行状态、人工场景输入和动作结果，区分模拟和实机模式。

重启默认暂停；不会恢复旧的在场信息或继续之前的动作。接口失败锁定故障，无自动重试。
暂停清除排队行为，已发出的灯光可能持续到 1.5 秒结束；不是机械狗急停。
当前没有安装 py_trees、PiDog 或 Reachy；按之前讨论独立实现最小状态循环。后续行为复杂后可迁移至行为树。

## 初版实机适配记录（历史，以上方更新为准）

待有线连接及现有 SSH/AgenticROS 隧道恢复后，可手动以
`./scripts/run-python.sh apps/companion/server.py --live` 启动。
默认 node 来自 PATH，可用 `--node /absolute/path/to/node` 指定。
实机模式不自动续报人工观察，仍需提交现场状态；当前没有自动读取电量/充电或视觉输入，不能作为无人值守模式。
蓝色短灯光代表邀请，绿色短灯光代表回应。使用已存在的 `datou_light`，不调用站立、行走、跟随或语音。
原生服务成功只记录 `service_acknowledged`，实物效果保留 `unverified`。

## 验证

```sh
uv run python -m unittest discover -s apps/companion/tests -v
```

下一阶段：接入带时间戳的真实状态和人物在场事件，核对原生表情资源并现场确认，然后扩展互动表现。
主人身份识别、自然语言理解、真实触摸输入、跟随和自主导航尚未接入。

## 设计参考

- https://github.com/rockywuest/pidog-embodiment — 状态与自主行为设计。
- https://github.com/pollen-robotics/reachy_mini_conversation_app — 待机和互动衔接。
- https://github.com/splintered-reality/py_trees_ros — 后续优先级与取消编排参考。

本目录没有复制上述项目源码。
