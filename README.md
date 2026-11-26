# 手机自动化工作台

本项目是一个通用 **手机自动化工作台**：PC 端负责设备连接、实时画面、页面采集、OCR 识别、任务与规则编辑、行为录制与回放分析，手机端按任务执行。规则是每个任务内嵌特有的（不共享），工作台本身不绑定任何单一 App。

整个项目主入口是 `启动工作台.bat`（等价于 `python main.py`）；独立刷时长任务可用 `刷时长_看过程.bat`（可见窗口 + 实时过程）。

> **想基于这套方案创建新 App / 新任务的自动化？** 看 [`docs/自动化任务方案.md`](docs/自动化任务方案.md)：包含汽水音乐任务的完整流程、12 条踩坑教训、可复用核心思想，以及用 `flow_engine.py` + `task_template.py` 创建新任务的步骤。

### 项目结构

```text
mobileauto/
├─ 启动工作台.bat          # 唯一启动入口
├─ main.py                 # 程序入口（打包也用它）
├─ requirements.txt
├─ pyrightconfig.json
├─ src/mobileauto/             # 核心代码包
│  ├─ web_gui.py           # pywebview 窗口入口
│  ├─ web_api.py           # Python 桥接层（给 JS 调用）
│  ├─ web/                 # Web 前端资源
│  │  ├─ index.html
│  │  ├─ style.css
│  │  └─ app.js
│  ├─ rules.py             # 声明式规则包
│  ├─ tasks.py             # 任务列表管理
│  ├─ analytics.py         # 回传报告分析
│  ├─ locator.py           # L0-L4 分层定位
│  ├─ cleanup.py           # 图片/记录自动清理
│  ├─ paths.py             # 统一路径（兼容源码与 exe）
│  ├─ inspector.py         # 页面元素检查
│  ├─ adb_runner.py
│  ├─ u2_runner.py
│  ├─ phone_runner.py
│  └─ collector.py
├─ tasks/                  # 任务列表（汽水音乐-领时长是第一个任务）
├─ rules/                  # PC 定制的规则包
├─ runtime/                # 运行数据（日志/报告/采集记录）
├─ templates/              # L2 模板匹配用的按钮小图（可选）
├─ packaging/              # 打包配置
│  ├─ build_exe.bat
│  └─ requirements-dev.txt
└─ README.md
```

所有日志和运行数据统一写入 `runtime/`。任务脚本位于 `src/mobileauto/`，由工作台通过 `python -m` 直接调用。

### 相比原版的改进：
1. **秒退直播间**：识别到屏幕顶部出现「关注」或「更多直播」等特征时，立即触发手机返回键，不用傻等。
2. **精准过滤标题**：修复原版把「再看XX秒继续解锁」倒计时误当成按钮点击导致的无限死循环问题。
3. **防卡死兜底**：当连续多次未能识别到广告目标时，自动执行返回操作，防止停留在未知页面。
4. **双版本可选**：ADB 版每次操作都调用 `adb` 命令；uiautomator2 版脚本本身不调用 `adb` 命令，连接初始化依赖 adb/adbutils，之后通过 HTTP 控制手机。
5. **严格文字匹配**：只把短按钮文字当成目标，不再误点「完成互动，领取奖励福利」这类长句。
6. **倒计时自动等待**：识别到「XX秒后可领取奖励」「再看XX秒继续解锁」时只等待，不再计入未匹配次数、不再乱按返回。
7. **防重复点击**：同一目标/位置 2.5 秒内不重复点击；「领取成功」是状态提示，默认不再反复点击。
8. **回到主页自动停止**：识别到「福利中心 / 免费听时长 / 金币收益 / 现金收益」后停止脚本，避免继续乱按返回。
9. **日志降噪**：默认不再逐帧打印 OCR，只保留点击、状态变化、异常和关键提示。
10. **未匹配留档**：连续未匹配时会把最近 5 帧 OCR 文本和当前截图保存到 `debug_ocr`，方便后续优化。
11. **可选自动启动 App**：可自动打开目标 App（默认 `com.luna.music`，在规则包里配置），并尝试从主页进入任务页。
12. **补充互动按钮**：根据运行记录加入「继续互动」，同时修正精确目标被屏蔽词误杀的问题。
13. **关闭领取成功提示**：识别到「领取成功」后不再点文字本身，改为点击提示条右侧的 X，避免卡在成功提示页。
14. **L0-L4 分层定位**：控件属性 → 控件文本 → 模板匹配 → OCR → 比例坐标，逐级兜底并记录命中层级，兼顾速度与稳定性。
15. **图片自动清理**：调试截图按数量/天数设上限，采集目录按批次设上限，不再无限积累。

