# Android DeepSeek 提问与回答一致性修复

## 需求与分类

用户反馈 DeepSeek 的选择题卡片与 Codex 不一致、顶部引擎名称缺少 Harness、提交选择题答案后没有新增用户回复。

本次按 Fix / PATCH 处理：对齐已有提问交互、引擎标识与回答展示，不新增用户任务。目标 Android 版本为 `0.129.2-dev.1`，`versionCode 472`；新安装包单独归档，保留历史包。

## 实施步骤

1. 对照 Codex、DeepSeek 的题目渲染、答案提交及历史消息链路，定位样式分叉和回复缺失原因。
2. 复用既有选择题视觉和交互规范，保留 DeepSeek 多选、自定义答案、计划审核、取消和失败重试能力，避免长题目被固定高度截断。
3. 将顶部 DeepSeek 引擎标识补全为 DeepSeek Harness，保持模型名称独立。
4. 服务端在接受答案前写入用户消息日志，并在 Harness 继续执行前广播该消息；Android 同时保留按连接、聊天隔离的已确认答案兼容记录，连接尚未升级的电脑端时也能显示、恢复，服务端出现同 ID 消息后自动去重。取消和失败不产生成功回复。
5. 使用针对消息一致性的回归测试和现有 Android 测试验证，构建并校验版本、签名和 APK 归档。
6. 在已授权手机上覆盖安装，避免远程操作用户正在编辑的聊天；记录验证范围，清理临时调试文件及进程，完成本地提交，不推送。

## 验证结果

### 实现

- Codex 与 DeepSeek 共用题目卡片、选项行、选中效果和发送按钮构建方法。DeepSeek 保留多选、自定义回答、审批与计划审核、取消和提交状态，移除黄色审批容器与固定 `360dp` 内层滚动区，长题目跟随聊天页面滚动。自定义答案编辑期间延迟消息重绘，防止键盘和草稿被刷新打断。
- 引擎标识统一为 `DeepSeek Harness`，模型名称仍由模型配置决定。
- 服务端先校验全部答案，按题目原顺序写入 `user-question-<questionId>` 用户消息，再确认提交；Harness 继续执行前广播现有 `userMessage` 事件。重复、无效、跨聊天和取消提交不写入答案消息，落盘失败时问题仍可重试。
- App 在提交成功后将兼容答案写入按连接和聊天划分的存储，恢复历史时按原回合合并，并在服务端出现相同消息 ID 后去重清理；删除聊天时同步清理。失败与取消不添加回复，不通过普通发送接口再次启动模型回合。

### 自动验证

- `./android/gradlew -p android :app:testDebugUnitTest :app:assembleDebug --offline --no-daemon -Pkotlin.compiler.execution.strategy=in-process` 通过，59 项单元测试全部通过，无失败、错误或跳过；仅有既存编译警告。
- 新增回归覆盖多问题、多选与自定义回答组合、答案顺序、取消、重载恢复、服务端回显去重和部分历史不串回合；原有失败重试和聊天隔离测试继续通过。
- `PYTHONPATH=server:server/tests .venv/bin/python -m unittest test_harness_interactions test_deepseek_harness.DeepSeekHarnessTests test_deepseek_harness.HarnessApiTests`：23 项通过。
- `PYTHONPATH=server:server/tests .venv/bin/python -m unittest test_deepseek_harness.RealHarnessTests.test_native_questions_plan_and_cancel`：1 项通过，使用本地模型夹具及真实 Harness SDK 验证提问和计划审核继续执行。
- 服务端新增回归验证确认前持久化、广播顺序、重复拒绝、重新打开数据库恢复，以及落盘失败后仍可重试。`git diff --check` 通过。

### 安装包与设备

- 独立归档：`artifacts/devwisp-android-0.129.2-dev.1-debug.apk`，包名 `com.sevencnup.codexremote`，`versionName=0.129.2-dev.1`、`versionCode=472`。
- `aapt` 元数据和 `apksigner verify` 校验通过，签名与上一版一致；SHA256 为 `2f567dcb3772eb2b9ac574fdedb9b5828c3466f330cd237b74d005e1dd7e4c38`。历史包保留。
- 已在 `10.126.126.2:5555`（PJZ110）覆盖安装成功，安装前后偏好文件哈希一致，配置及已保存草稿保留。
- 真机仅检查安装、启动和进程状态；不在用户真实聊天中代选或发送测试答案。用户截图所示选择题的完整真机提交流程未自动操作，功能与恢复逻辑通过上述回归验证。
- 未重启当前正在提供服务的电脑端进程；新 App 的兼容记录可直接适配旧电脑端，服务端持久化修复随电脑端服务下次加载生效。
- 本次没有新增依赖、持久临时脚本或后台测试服务；收尾断开 ADB 并停止本次启动的 ADB 服务，保留应用运行。
