# 仓库目录约定

DogOS 按运行职责分层，避免把可复用库、可执行应用和设备部署代码混在仓库根目录。

```text
packages/       可导入的核心能力；保持 Python 模块名稳定
apps/           直接运行的本地体验和演示应用
integrations/   复制到 Vbot 或其他外部运行时的最小桥接层
scripts/        从仓库根目录运行的运维与开发命令
tests/          跨包核心行为测试；模块测试与所属模块同目录
examples/       合成样例与占位配置
docs/           面向贡献者的设计和操作说明
```

## 入口规则

- Python CLI：使用 `./scripts/run-python.sh -m dogos_demo ...`，不要依赖当前工作目录的隐式
  import 路径。
- Node MCP：使用根目录的 `npm run test:mcp`、`npm run test:dogos-mcp`，依赖只声明在根
  `package.json`。
- 实机脚本只接受显式设备地址和已验证的 SSH host key；`integrations/` 不是本机控制入口。

## 放置新代码

- 可被多个流程复用、且不直接启动 UI/服务的 Python 代码放在 `packages/`。
- 具有自己的服务、控制台或演示生命周期的代码放在 `apps/`。
- 对 ROS、设备文件系统或上游 Agent 运行时的最小适配放在 `integrations/`；不要把上游源码、
  构建产物、运行数据或凭据复制进来。
- 新测试优先与对应模块相邻；仅跨模块契约放在根 `tests/`。

这个结构不改变 `dogos_memory`、`dogos_demo` 或 `native_agent` 的 Python import 名称，减少
下游集成迁移成本。
