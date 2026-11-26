# 手机自动化工作台：完整功能规格

本文档记录现有工作台的全部功能，作为后续重构的验收基线。新实现必须逐项保留这些能力；数据来源、文件格式与用户操作语义都应兼容。

---

## 1. 产品定位

工作台是一个 Windows 桌面应用，用于连接安卓手机，在电脑上完成：

- 实时查看手机画面并在画面上点击、滚动、按系统键。
- 识别手机上的可点击控件并在画面上叠加按钮框。
- 通过选点、框选、选择识别按钮或系统操作录制自动化步骤。
- 将录制步骤转换成任务草稿，编辑后执行。
- 通过 OCR + 控件树驱动任务自动运行。
- 查看任务执行过程、回放统计、记录文件。

---

## 2. 启动、窗口与目录

### 2.1 启动方式

- `启动工作台.bat`
  - 设置 UTF-8 代码页。
  - 进入脚本所在目录。
  - 检查 `pywebview`，缺失时自动安装。
  - 执行 `python main.py`。
- `main.py`
  - 把 `src` 加入 `sys.path`。
  - 调用 `mobileauto.web_gui.main()`。
- `刷时长_看过程.bat`
  - 固定任务 `qishui_time`。
  - 通过 `QISHUI_MAX_ROUNDS` 控制轮数，默认 30，0 表示无限。
  - 执行 `python -u -m mobileauto.app_task`，同时写入 `runtime/flow_live.log`。
- `packaging/build_exe.bat`
  - 使用 PyInstaller 打包为 `MobileAuto`。
  - 附带 web 前端资源和 `webview` 子模块。

### 2.2 窗口

- 标题：`手机自动化工作台`。
- 初始尺寸：`1500 × 980`。
- 最小尺寸：`1180 × 780`。
- 可调整大小。
- 启动时先启动画面服务，再创建 API，最后打开 WebView。

### 2.3 目录

| 目录 | 用途 |
|---|---|
| `tasks/` | 任务 JSON |
| `rules/` | 规则包 JSON |
| `templates/` | L2 模板图片 |
| `runtime/` | 运行数据根目录 |
| `runtime/debug_clicks/`、`debug_ocr/`、`debug_ui/`、`debug_phone/` | 调试产物 |
| `runtime/page_data/` | 单帧采集批次 |
| `runtime/reports/` | 手机回传报告 |
| `runtime/flow_probe/` | 流程探测记录 |

清理策略：调试文件保留 3 天且最多 60 个；采集批次最多保留 8 个。

### 2.4 数据格式

- 任务 JSON：`tasks/<key>.json`
  - 字段：`key`、`name`、`description`、`app`、`rule`、`flow`、`engine`、`enabled`、`max_loops`、`capabilities`、`states`、`stuck_states`、`stuck_limit`、`finish_info_keywords`、`poll`、`tap_gap`、`updated`。
- 规则包 JSON：`rules/*.json`
  - 字段：`version`、`name`、`app`、`updated`、`targets`、`flow`、`fallback`、`guards`。
- 活动日志：`runtime/activity.jsonl`
  - 每行 JSON：`ts`、`kind`、`target`、`detail`，可选 `session` 与扩展字段。
- 录制草稿：`runtime/recording_draft.json`
  - 步骤数组：`id`、`kind`、`detail`、`target`、`action`，可选 `ratio`、`x/y`、`texts`、`label`、`match`、`loop`。
- 设备地址缓存：`runtime/last_device_address.txt`。
- 执行日志：`runtime/flow_live.log`。
- 单帧采集目录：`runtime/page_data/<时间戳>_单帧采集/`
  - `screen.png`、`ocr.txt`、`ui.xml`。
- FlowSpec：
  - `$schema = mobileauto/flow/v1`，`version = 1.0`。
  - 段：`meta`、`settings`、`locators`、`flow`、`observability`。
  - `flow.mode` 支持 `priority` 与 `sequence`。
- 手机报告：`/sdcard/mobileauto/reports/*.jsonl`，拉回后为 `runtime/reports/phone_*.jsonl`。
- 规则下发目标：`/sdcard/mobileauto/rules/active.json`。

---