> 这些行为都可以在脚本顶部配置区调整：`MISS_BACK_LIMIT`、`CLICK_DEDUPE_WINDOW`、`CLICK_SUCCESS_BANNER`、`STOP_ON_HOME`、`LOG_OCR_EVERY_FRAME`、`AUTO_START_APP`、`APP_PACKAGE`、`AUTO_NAVIGATE_HOME`、`HOME_ENTRY_KEYWORDS`。

### 工作台里的三种任务

在「自动化任务」页面选择启动，不需要单独双击脚本：

| 任务 | 模块 | 设备控制方式 | adb 依赖 |
| --- | --- | --- | --- |
| ADB 版 | `mobileauto.adb_runner` | `adb shell input` / `adb screencap` | 每次操作都调用 `adb.exe` |
| uiautomator2 版 | `mobileauto.u2_runner` | uiautomator2 手机端服务（HTTP） | 连接初始化依赖 adb server，之后走 HTTP |
| 手机版 Termux | `mobileauto.phone_runner` | 手机本地 uiautomator2 | 不调用 adb，需先在电脑装 atx-agent |

---

## 电脑小白手把手使用教程

### 第一步：手机设置（只需设置一次）
1. 打开手机的 **「设置」** -> **「关于手机」**。
2. 连续快速点击 **「版本号」** 7次以上，直到屏幕提示已开启开发者模式。
3. 返回设置页面，找到 **「开发者选项」**。
4. 开启以下开关：
   - **USB 调试**（必须开启）
   - **USB 调试（安全设置）** 或 **允许模拟点击**（小米/vivo等机型必开）
5. 用数据线把手机连接到电脑，手机弹窗勾选始终允许并确定。

---

### 第二步：安装 Python 依赖
两个版本共用同一套依赖，`requirements.txt` 里已经包含 `opencv-python`、`numpy`、`rapidocr-onnxruntime`、`uiautomator2` 和 `pywebview`（Web 工作台所需）。

脚本现在会**先自动检查依赖**，发现缺少的就自己执行 `pip install`，装完继续运行；已经装好的不会重复安装。

1. 确保电脑或手机已安装 Python 3。
2. 依赖检查代码已经**直接写在两个脚本里**，不需要再复制任何额外文件。
3. 直接运行脚本即可，首次运行会看到：
   ```text
   检测到缺少依赖，开始自动安装：
   ```
4. 自动安装失败时才需要手动兜底，在项目目录执行：
   ```bash
   python -m pip install -r requirements.txt
   ```
   在 Pydroid3 里若失败，请先安装 **Pydroid repository plugin**，或在 Pip 界面勾选预编译包后重试。

---

### 第三步：按你选择的版本配置环境

#### A. ADB 版
1. 下载 Android platform-tools：<https://developer.android.com/tools/releases/platform-tools>
2. 解压后，把里面的 `adb.exe` 复制到本项目文件夹；或者把整个 `platform-tools` 目录加入系统 PATH。
3. 验证：在黑窗口输入 `adb version`，能显示版本号即可。
4. 脚本会自动查找 `ADB_PATH`、项目目录、系统 PATH 和常见 Android SDK 目录；找不到时会明确提示并退出，不会反复报错。

