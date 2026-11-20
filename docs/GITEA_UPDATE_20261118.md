# Gitea 更新

目标 origin/main 9c249d82，版本 0.136.0-dev.1。

1. 备份当前 main 1136a3ed。
2. 已验证 Gitea cddf7e3d 与已合入的 GitHub 03f45e9a 内容树完全相同，将该等价历史节点标记为已集成，随后正常合并 origin/main。
3. 保留本机端口 26546、workspace 依赖路径、重复启动保护、队列和 Claude API 错误处理。按真实增量解决冲突。
4. 构建前端，运行相关检查，本地提交。未授权推送或重启，不执行模型请求，不修改用户数据。

分类：MINOR / 新功能，合入 DeepSeek/Harness 等上游能力，沿用上游版本。

## 完成记录

- 已备份 backup/main-before-gitea-pull-20261118；从等价内容节点 cddf7e3d 合并最新 Gitea，保留 Windows 本地差异。
- 仅共享队列读取处存在冲突；保留 Codex prefer_runtime 修复，同时接入上游的多引擎分派。
- TypeScript/Vite 构建通过，11 项浮窗和流式工作区操作检查通过。
- 已在项目 .venv 安装 deepseek-harness-sdk 和其 Windows 原生运行依赖 0.2.2rc2。
- 首轮后端检查在依赖安装前运行，存在依赖缺失及测试加载路径问题，未计为通过。安装后定向复查仍有上游 Windows 测试兼容问题：POSIX 文件模式断言、端点测试未关闭新建的 deepseek-harness.sqlite3 导致临时目录清理 WinError 32；工作区测试还有 Windows 写入 CRLF 与断言预期不一致。未据此声称后端检查全通过。
- 未修改上游测试来掩盖失败，未发送真实模型请求、重启服务或推送。