## 3. 页面结构

应用包含四个导航页，侧栏设备状态卡在所有页面共享。

### 3.1 全局设备状态卡

- 状态文本：设备状态。
- 设备描述：连接标签。
- 地址输入框：手机 IP:7912 或 adb serial。
- 自动连接开关，默认开启。
- 按钮：
  - 扫描：使用当前地址发起连接。
  - 刷新：重新读取控件树。
  - 断开：清空连接并黑屏。
  - 诊断：显示 adb、设备、u2 服务和提示。
- 状态联动：
  - 扫描中禁用扫描按钮。
  - 未连接禁用刷新、断开、返回、主页、最近。
  - 任务运行中禁用执行和采集；未运行禁用停止。

### 3.2 设备工作台

顶部：

- 任务下拉框：列出全部任务，停用任务显示“（停用）”。
- 执行任务：启动选中任务。
- 停止任务：终止任务进程。

左侧：

- 按钮识别工具栏：
  - 过滤按钮名称，匹配 text、desc、resource-id、class。
  - 隐藏系统 UI，过滤 `com.android.systemui`。
  - 可点击元素数量。
  - 显示按钮识别开关。
- 实时画面：
  - MJPEG `<img>` 回退。
  - H264/WebCodecs Canvas。
  - 手机刘海装饰。
  - 选点/框选覆盖层。
  - 画面状态与 H264 FPS。
- 系统按键栏：
  - 向上滚动。
  - 返回。
  - 主页。
  - 最近任务。
  - 向下滚动。
- 采集区：
  - 采集当前页面。
  - 展示采集时间、OCR 段数、按钮数、缩略图、文字列表和目录。
- 行为录制面板：
  - 开始/停止录制。
  - 选择按钮。
  - 选点/框选。
  - 折叠循环。
  - 清空。
  - 转成任务。
  - 可编辑步骤列表。
- 执行过程面板：
  - 固定显示约 5 行，全部记录可滚动。
  - 每条显示圆点、详情、时间。
  - 轮询状态，设备页激活时刷新执行与录制步骤。

### 3.3 任务列表

- 列表项：
  - 任务名、启用勾选框、描述。
  - 元信息：App、规则、循环、引擎、能力数、修改时间。
  - 编辑、执行按钮。
  - 点击整项打开详情。
- 头部操作：
  - 刷新任务。
  - 新建任务。
  - 保存任务。
  - 删除任务。
  - 导入 FlowSpec。
  - 导出 FlowSpec。
- 三个编辑标签：
  - JSON 编辑：直接编辑任务 JSON；8 个能力勾选框与 JSON 双向同步。
  - 流程视图：Mermaid 渲染；统计模式、步骤、点击、循环、结束词；缩放、拖拽、适应窗口。
  - Mermaid 源码：显示并可复制。
- FlowSpec 导入导出：
  - 导出为 FlowSpec v1，保存到用户选择路径。
  - 导入时校验 schema、节点非空、id 不重复。
  - 导入任务默认停用。

### 3.4 回放分析

- 报告来源：
  - `runtime/reports/*.jsonl`、`*.log`。
  - `runtime/activity.jsonl` 作为 PC 任务执行日志。
  - 默认优先选中 `activity.jsonl`。
- 统计：
  - 总事件数、已执行数、失败数、成功率。
  - 动作/层级分布、结果分布、来源分布、目标分布、各层级平均耗时。
  - 最近事件列表，最多 150 条。
- 结果推断：
  - 失败/FAIL → error。
  - 超时 → timeout。
  - [OK]/成功 → ok。
  - 点击类动作 → clicked。
  - task → task。
  - 其他 → info。

### 3.5 记录文件

- 递归扫描 `runtime/`，允许 `.log`、`.txt`、`.xml`、`.json`、`.png`。
- 返回相对路径、文件名、大小、mtime，最多 800 个。
- 筛选：
  - 全部、日志、OCR/文本、UI 层级、报告。
- 点击列表项读取文本预览，最多 300000 字符。
- 路径必须在 `runtime/` 内，防止越界。
- PNG 不显示图片，只显示路径提示。

---

## 4. 设备连接与控制

### 4.1 连接模式