#### B. uiautomator2 版
1. 脚本本身不再调用 `adb.exe`，但 uiautomator2 的连接初始化依赖 `adbutils`/adb server，电脑仍需能运行 `adb devices` 并看到设备（adb.exe 可按 A 节方式放到项目目录或 PATH）。
2. 首次运行时，uiautomator2 会自动在手机上安装自动化服务，请保持 USB 调试授权，手机弹窗点「确定」。
3. 默认自动检测已连接的设备；如有多台设备，可设置环境变量 `DEVICE_SERIAL` 或 `ANDROID_SERIAL` 指定，例如 `192.168.1.100:5555`。
4. 连接成功后控制台会显示 `✅ 设备已连接: 设备名 宽x高`。

> 如果提示 `❌ uiautomator2 连接设备失败`，请确认：USB 调试已授权、`adb devices` 能看到设备、`DEVICE_SERIAL` 填写正确。

---

### 第四步：开始使用
1. 双击 **`启动工作台.bat`** 打开工作台。
2. 手机打开规则包里配置的目标 App，进入对应任务页面，保持屏幕常亮。
3. 在「设备页面信息」页确认设备已自动连接；在「自动化任务」页选择 ADB 版或 uiautomator2 版并点「启动」。
4. 运行日志会实时显示在工作台底部。

---

### 第五步：日志与记录
所有日志和记录都统一生成在**程序目录下的 `runtime/`**，打包成 exe 后也一样：

- ADB 版日志：`runtime/mobileauto__adb.log`
- uiautomator2 版日志：`runtime/mobileauto__u2.log`
- 手机版日志：`runtime/mobileauto__phone.log`
- 采集日志：`runtime/collect_page_data.log`
- 点击调试截图：`runtime/debug_clicks/`
- 未匹配 OCR 留档：`runtime/debug_ocr/`
- 手机版 UI 层级留档：`runtime/debug_ui/`
- 页面采集数据：`runtime/page_data/`

日志采用追加方式，每次启动都会写入一行包含时间的分隔记录。启动时控制台也会显示日志文件的实际路径。

调试图片和采集目录都有自动清理：`runtime/debug_clicks/`、`debug_ocr/`、`debug_ui/`、`debug_phone/` 默认最多保留 60 个文件，且超过 3 天自动删除；`runtime/page_data/` 最多保留最近 8 批采集。上限可在 `src/mobileauto/paths.py` 的 `DEBUG_KEEP_MAX`、`DEBUG_KEEP_DAYS`、`COLLECT_KEEP_MAX` 调整，不会无限积累。

如果想让脚本自动打开目标 App 并尝试进入任务页，把配置区改为：

```python
AUTO_START_APP = True
AUTO_NAVIGATE_HOME = True
```

目标 App 包名通过环境变量 `MOBILEAUTO_PACKAGE` 或规则包的 `app` 字段配置，默认是 `com.luna.music`。

---

### 第六步：纯手机运行（Termux）

手机版任务（`mobileauto.phone_runner`）是**纯手机端方案**：不依赖 OCR、`cv2`、`numpy`、adb 命令，直接通过 uiautomator2 读取界面文字来判断按钮。

准备步骤：

1. 从 **F-Droid** 安装 Termux（Play 商店版本已废弃）。
2. 在 Termux 中执行：
   ```bash
   pkg update && pkg upgrade
   pkg install python
   pip install uiautomator2
   ```
3. **关键一步**：首次建议在电脑上执行一次：
   ```bash
   python -m uiautomator2 init
   ```
   它会把 atx-agent 安装到手机上。然后在手机「设置 → 无障碍」里给 atx-agent / uiautomator 开启无障碍服务权限。
4. 在 Termux 里进入项目目录运行：
   ```bash
   cd /sdcard/Download/mobileauto
   PYTHONPATH=src python -m mobileauto.phone_runner
   ```

说明：

