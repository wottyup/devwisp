# Android DeepSeek 模式

## 需求与分类

网页版已提供 `deepseek-harness` 引擎，Android 原生客户端也应能选择该引擎、创建和恢复对话、发送消息及附件、查看流式回复，并处理引擎要求的权限审批和提问。沿用电脑端已保存的 DeepSeek API 配置，不在手机重复保存密钥。

属于新功能 / MINOR：用户首次能够从 App 使用 DeepSeek 对话。Android 目标版本从 `0.128.0` 升至 `0.129.0`，补丁号归零；网页版本独立保留。首轮验证包为 `0.129.0-dev.1` / `versionCode 467`，最终包为 `0.129.0-dev.2` / `versionCode 468`，分别保留，不覆盖历史安装包。

## 实施步骤

1. 核对网页与服务端 DeepSeek 的状态、对话、队列、权限、提问和设置协议，以及 Android 双引擎分支。
2. 接入三引擎选择、品牌图标、侧栏筛选、对话恢复和连接隔离；保证原 Codex、Claude 配置兼容。
3. 接入 DeepSeek Agent、权限及模型偏好，普通发送、定时发送、排队编辑与重发均保留相应参数；同步电脑端可用状态。
4. 支持 DeepSeek 权限与提问的恢复、呈现和响应，并接入现有消息、终端和文件能力。
5. 添加关键协议与状态回归测试，运行 Android 单元测试、构建和适用的现有回归检查；修复验证中发现的问题。
6. 更新 Android 说明和版本历史，生成独立命名的新 APK 到 `artifacts/`，记录验证结果，完成本地提交，不推送。

## 验收重点

- 三种模式可直接选择；DeepSeek 的图标、名称和筛选正确，不误用 Codex 参数。
- 重新打开 App、切换电脑连接和切换聊天后，模式、选中聊天及草稿正确恢复。
- DeepSeek 新建、发送、排队、定时、重发和继续执行使用服务端实际协议。
- 审批与提问按聊天隔离，断线恢复后仍可处理，已处理请求及时清除。
- 旧版服务缺少 DeepSeek 接口时，Codex 与 Claude 仍正常工作。
- 保留既有未跟踪用户文件和历史安装包，清理临时脚本及调试进程。

## 实施与验证结果

- 已完成三引擎选择、DeepSeek 官方鲸鱼矢量图标、侧栏筛选、空状态与完成提醒；连接级聊天恢复兼容旧版双模式偏好。
- DeepSeek 模型、思考、Agent 与权限偏好独立存储；发送时映射到共享队列的 `model` / `effort`、`deepseekAgentPreset` / `deepseekPermissionMode`。新建、附件、定时、排队编辑、重发和继续执行沿用现有服务接口。
- Agent 遵守服务端绑定结果，保留旧版 SDK 会话；Minimal 排除计划模式。思考等级使用电脑端 `reasoningEffortsByModel`，兼容第三方 API 模型。
- 权限复用服务端共享审批通道，标题按引擎显示；原生提问和计划审核支持单选、多选、自定义反馈、取消与提交，长内容独立滚动，操作按钮保持可见。
- 提问状态按聊天隔离，保存未提交答案，防止重复提交；HTTP 恢复使用状态修订号，避免过期快照复活已处理提问。网络失败保留答案，409 冲突通过重新同步确认请求状态。
- 状态定期刷新并响应配置变更；旧服务缺少 DeepSeek 接口时显示升级提示，原有引擎继续工作。补充新增界面的英文文案。
- 首轮 Android 单元测试 56 项通过（其中新增 18 项），零失败、零错误；`:app:assembleDebug` 通过。
- 最终 `:app:testDebugUnitTest :app:assembleDebug --offline --no-daemon -Pkotlin.compiler.execution.strategy=in-process` 通过；56 项测试，零失败、零错误、零跳过。`git diff --check` 通过。
- 最终安装包 `artifacts/devwisp-android-0.129.0-dev.2-debug.apk`，7,582,296 字节；`aapt` 验证 application ID 为 `com.sevencnup.codexremote`、版本号为 `0.129.0-dev.2`、`versionCode` 为 468。`apksigner verify` 通过，签名证书与 `0.128.0-dev.2` 旧包一致。
- 最终 APK SHA-256：`aeedbb843001eecd1330daf2126d39a9b4f969aa9e0abe8b4173d19d7a35d3c6`。保留首轮验证包与全部历史包，归档复制使用排他创建，避免覆盖。
- 当前没有已连接 Android 设备，未执行真机安装、截图或真实模型请求验证。
- 无新增依赖，无临时测试脚本；构建使用单次 Gradle 进程，临时启动的 ADB 服务已关闭。保留用户原有未跟踪文件。仅进行本地提交，不推送 GitHub。