支持 `auto`、`u2`、`adb`。

无显式地址时：

1. 检测本机 `127.0.0.1:7912` 的 u2/atx-agent。
2. 查询 adb 已授权设备。
3. 最后尝试 u2 自动发现。

地址形态：

- `IP:7912` 或 `http://...` 视为 u2 地址。
- u2 地址会转换为 `IP:5555` 尝试 adb。
- adb serial 直连。

adb 查找顺序：`ADB_PATH` → PATH → 项目根目录 `adb.exe` → `platform-tools/adb.exe`。

### 4.2 自动重连

- 后台常驻。
- 未连接且自动连接开启时，用上次成功地址连接。
- 退避从 2 秒开始，每次 ×1.5，上限 12 秒。
- 连接成功后重置退避和重试次数。

### 4.3 控件树

- u2：`dump_hierarchy()`。
- adb：`uiautomator dump /sdcard/window.xml` 后 `cat`。
- 解析字段：
  - `index`、`text`、`content-desc`、`resource-id`、`class`、`package`、`clickable`、`enabled`、`visible`、`bounds`。
  - 派生：`x1/y1/x2/y2/cx/cy/w/h`。

### 4.4 点击与滚动

- `click_xy(x,y,record=true)`
  - 优先 scrcpy 控制通道。
  - 失败回退 u2 click 或 adb `input tap`。
  - 写活动日志；录制开启时写草稿。
- `click_ratio(rx,ry)`
  - 用缓存分辨率或 scrcpy 分辨率换算像素。
  - 用于试点击，不写草稿。
- `scroll_page("up")`
  - x = w/2，y 从 0.75h 到 0.30h。
- `scroll_page("down")`
  - x = w/2，y 从 0.30h 到 0.75h。
  - 优先 scrcpy 带插帧 swipe。
- `press_back/home/recents`
  - u2：`back` / `home` / `recent`。
  - adb：keyevent `4` / `3` / `187`。

---

## 5. 实时画面

### 5.1 双链路

MJPEG 回退：

- HTTP `127.0.0.1:8765`。
- `/stream` multipart MJPEG，`/frame` 单帧 JPEG。
- u2 直接取设备 JPEG；adb 用 `screencap -p`。
- 识别模式 0.04 秒/帧、JPEG 质量 88。
- 高清模式 0.02 秒/帧、JPEG 质量 98。

H264 主链路：

- 官方 scrcpy-server v5.0.1。
- 推送到 `/data/local/tmp/scrcpy-server-5.0.1.jar`。
- `adb forward` 到本地随机端口。
- WebSocket `ws://127.0.0.1:8766`。
- 参数：H264、60fps、8Mbps、max_size=1920、控制开启、音频关闭。
- 协议：
  - `0x00 + description`：SPS/PPS。
  - `0x01 + keyflag + AVCC NAL`：视频帧。
  - 后端每个客户端最多一个待发送帧，旧帧丢弃。
  - 前端 `decodeQueueSize > 2` 时丢帧。
- WebCodecs 不支持或解码失败时回退快速截图。

### 5.2 断开行为

- 生成纯黑 JPEG 并替换最新帧。
- 状态改为“设备已断开”。
- 取消 scrcpy 连接任务并关闭会话。
- 不再保留最后一帧。

### 5.3 按钮覆盖层

- 按控件边界相对屏幕百分比定位。
- 最多渲染 200 个控件；跳过超过屏幕 90% 的大容器。
- 标签取 text、desc、resource-id 末段，最多 10 字。
- 默认红色半透明框；悬浮加深并外发光；选中蓝色框。
- “选择按钮”模式变琥珀色，鼠标为 copy。
- 状态签名避免轮询期间重复重建 DOM。

### 5.4 选点与框选

- 提示显示 2.6 秒后自动消失。
- 单击：点击该点。
- 拖拽：取矩形中心。
- 归一化坐标乘以屏幕尺寸得到物理像素。
- 先试点击，再加入录制草稿。
- 未连接或分辨率未知时提示无法定位。

---

## 6. 单帧采集 / OCR / 控件树

点击“采集当前页面”后：