- 脚本会依次尝试 `ATX_AGENT_URL`、`http://127.0.0.1:7912`、`127.0.0.1:7912`、自动检测，兼容不同 uiautomator2 版本。
- 如果连接失败，可设置：
  ```bash
  export ATX_AGENT_URL=http://127.0.0.1:7912
  ```
- 手机版日志：`mobileauto__phone.log`
- 点击截图：`debug_phone` 文件夹
- 连续未匹配时的 UI 层级记录：`debug_ui` 文件夹

限制：

- 手机版靠 UI 文字识别。如果广告文字是图片/Canvas 绘制、UI 层级里读不到文字，这个版本识别不到，需要用电脑上的 OCR 版。
- Termux 里直接运行 `python -m uiautomator2 init` 经常因为没有 adb 而失败，所以首次安装 atx-agent 建议在电脑上做。
- 如果 atx-agent 没有拿到无障碍权限，无法控制其他 App。

---

### 第七步：页面数据采集模式（用于优化）

这是工作台里的一个任务：**不执行自动点击，只把手机页面数据存下来**，方便把记录发回来继续优化识别规则。

打开工作台后切到「采集页面数据」页，填写帧数、间隔、前缀、连接方式、OCR 选项，点「开始采集」即可。命令行也支持：

```bash
PYTHONPATH=src python -m mobileauto.collector --count 15 --interval 0.8 --prefix 异常页面
```

在 Termux / 手机上同样可用：

```bash
cd /sdcard/Download/mobileauto
PYTHONPATH=src python -m mobileauto.collector --count 10 --interval 1 --mode u2
```

脚本会优先用 uiautomator2 连接，失败时回退到 adb。采集结果保存在：

```text
runtime/page_data/时间戳_前缀/
```

目录结构：

```text
frames/   截图
ocr/      OCR 文本（环境支持时生成）
ui/       UI 层级 XML（u2 模式生成）
summary.txt
```

日志：`collect_page_data.log`

采集到异常页面后，把整个 `runtime/page_data/时间戳_前缀/` 目录发回，我就能按真实页面继续优化目标文字、ROI 和按钮识别规则。

---

### 第八步：交互工作台（主推 Web 版）

中长期方案采用 **pywebview + HTML/CSS/JS**：Python 负责设备控制、任务进程和文件读写，界面用现代 Web 技术渲染，兼顾轻量、美观和后续可扩展性。双击：

```text
启动工作台.bat
```

首次运行需要安装 `pywebview`（执行 `python -m pip install -r requirements.txt` 即可）。Windows 上界面依赖 Edge WebView2 Runtime，Win10/11 多数已内置，缺失时 pywebview 会提示安装。程序启动后会自动扫描可连接设备，断线后自动重连。界面为左侧导航 + 右侧内容页，不再把一堆按钮平铺在一起。

左侧导航：

- 设备工作台
- 任务列表
- 回放分析
- 记录文件

页面说明：

1. **设备工作台**（主页面）
   - 左侧「设备状态」卡片集中放连接控制：设备地址、自动连接、开始扫描、刷新、断开、诊断
   - 顶部选择任务并点「执行任务」；实时画面下方是固定高度的「执行过程」窗口，最多显示最近 5 步并自动滚动，只显示「做了什么」
   - 实时画面：手机真实画面，UI 控件树解析出的红框叠加在画面上；画面下方是安卓三键（返回/主页/最近）
   - 行为录制已并入本页右侧：开始/停止录制、选点/框选、折叠循环、清空、转成任务；录制步骤可改名、改动作、改坐标、试点击、排序、删除
   - 按钮识别不到时点「选点/框选」，在画面上单击定位一个点，或拖拽框选区域取中心点，自动试点击并写入录制草稿
   - 可点击按钮：小方块网格，带序号，点一下即可操作
   - 任务启动后按**任务内嵌的规则**自动执行；跳开屏和按钮识别全部走 OCR（App 是 Canvas 自绘，UI 层级读不到文字），识别到文字立刻点击
   - 采集：嵌在画面下方，点一下「采集当前页面」即可；当场抓帧 + OCR 识别文字 + 控件树，结果直接显示在按钮下方

