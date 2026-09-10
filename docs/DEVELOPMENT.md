# 开发与发布清单

## 本地

```bash
uv sync --locked
uv run pytest
npm install
npm run test:mcp
npm run test:dogos-mcp
```

Python 代码默认从仓库根目录导入。MCP 测试会启动临时回环服务并清理临时数据库，不需要
机器人、ROS 或云模型。

## 公开仓库前

- 不提交 `data/`、`.env`、运行数据库、日志、私人的 SOUL/USER/MEMORY 或 SSH 主机密钥。
- 不把第三方 AgenticROS、Node/Python 缓存和构建产物复制进本仓库。
- 使用合成身份和 `simulation` 数据做协议验证。
- 重新检查设备地址、主机密钥、身份 revision 和所有 live 命令的安全 Gate。
- 把服务确认、模型自报和历史录像与现场确认的身体效果分开记录。