1. 抓当前帧。
2. 写入 `runtime/page_data/<时间戳>_单帧采集/screen.png`。
3. RapidOCR 识别文字；宽度超过 960 时先缩放。
4. 写 `ocr.txt`：时间、文字数、每条文字/置信度/中心坐标。
5. 抓控件树并写 `ui.xml`。
6. 生成 JPEG base64 预览。
7. 返回：
   - `ok`、`time`、`dir`、`preview`。
   - `ocr`：文字、分数、中心坐标。
   - `clickable`：名称、中心坐标。
   - `ocr_count`、`clickable_count`。
8. 前端展示缩略图、时间、OCR 段数、按钮数、文字列表、目录。
9. 录制开启时，把前 20 条 OCR 文本作为 `capture` 步骤写入日志。

---

## 7. 行为录制

### 7.1 录制来源

开始录制后，以下行为写入活动日志和草稿：

- 点击坐标。
- 点击红框。
- 采集页面。
- 返回、主页、最近任务。
- 滚动。

手动选点/框选、选择按钮无需开启录制也加入草稿。

清空操作只删除录制草稿和带 `recorded` 标记的活动日志，保留任务执行日志。

### 7.2 步骤编辑

每个步骤支持：

- 序号。
- 名称输入框，失焦保存。
- 动作下拉框，8 种选项：
  - `click_ratio` 点击坐标。
  - `click_match` 点击文字。
  - `back` 返回键。
  - `scroll` 页面滚动。
  - `wait` 等待。
  - `finish` 结束。
  - `mute_once` 静音一次。
  - `close_ad` 关广告。
- `click_ratio` 显示 X/Y 比例输入框，范围 0–1，步长 0.001。
- 编辑期间暂停自动刷新。
- 操作按钮：
  - 试点击。
  - 上移。
  - 下移。
  - 删除。
- 每次编辑整体写回草稿。

### 7.3 折叠循环

- 步骤签名：`action:ratio`。
- 检测连续重复序列，最小重复 2 次。
- `A B A B A B` → `start=0, length=2, repeat=3`。
- 折叠为 `loop` 步骤，`body` 保存片段，`repeat` 保存次数。

### 7.4 转任务

- 输入任务名。
- 每个步骤转换为状态：
  - `click_ratio`：保留比例。
  - `click_match`：保留文字。
  - `back`、`scroll`、`wait`、`finish`、`mute_once`、`close_ad`：映射动作。
  - `loop`：展开 body，并把 repeat 作为 max_loops。
- 前一次采集的 OCR 文本作为后续点击的 match 条件。
- 生成任务：
  - `engine=configured`。
  - `enabled=false`。
  - 使用默认能力。
- 保存为 `tasks/<key>.json`。

---

## 8. 任务执行引擎

### 8.1 启动链路

- 读取任务 JSON。
- 设置环境变量：
  - `MOBILEAUTO_TASK_KEY`
  - `MOBILEAUTO_PACKAGE`
  - `MOBILEAUTO_MAX_LOOPS`
- 启动独立子进程。
- 同一时刻只允许一个任务进程。

### 8.2 状态匹配

按顺序匹配状态，第一条命中胜出：

- `match`：任一关键词命中。
- `match_any`：任一条件满足。
- `match_all`：全部条件满足。
- `exclude`：命中排除词时忽略。
- 条件可为字符串、数组或嵌套 any/all/match/exclude。
- `fallback: true` 作为未知页默认状态。

### 8.3 动作类型

配置驱动引擎支持：

1. `click_match`：点击 OCR 命中文字，支持 click_target、click_exclude、count、retry、reset_flags。
2. `click_ratio`：按比例坐标点击。
3. `click_ratio_once`：每状态只点一次。
4. `back`：返回键并验证。
5. `scroll`：按 direction 滚动。
6. `wait`：等待。
7. `finish`：结束任务并输出结束信息。
8. `mute_once`：一次性静音，默认比例 `[0.23,0.027]`。
9. `close_ad`：优先点 OCR 的 ×，否则按比例坐标，最后回退返回键。

录制 UI 的动作下拉不包含 `click_ratio_once`，但引擎支持。

### 8.4 能力开关

默认 8 项全开，任务 JSON 可逐项覆盖：