2. **任务列表**：管理多个任务（新建、编辑 JSON、启停、删除、直接执行）。编辑区有三个标签：
   - **JSON 编辑**：直接改任务 JSON（每个任务自带规则 `flow` 和可选能力）+ 能力勾选
   - **流程视图**：本地渲染的流程图，显示步骤、点击、判断、循环回边、卡住重试，并给出步骤数/点击数/循环轮数统计
   - **Mermaid**：同一流程的 Mermaid 源码，可复制到支持 Mermaid 的编辑器生成专业图
   - 顶部「导出流程 JSON / 导入流程 JSON」使用可流通的 `FlowSpec v1` 标准格式（`mobileauto/flow/v1`），文件可跨工具流通、版本管理

3. **回放分析**：统计手机回传的层级命中与耗时

4. **记录文件**：按类型筛选并预览 `runtime/` 下的文件——运行日志、采集 OCR 文本与 UI XML（写规则用）、录制/执行日志 JSONL、回放报告。点中文件在右侧预览文本

Web 前端通过 `src/mobileauto/web_api.py` 暴露的桥接方法调用 Python 能力，所有数据仍在 `runtime/`。

### 可流通的任务流程格式（FlowSpec v1）

参考 Maestro（声明式 YAML flow + Studio 可视化）、Tasker（触发器/动作/变量）、Node-RED（可视化流程编排）等产品的共同思路，任务被设计为**人可读、机器可写、可版本管理、可跨工具流通**的标准 JSON：

```json
{
  "$schema": "mobileauto/flow/v1",
  "version": "1.0",
  "meta": { "name": "每日打卡", "app": "com.example.app" },
  "settings": { "engine": "configured", "max_loops": 3, "capabilities": {} },
  "locators": [{ "id": "loc_1", "type": "text", "value": "领取奖励" }],
  "flow": {
    "start": "DONE",
    "mode": "priority",
    "states": [
      { "id": "REWARD", "name": "领奖", "when": { "text_any": ["领取奖励"] }, "do": [{ "action": "click_match" }] }
    ],
    "loops": [{ "id": "main_loop", "from": "REWARD", "max": 30 }]
  },
  "observability": { "screenshot_on_step": true, "ocr_on_step": true }
}
```

要点：
- **动作与观察分离**：`locators` 定义「怎么找」，`states` 定义「找到后做什么」，`observability` 定义「记录什么」
- **智能等待**：`when.text_any` 是轮询命中条件，代替硬编码 sleep
- **多策略定位**：文字 / 比例坐标 / OCR 兜底同时保留
- **循环可视化**：`flow.loops` 在流程视图里画成回边，Mermaid 源码同步生成
- **导入导出**：任务列表顶部按钮导出 `.flow.json`，导入后自动转为工作台任务（默认停用，避免误执行）

对应实现在 `src/mobileauto/flow_schema.py`（转换、校验、Mermaid/图结构生成、统计）。

---

### 实时画面流

工作台「设备工作台」页的手机视图显示的是**手机真实画面**，不是结构示意图：

- 优先复用工作台已连接的设备抓帧，不需要单独配置 adb
- 若已连接设备不可用，再尝试 `adb-scr-py` 走 scrcpy 协议抓帧（约 50ms）
- Python 后端在 `127.0.0.1:8765` 提供 MJPEG 流，前端直接显示
- UI 控件树解析出的红框叠加在真实画面上，坐标一一对应
- 未连接设备时显示简洁占位画面，状态文字在图下方说明，不往画面里写中文（避免乱码）

依赖 `adb-scr-py` 已加入 `requirements.txt`，需要 adb 可用且手机开启 USB 调试。

---

### 分层定位策略（L0-L4）

