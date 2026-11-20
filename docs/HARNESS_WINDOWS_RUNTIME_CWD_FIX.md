# Windows Harness 启动目录修复

分类：修复。Windows 打包运行组件在 D 盘工作目录启动时，加载 D:/snapshot 下的 koffi.node 失败，错误被泛化为 API 配置问题。

1. 保持聊天 cwd，单独将 Windows runtime_cwd 设置为应用数据库目录。
2. 增加原生模块加载失败的固定提示，不输出原始凭据或请求。
3. 使用已保存配置进行隔离请求验证：原启动目录失败，分离启动目录后完成回复。
4. 更新补丁版本，中文提交并尝试推送 GitHub。