- `ocr`
- `action_verify`
- `anti_stuck`
- `dedupe`
- `structured_log`
- `end_detect`
- `auto_start`
- `entitlement`

### 8.5 OCR、去重与验证

- RapidOCR 懒加载；宽度 >960 先缩放再换算坐标。
- `tap_gap` 默认 0.35 秒；去重开启时间隔内重复点击被丢弃。
- 动作验证开启时，最多等 2.5 秒重新 OCR 并分类；页面未变化则 FAIL，可重试。
- `poll` 默认 0.25 秒。
- `max_rounds=0` 表示无限循环。
- 自动启动开启时启动 App 并等待 3.5 秒。

### 8.6 防卡死

- `stuck_states` 指定易卡状态。
- 同一状态连续无变化，`idle % stuck_limit == 0` 时按返回键兜底。
- `stuck_limit` 默认 12。
- 明确结束态命中后直接停止。

### 8.7 分层日志

- 控制台和 `runtime/flow_live.log` 双输出。
- 日志类型：
  - 任务标题框、模式、App。
  - 轮次分隔。
  - 页面切换 old → new + 关键词。
  - 动作与坐标。
  - 验证结果 [OK]/[FAIL]。
  - 详情、错误、结束信息。
- `structured_log=false` 时只输出简版标题。

### 8.8 内置汽水任务

状态优先级：

`DONE > SPLASH > CONTINUE > REWARD > COUNTDOWN > LIVE > LANDING > SUCCESS > WELFARE > HOME > AD > UNKNOWN`

行为：

- 跳过开屏。
- 进入福利页。
- 点领时长。
- 广告静音。
- 倒计时只等待并静音，不点击倒计时文字。
- 直播页一次性关直播。
- 关直播落地页。
- 领取成功尾页退出。
- 领取奖励计数并重置静音/一次性标志。
- 识别“明日再来/畅听权益截止/已领完”后结束并输出权益截止时间。
- 默认 30 轮；stuck_states=SUCCESS/LANDING/LIVE，stuck_limit=12。

### 8.9 旧版顺序流程引擎

保留兼容：

- 每步配置 `find`、`stay_text`、`click_text`、`click_ratio`、`action=back`、`timeout_ms`、`interval_ms`、`max_clicks`、`ocr_width`。
- 点击冷却 0.5 秒。
- `loop_from` 指定循环起点。
- 每步独立超时；超时记录 timeout 并停止。

---

## 9. 规则包、定位器与辅助能力

### 9.1 规则包

- CRUD：列出、加载、保存、删除。
- 校验：
  - 根节点为对象。
  - 必须有 name。
  - targets 非空，每个 target 必须有 key。
  - roi.x/y 在 0–1。
  - fallback.ratio.points 为二元数组。
- 默认规则包含：
  - L0–L4 定位策略。
  - 6 步业务 flow。
  - template/OCR/ratio 三路 fallback。
  - guards：倒计时等待、主页停止、去重、点击冷却、未命中返回上限。
- 下发：`adb push` 到 `/sdcard/mobileauto/rules/active.json`。

### 9.2 分层定位器

- L0：resource-id / content-desc 稳定属性。
- L1：控件 text 精确/模糊匹配。
- L2：模板匹配，支持 0.9/1.0/1.1 缩放和阈值。
- L3：OCR 文字。
- L4：固定比例坐标。
- 文本归一化、OCR 错字修正、倒计时/长句拦截。
- ROI 限制、系统控件过滤、点击去重、右上角关闭按钮定位。

### 9.3 报告拉取

- 查询 `/sdcard/mobileauto/reports`。
- 拉取 `.jsonl` 到 `runtime/reports/phone_<name>`。

### 9.4 流程探测与采集

- `flow_probe.py`：每步记录截图、OCR、包名到 `runtime/flow_probe/`，写 `state_log.jsonl`。
- `collector.py`：交互式或命令行多帧采集，参数 `--count`、`--interval`、`--mode`、`--prefix`、`--ocr`。
- `task_template.py`：新任务模板。
- `cleanup.py`：按天数和数量清理调试文件与采集批次。

---

## 10. 前端调用 API 清单