三个运行脚本共用 `src/mobileauto/locator.py`，按下面的顺序查找按钮，命中即停，控制台会打印实际命中的层级：

| 层级 | 策略 | 适用条件 | 配置方式 |
| --- | --- | --- | --- |
| L0 | 控件 ID / 稳定属性 | `resource-id` 或 `content-desc` 可识别 | `TARGET_IDS`（id 关键词）、`TARGET_DESCS`（desc 精确/包含），默认已把目标文字作为 desc |
| L1 | 控件文本（精确 + 模糊） | 控件树有可读 `text` | 自动执行，允许 1 字误差，拒绝长句与倒计时 |
| L2 | 模板匹配 | Flutter / Canvas / WebView 渲染，控件树无文字 | 把按钮小图命名为 `领取奖励.png` 放到项目根目录 `templates/`，阈值 `TEMPLATE_THRESHOLD`（默认 0.85） |
| L3 | OCR 兜底 | 前三级都失败但仍有视觉文字 | u2 / adb 版自动执行；手机版无 OCR 会跳过 |
| L4 | 百分比坐标 | 以上全部失败且按钮位置固定 | `TARGET_RATIO`，默认中央按钮 `(0.5, 0.52)`；「领取成功」X 为 `(0.915, 0.07)` |

每一级都受 `TARGET_ROI` 区域约束，并复用 2.5 秒点击去重。把脚本顶部 `ENABLE_LAYERED` 设为 `False` 可跳过控件层级读取（不启用 L0/L1），只保留 L2-L4 视觉兜底。`LOG_HIT_LEVEL = True` 时日志形如 `[L1/text] 目标「领取奖励」...`，可直接看出哪一级生效。

「领取成功」提示条走单独逻辑：先用 L0-L3 确认提示出现，再找右上角关闭控件（L0），找不到时用 L4 比例坐标，不会误点文字本身。

---

### 规则与回放分析

**规则（已合并进任务）**
- 规则不再是共享的全局规则包，而是每个任务 JSON 里内嵌的 `flow.steps`；在「任务列表 → 编辑」里直接改
- 默认任务内嵌完整流程（领时长→看广告→领奖），旧任务缺 `flow` 时启动自动补齐
- 规则文件 `rules/` 保留作为模板来源，供新建任务时复制

**回放分析**
- 「拉取手机报告」通过 adb 把手机 `/sdcard/mobileauto/reports/*.jsonl` 拉回 `runtime/reports/`
- 选择报告后点「分析」，查看层级分布、平均耗时、最近事件
- 手机端执行引擎按 JSONL 逐条写事件：`{"ts":"...","target":"...","level":"L1","source":"text","cost_ms":12,"result":"clicked"}`

> 阶段2 目前完成的是 **PC 端**。手机端执行引擎读取规则并回传报告是下一步。

---

### 第九步：打包为 exe

项目已经按可打包结构整理好了。后续要打包时：

1. 确认已安装 Python。
2. 双击：
   ```text
   packaging\build_exe.bat
   ```
3. 脚本会自动创建 `venv`、安装 `requirements.txt`（含 `pywebview`）和 `packaging\requirements-dev.txt`，然后调用 PyInstaller；打包命令通过 `--add-data` 把 `src/mobileauto/web/` 的 HTML/CSS/JS 静态资源一起打进 exe。
4. 输出目录：
   ```text
   dist\MobileAuto\
   ```

打包入口是根目录 `main.py`，它启动的是 Web 工作台。`src/mobileauto/paths.py` 会自动判断：

- 源码运行：项目根目录作为程序目录
- exe 运行：exe 所在目录作为程序目录

因此运行数据始终写到 exe 旁边的 `runtime/`，不会写进 PyInstaller 临时目录。

---

### 第十步：如何退出
命令行运行时按：
```text
Ctrl + C
```

GUI 运行时点“停止”按钮，或直接关闭窗口；Web 工作台直接关闭窗口即可。
