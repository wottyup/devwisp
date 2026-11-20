# Android 引擎图标与消息配色修复

## 需求与分类

1. DeepSeek 鲸鱼在修复宽高比后视觉偏小，需与 Codex、Claude 的图标协调，保持原图比例。
2. 所有模式的 AI 回复名称左侧加入对应引擎图标，参考网页端的图标、名称、时间排列。
3. DeepSeek 用户气泡使用网页端浅蓝色，发送中的消息与历史消息保持一致。

分类为 Fix / PATCH：修正已有界面的视觉一致性，不改变用户可执行的任务。目标版本 `0.129.3-dev.1`，Android `versionCode 473`，历史 APK 保留。

## 实施步骤

1. 对照网页端引擎图标和 DeepSeek 用户消息颜色，以及 Android 的图标容器、消息头和两条用户气泡渲染路径。
2. 调整鲸鱼的等比显示尺寸，统一封装品牌图标；在各引擎回复及 DeepSeek 提问卡片的名称左侧显示图标，并保持名称、时间对齐。
3. 提取当前引擎的用户气泡颜色入口，按网页端浅蓝色及像素主题的对应色实现 DeepSeek 配色，应用到即时发送和历史回复。
4. 构建并运行适用的现有 Android 检查，校验 APK 版本、签名和独立归档；在已授权手机覆盖安装并验证启动，不远程操作用户真实会话。
5. 记录验证范围，清理本次临时资源和调试进程，本地提交，不推送。

## 验证结果

### 实现与颜色核对

- 顶部模式切换及侧栏引擎筛选为鲸鱼减少 `2dp` 内边距，其他引擎保持既有内边距；切换模式刷新时明确恢复对应尺寸，避免放大状态串到其他图标。鲸鱼 VectorDrawable 路径和等比 viewport 未改动。
- 新增共用 `assistantNameView`，所有模式的 AI 回复名称及 DeepSeek 提问名称均使用“图标＋名称”，后接既有时间与耗时。图标与名称同色、垂直居中；采用统一 `18dp` 槽位，Codex / Claude 图形为 `14dp`，横向鲸鱼为 `16dp`，避免名称起点随引擎变化。装饰图标不重复朗读引擎名称。
- 使用本机 Chromium Canvas 实测网页 CSS：`color-mix(in oklch, #4d6bfe 40%, #f5f7ff)` 转为 sRGB 是 `#ACC3FF`；像素主题 `color-mix(in oklch, #4d6bfe 24%, #253d59)` 对应 `#264B7E`。Android 直接使用相同颜色，前者配深色文字，像素主题配既有浅色文字。
- 历史消息和即时发送中的用户气泡均使用相同的引擎配色入口，因此 DeepSeek 的普通消息和选择题回答一致。DeepSeek 名称在暖色及自定义主题下也使用对应品牌蓝色。

### 构建与归档

- `./android/gradlew -p android :app:testDebugUnitTest :app:assembleDebug --offline --no-daemon -Pkotlin.compiler.execution.strategy=in-process` 成功，59 项现有单元测试通过，无失败、错误或跳过；未为本次可逆视觉调整添加实现镜像式测试。仅有既存编译警告。
- `git diff --check` 通过。`aapt` 校验包名 `com.sevencnup.codexremote`、`versionName=0.129.3-dev.1`、`versionCode=473`；`apksigner verify` 通过，签名证书与上一版一致。
- 新包独立归档到 `artifacts/devwisp-android-0.129.3-dev.1-debug.apk`，SHA256 为 `8d2e22690289118dbefa00b053879ddc2b631f469e2ab441ea1eb05de90bf2b8`，历史 APK 保留。

### 真机验证与范围

- 已在 `10.126.126.2:5555` 覆盖安装成功，设备版本为 `0.129.3-dev.1 / 473`，启动检查正常，应用进程存在。
- 截图测得顶部鲸鱼可见尺寸从约 `66 × 48` 到 `78 × 58` 像素，横纵同比放大；Claude 图形保持原尺寸。仅保存两个顶部模式图标的前后裁剪对比：`artifacts/devwisp-android-0.129.3-dev.1-brand-comparison.png`，左为旧版，右为新版。
- 安装前后的可见输入内容一致，当前 Codex 聊天保留。偏好文件哈希在应用运行期间发生变化，不据此声称文件字节完全一致。
- 安装后用户当前屏幕处于长回复中部，回复名称位于屏幕之外；未远程滚动、切换模式或发送消息。三个引擎回复头部与 DeepSeek 气泡配色通过共用渲染入口及网页实际颜色核对，本次未在真机上逐个切换主题和引擎。
- 本次临时 Chromium 已退出，临时页面自动删除；收尾清理完整屏幕截图与 UI 转储，断开 ADB 并停止本次启动的 ADB 服务。保留应用运行及上述归档产物。