所有前端调用都通过统一桥接封装。新项目必须提供等价能力。

### 10.1 状态与轮询

| API | 用途 |
|---|---|
| `get_state()` | 设备状态、按钮行、屏幕尺寸、任务状态 |
| `get_stream_status()` | 画面状态、MJPEG URL、WS URL、H264/FPS |
| `clear_log()` | 清空内存日志 |

### 10.2 设备连接

| API | 用途 |
|---|---|
| `set_auto_connect(enabled)` | 自动连接开关 |
| `remember_address(address)` | 保存地址 |
| `connect(mode,address)` | 扫描连接 |
| `refresh()` | 刷新控件树 |
| `diagnose_device()` | 诊断 |
| `disconnect()` | 断开并黑屏 |

### 10.3 设备操作

| API | 用途 |
|---|---|
| `scroll_page(direction)` | 上下滚动 |
| `press_back()` | 返回键 |
| `press_home()` | 主页键 |
| `press_recents()` | 最近任务键 |
| `click_xy(x,y,record?)` | 点击坐标 |
| `click_ratio(rx,ry)` | 试点击 |
| `click_element(index)` | 按控件 index 点击 |

### 10.4 画面与识别

| API | 用途 |
|---|---|
| `set_stream_quality(clear)` | 画质模式 |
| `set_detection(enabled)` | 按钮识别开关 |
| `set_h264_enabled(enabled)` | H264 开关 |
| `start_collect(options)` | 兼容旧调用 |

### 10.5 采集与录制

| API | 用途 |
|---|---|
| `capture_page()` | 单帧采集 |
| `get_recording_state()` | 录制状态与步骤 |
| `start_recording()` / `stop_recording()` | 录制开关 |
| `clear_recorded()` | 清空录制 |
| `add_manual_click(x,y,label)` | 手动加入点击步骤 |
| `save_recording_draft(steps)` | 整体保存草稿 |
| `delete_recorded_step(index)` | 删除步骤 |
| `move_recorded_step(index,offset)` | 移动步骤 |
| `fold_recording_loops()` | 折叠循环 |
| `recording_to_task(name,app)` | 转成任务 |
| `list_actions(limit)` / `clear_actions()` | 活动日志 |
| `analyze_recording()` | 录制分析 |

### 10.6 任务与流程

| API | 用途 |
|---|---|
| `start_task(key)` / `stop_task()` | 任务执行 |
| `get_task_progress(limit)` | 执行过程 |
| `list_tasks()` / `load_task(key)` / `save_task(data)` / `delete_task(key)` | 任务 CRUD |
| `set_task_enabled(key,enabled)` | 启用停用 |
| `select_task(key)` | 选择任务 |
| `task_mermaid(key)` | 生成 Mermaid 与统计 |
| `save_flow_as(key,name)` | 原生导出 FlowSpec |
| `export_task_flow(key)` / `import_task_flow(spec)` | FlowSpec 导入导出 |

### 10.7 记录与报告

| API | 用途 |
|---|---|
| `list_records()` / `read_record(path)` | 记录文件列表和预览 |
| `list_reports()` / `analyze_report(name)` | 回放分析 |
| `pull_reports()` | 拉取手机报告 |

---

## 11. 新项目验收清单

新项目完成时必须逐项验证：

1. 四页导航与全局设备卡。
2. u2/adb 自动连接、手动地址、自动重连、诊断、断开黑屏。
3. scrcpy H264 实时画面与控制通道；MJPEG 回退。
4. 按钮识别覆盖层、悬浮高亮、过滤、系统 UI 过滤、识别开关。
5. 点击执行、选择按钮录制、选点/框选录制。
6. 单帧采集、OCR、控件树、预览与落盘。
7. 录制编辑、折叠循环、转任务。
8. 任务 CRUD、FlowSpec 导入导出、Mermaid 流程视图与缩放拖拽。
9. 配置驱动状态机引擎、9 种动作、8 个能力、验证、去重、防卡死、分层日志。
10. 活动日志、执行过程、回放统计、记录文件预览。
11. 规则包 CRUD/校验/下发、手机报告拉取。
12. 数据目录和文件格式兼容旧项目。
