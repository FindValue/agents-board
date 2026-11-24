# AGENTS.md — agent-board

本地常驻置顶透明悬浮窗，展示当前打开的 agent 会话（qodercli、VS Code agent host 里的 Copilot、opencode）的处理进展。
Python 3.13 + tkinter，**只用标准库**，无构建、无依赖、无测试框架。

启动：`python board.py`（或 `start.cmd`，后台用 `pythonw`）。
无界面自检：`python board.py --once` / `--once --repeat` / `--home <dir>`（`--home` 喂给所有 `*_homes`，各适配器用自己的 marker 决定认不认）。

## 架构与职责边界

```
board.py            tkinter HUD：绘制、透明度动画、拖拽/缩放、右键菜单、点卡片切窗口、geometry 持久化
config.py           config.json 读写 + DEFAULTS + 几何持久化（唯一配置入口）
core/state.py       AgentSession —— UI 与适配器之间唯一的共享数据模型 + 格式化/裁剪/payload 取值工具
                    + 会话去留判定 dropped() / 进程退出态 dead_detail()；状态表 STATE_META 的 label 是 i18n key
core/i18n.py        界面语言：系统语言检测（auto，检测不到用英文）+ t(key, **kw) + 配置持久化
core/locales/       每种语言一个 JSON 文案包（zh-CN.json / en.json），同 key 不同文案
core/winui.py       Windows 呈现层：DPI 感知、设计像素↔设备像素换算、DWM 圆角、focus_process() 拉起宿主窗口
core/pid.py         跨平台进程存活判定（Windows 走 ctypes OpenProcess）+ Liveness 结果缓存
                    + 进程事实：processes() 快照 / console_of() 控制台与 tab 标题 / cwd_of() 读 PEB 拿工作目录
core/fs.py          目录列举 dirs() + 卡片标题 project_name() + 适配器 home 探测 homes()
core/jsonl.py       JsonlTailer：按字节偏移增量读 jsonl，处理残行/截断/轮转
sources/base.py     AgentSource 适配器契约（id / label / scan(cache)）
sources/__init__.py SOURCES 注册表 + scan_all()（隔离单个适配器的异常）
sources/qoder_cli.py qodercli 日志解析：Run 发现、SegmentStream 状态推导、TranscriptFeed 任务清单
sources/vscode_agent.py Copilot/agent-host 解析：会话发现、EventStream 状态推导、workspace.yaml 身份
sources/opencode.py opencode CLI 解析：只读 SQLite（session_v2 / session_message）、按 time_updated 增量、按 PEB 工作目录认窗口
```

数据流是单向的：`sources.scan_all() -> list[AgentSession] -> Board._render()`。
UI 不认识任何 CLI 特有的字段；适配器不认识 tkinter。

## 硬约束

### 1. 严格 DRY

- **状态词汇表只有一处**：`core/state.STATE_META`（label key / 颜色 / 是否需要用户）。新增状态就在那里加一行，
  并在每个 `core/locales/<code>.json` 里加一条同名 key 的文案；不要在适配器或卡片里再写一套 `if state == ...` 的颜色/文案映射。
- **所有界面文案都要走 i18n**：`core/locales/` 下每种语言一个 JSON，两份 key 集合必须一致，代码里只 `i18n.t(key, ...)`，
  不要硬编码中英文字面量（标识符仍用英文）。`auto` 检测系统显示语言，检测不到回退英文，右键菜单可切换，选择存 `config.json` 的 `language`。
- **文本截断与时长/相对时间/用量格式化只有一处**：`clip` / `fmt_duration` / `fmt_age` / `fmt_tokens` / `display_width`（`core/state.py`）。
  board 与 `render_text` 都必须复用它们，不要各写一份。
- **文件系统遍历、时间戳解析、payload 取值等小工具只写一次**；发现第二个调用点就提到 `core/`，
  而不是在另一个适配器里复制。
- **配置项只在 `config.DEFAULTS` 声明**，读取一律 `config.settings[...]`；不要硬编码默认值或再引入模块级常量。
- 重复出现三次以上的同构分支才允许抽象；两处复制优于提前造基类/注册钩子。
- 删除即删除：改代码时同步删掉不再使用的字段、`_` 前缀私有函数、注释掉的旧逻辑与 re-export 兼容层。

### 2. 严格面向可扩展（后续要接其它 agent CLI）

- **加一个 CLI = 新增 `sources/xxx.py` + 在 `SOURCES` 里注册一行**。
  如果这件事需要改 `board.py`、`config.py` 或 `core/state.py` 的字段，说明抽象漏了——先修边界，别加 if 分支。
  三个适配器共用的那几件已经在 `core/` 里了，别在适配器里重写：home 探测 `fs.homes(config_key, env_vars, defaults, marker)`、
  去留判定 `state.dropped(age, alive=None)`、退出态文案 `state.dead_detail(age)`。剩下的就只有"它的日志/库长什么样"和"怎么推状态"。
  home 的配置键必须叫 `<source>_homes` —— `board.py --home` 与 `config.load()` 都按这个后缀工作，新适配器零改动继承。
- 所有 CLI 特有的解析逻辑留在适配器内部；`AgentSession` 只放跨 CLI 通用的语义字段。
  确实只有某个 CLI 才有的信息，用 `detail` / `activity` 承载，不要给 dataclass 加专用字段。
- 每个适配器自己持有 `cache`（tailers、已解析流、PID 结果、manifest），跨轮次复用；
  适配器必须能从空 cache 冷启动，且单实例抛异常不能把整块看板清空。
- 窗口尺寸、卡片高度、配色键、透明度都走 `config.json`，不要为了新 UI 把常量写死在 board 里。

## 编码约定

- `from __future__ import annotations`，类型标注用新语法（`list[str]`、`X | None`）。
- 注释默认不写；只在解释非显然的原因时写一行（例如下面这些坑）。文件 docstring 说明该模块的边界即可。
- 界面文案走 i18n，每种语言一个 JSON；标识符英文。
- 只做被要求的事：不顺手重构、不给不可能发生的情况加兜底、不写 README 或分析文档（除非要求）。

## 已知坑（改动前先看）

- **Windows 上绝不能用 `os.kill(pid, 0)` 判存活**：CPython 对非 CTRL 信号会调 `TerminateProcess`，会杀掉被监视的 CLI。走 `core/pid.is_alive`。
- 段落/转录的 tailer 必须按**文件路径**而非 run key 做清理，否则每轮从头重读整份日志，计数器会成倍翻。
- **发现阶段的目录列举按目录自身 mtime 缓存**（`_segment_names`）：NTFS 上目录 mtime 只在**增删条目**时移动，
  所以缓存的只是"有哪些文件"，**最新那个文件仍必须每轮 stat** —— 追加写只改文件 mtime/size，而 `age` 闸门和 tailer 都读它们。
  把 stat 也省掉就会让活跃会话看起来停止活动、`dropped()` 把它踢上看板之外；缓存靠 400 条上限自清，别改成无限增长。
- 首次读取要按 `JsonlTailer.at_start` + `full_read_kb` 从头读，否则 320KB 尾部窗口会丢掉会话开头的
  `session.config.loaded` / 首个 prompt，模型、分支、任务全空。
- 任务清单只接受 `tool_use_id` 命中先前 `TaskCreate` 的 `tool_result`，否则探针/自查输出里的
  `Task #1 created successfully: ...` 会被当成真任务。
- 清单属于创建它的那次请求：全部 `completed` 后再来一条真实 user prompt 就清空（`_prompt_text` 过滤
  `<local-command-caveat>` / `<command-message>` 这类注入噪声）。否则上一件事的 "任务 4/4" 会一直挂在卡片上，
  而当前请求根本没建过任务。还有 pending 的不清，允许跨请求续用。
- **Qoder CN 桌面版把 CLI 当 `--sdk` worker 拉起**：一次启动的所有会话共用**同一个 run_id**（manifest 在 run 级，
  `_manifest` 已经读过），每个会话目录只分到一段日志切片（seq 不从 1 开始，只有一份含 `session.config.loaded`
  的 `interactive:false`），且 manifest 的 `cwd` 是 `D:\Program Files\Qoder CN` 这种安装目录。按 `interactive` 过滤
  挡不住全部，照单全收又会刷出四五张空卡。现在靠 `argv` 里的 `--sdk` 分流：**每个 session 目录出一张卡**，
  真实 cwd 只从 `hook.started` 的 `hook_input.cwd` 取（`workspace_dirs` 同值），别去解码 project_id —— `--`
  不是单纯的路径分隔符。worker 的 pid 所有会话共享，判存活还要叠加该日志 mtime 在 `live_minutes` 内；
  只有 hook 的切片和 `Session title:` 标题生成会话要滤掉。worker 没有控制台，点击聚焦靠 `core/winui._host_window`
  沿父进程链命中 `Qoder CN` 主窗口（实测命中），多会话共用一扇窗口、不切标签。
- **`turn_stats` 属于上一次回合，新回合必须清掉**：`SegmentStream` 里 `turn.finished` 写入"上轮 N 步 / 时长"，
  若 `turn.started` / `input.prompt.received` 不清，新回合在两次事件之间的安静期（实测 33 秒）会被 `derive()`
  判成 `idle`，还挂着上一轮的统计——用户看到的就是"活动中的会话显示空闲待输入"。用 `in_turn` 标记，回合中
  即使没有更具体的信号也返回 `model`，别让 `turn_stats` 抢在前面。
- **VS Code 会话的身份不能从 events 日志里取**：`~/.copilot/session-state/<id>/events.jsonl` 动辄几十 MB，
  尾部窗口永远读不到头部，cwd / 标题 / 起始时间只能来自同目录的 `workspace.yaml`。
  存活看 `inuse.<pid>.lock` 的文件名 PID —— 进程退出后锁文件不会清理，残留必须过 `Liveness` 才算数；
  同级还有 `.session-operation-locks/` 之类的非会话目录，只因为缺 `events.jsonl` 才被 stat 探步跳过，别改成按名字猜。
- Copilot 在 autopilot 下 `permission.requested` 之后几十毫秒就跟着 `permission.completed`（host_policy 代发），
  两轮日志会交错出现；按 `requestId` 配对即可，别把 awaiting 当噪声删掉——真正的授权请求只有 requested 没有 completed。
- **用量走 `AgentSession.usage`（适配器排好版的一句话），不要加 token 数值字段**：qodercli 的
  `turn.finished.input_tokens` 恒为 0（本机 1130 轮全零），只有 Copilot 的 `session.usage_record` 有数；
  空串时 board 那一行整段不画，所以不会一半卡片空着。
  `usage_record.inputTokens` 就是当前上下文规模（单调涨，压缩后回落），`maxPromptTokens` 是上限，取百分比。
  别把它塞进 `detail` 尾巴，那行要先给工具名留位置，用量会被剪掉。
- **credits 只能从 `session.usage_checkpoint` 的 `accountingSnapshot.totalPremiumRequestCost` 拿**，
  而且它不在尾部窗口里：checkpoint 十几分钟才落一条，本机实测间隔 200~1600 条记录（约 1~8MB，
  最长一条距文件尾 10MB+），320KB 窗口基本扫不到。所以 `_credits()` 记 **恢复偏移**而不是节流时间：
  冷启动 `_newest_credits()` 从末尾按 `_CREDIT_SCAN` 反向找（找不到就 ×4 扩大，实测 107MB 文件 ~11ms），
  之后每 `_CREDIT_STEP` 只读偏移之后的新字节，并把半行尾（`_settled()`）留到下一轮重读 ——
  固定反向窗口会让跑了很久的会话突然掉「额度」，别退回那种写法。
  `usage_record.cost` 恒为 1.0，是占位值，别拿它当计费。
  checkpoint 里还有 `totalUserRequests` / `tokenDetails`（input/cache_read/cache_write/output）/
  `totalApiDurationMs` / `codeChanges.linesAdded|Removed`，这些是**全会话累计**，不受窗口影响。
- **opencode 的状态只能从 SQLite 推**（`~/.local/share/opencode/opencode.db`，`journal_mode=wal`）：
  `log/opencode.log` 是 `key=value` 文本，没有 session id / prompt / 工具名 / token，什么也推不出来。
  只读连接用 `mode=ro` + `PRAGMA query_only=1`（实测服务端持续写时 300 次查询 0 错误、中位 0.11ms）；
  **再加 `immutable=1` 会静默返回 0 行** —— 已提交的行还在 -wal 里，别为了"更快"把它打开。
- **工具 part 存在哪张表取决于 opencode 版本，别只读 `session_message.data.content`**：2.4.0 起 part 移到独立的
  `session_part` 表（message 的 `content` 恒为空），2.3.2 是过渡版两处都写，更早的版本只在 content。按
  `session_v2.version` 检测：≥2.4 以 table 为准，<2.4 把 content 和 table 按 part id 合并（table 覆盖同 id），
  表不存在就只用 content——只认一种来源会让另一版本的会话工具数归零，卡在「请求模型中」。
  **part 在 message 行落库之后才被改写**（running→completed），所以已读过的 message 也要定期从 table 刷新 parts；
  增量游标仍是 `time_updated` 而不是 `seq`，计数按 part 的 `id` 和上一份同 seq 记录比对（`_Fold.feed`），否则 `工具` 成倍翻。
  `question` 工具等待用户回答时状态是 `running`（既不是 pending 也还没 completed），名字就叫 `question`，
  所以 `asking` 要认 name=question + pending/running，漏掉就会显示「请求模型中」。
  回合结束的信号是 `type='idle'` 行，不是 `session_v2.time_idle`；
  `session_v2.title` 实测恒为 NULL，标题用 `slug`；`parent_id IS NULL` 之外的子会话不单独出卡片（父会话的 `subtask` 行只用于计数文案，
  子代理是否还在跑要按 `parent_id` 查子会话的 `time_suspended`，见下面子代理那条）。
- **会话是否活着用 `time_suspended` + 活动新鲜度，`别用窗口匹配推`**：`time_suspended` 是
  opencode 的执行占用标记（`SessionStore.claim`，`listSuspended` 用 `time_suspended is not null`），
  但它在**回合结束进入空闲时会被清空**——只用 claim 会让空闲的当前会话变灰。所以还要叠加
  `time_updated` 新鲜度（`config.live_minutes`，默认 30 分钟）覆盖空闲间隙；两者都没有的才是关闭的历史。
  反过来，异常退出时 claim **不会被清空**：残留的 claim 会让会话一直是 `alive`，直到 `max_idle_minutes`
  （6 小时）才被 `dropped()` 清掉，期间还显示成活跃状态。`_alive()` 因此还要求 claim 自身落在
  `max_claim_minutes`（默认 120 分钟）内，超了就按已退出处理，走 `recent_dead_minutes` 的宽限。
  别用 `live_minutes` 当 claim 上限——长时间跑的工具调用可能几小时不写库，会误杀活跃会话。
  窗口 pid 只用于点击聚焦——location switch 会改 `session_v2.directory` 而不动 TUI 的 PEB cwd，
  多个活跃会话也会抢同一扇窗口，所以抢不到窗口的会话照样可以是活跃彩色（只是 `pid=0` 不可点击）。
- **同窗口多会话的 pid 兜底要分给所有未认领会话，不是只分剩余的**：`_claim` 里第一个匹配会话 `free.remove(pid)` 后，
  一个 pid 多会话时 `free` 立刻空掉，兜底 `if free:` 不生效，后面的会话全拿 `pid=0` 点不动（用户报过「非在前会话切不到窗口」就是这）。
  保留一份完整 `all_pids`，把未认领的会话都映射到它——它们本来就是同一 TUI 的标签页，共享 pid 是对的。
- **子代理是否活跃只看 `time_suspended`，不能用 `time_updated` 新鲜度**：父会话消息流结束在 `idle` 时，子代理可能还在后台跑，
  活动全在子会话自己的记录里；按 `parent_id` 统计带 claim 的子会话，父会话 `idle`/`starting` 且有活跃子会话就翻成 `subagent`（青色点，
  别和 `idle` 的绿撞色，否则远看会误判为空闲）。子代理跑完 claim 清空但 `time_updated` 还要过 `live_minutes` 才过期——
  用新鲜度会把结束的子代理一直算成活跃，状态卡在「子代理运行中」下不来（用户报过这个）；父会话才需要新鲜度覆盖空闲间隙，子代理没有「开着等输入」的空档。
- **opencode 库里没有任何 pid**，控制台标题又恒为 `'OpenCode'`（改名的是 tab，不是控制台），
  所以只能拿 `pid.cwd_of(candidate)`（读 PEB）等于 `session_v2.directory` 来认窗口；`opencode serve` 没有控制台，天然被排除。
  `AttachConsole` 只在**无控制台**的进程里成功：HUD（pythonw）正常，`python board.py --once` 在终端里跑会全部退化 `pid=0`，
  那是预期行为，别用 `--once` 验证点击链路。授权请求不落库，`awaiting` 只能靠"pending 的 tool part 卡住超过 6s"推断，
  阈值是为了不在正常的 pending→running 毫秒级抖动上闪红；回合结束等下一条输入是 `idle`（绿），不是 `awaiting`。
- **卡片每行的截断预算是像素，不是字符格**：`clip(text, px, font.measure)` —— `core/state.py` 的 `clip` 默认按
  display_width 数字格（给 `--once` 文本用），board 必须传自己那行的字体和 `row_px(card_w, name)` 量出的像素。
  早先用「YaHei 粗体 M 的宽度」当格数（19px）去预算混排文本，实际平均一个字 ~9px，每行只画到半宽就加省略号，
  右边永远空一截；`width` 默认值也是被这个幽灵宽度撑大的。行框右边缘统一留 `EDGE` 设计像素。
  窗口宽度变了必须重画（`Card.box` 记下上次用的宽度），否则裁剪预算是旧的。
- **窗口必须 DPI 感知（`winui.become_aware()`，在 `tk.Tk()` 之前）且不能用 `-transparentcolor`**：色键是二值的，
  边缘只能整像素取舍，圆角和任何斜线必然锯齿；不感知的进程还会被桌面合成器整窗拉伸（150% 下文字发虚带彩边）。
  圆角交给 `winui.round_corners()`（DWM，Win11 起有效，Win10 退化为直角）。
- **所有像素常量都是"设计像素"**，进 Tk 之前必须过 `winui.px()`，从 Tk 读回来（位置、尺寸、屏幕宽高）用 `winui.device()`；
  `geometry.json`/`config.json` 里存的也是设计像素。canvas 的 `width/height` 选项是请求尺寸，改它等于改布局，缩放要用 `place`。
- **点卡片切窗口以控制台为准，父链只是兜底**：`qoderclicn-*.exe`、`copilot-runtime.exe` 自己不拥有窗口（按 pid 匹配 `EnumWindows` 一个都没有）。
  先 `pid.console_of(pid)` 拿控制台桩窗口（`PseudoConsoleWindow`，0×0 无标题，**本身绝不能当目标**），它的 `GetParent()` 才是
  `CASCADIA_HOSTING_WINDOW_CLASS`，且必须回到 `_app_windows()` 的结果里核对一遍再用。
  反过来按父链找会踩坑：同一个会话里的 shell 可以另外拥有**别人的**控制台窗口（实测 `cmd.exe - opencode upgrade` 那种），
  链上 pid 一撞就命中错窗口，窗口标题变成那条命令行，于是活跃 tab 的卡片也被误判成"显示的是别的标签页"。
  没有控制台的会话（VS Code 宿主）才走父链；链走到 `explorer.exe` 会命中桌面，所以必须按窗口类名过滤。
  两种都找不到（如 `start` 分离的会话、进程已退出）—— 蜂鸣 + 提示就是正确结局。
- **HUD 不是前台进程，`SetForegroundWindow` 会被系统拒绝**（只闪任务栏图标）：先 `AttachThreadInput` 到当前前台线程再调。
- **点卡片时前台窗口已经是 HUD 自己**（Tk 会激活无边框窗口），所以"当前在看哪个窗口"不能问 `GetForegroundWindow`：
  `_animate` 每帧 `winui.track_foreground()` 记录最近 8 个非 HUD 前台窗口，`_active_window()` 才是判断依据；
  回退目标 `back` 在拉起时从历史里取（此时新窗口还没进历史）。二次点击同一会话 = 回到 `back`，原本最小化的窗口重新最小化。
  即使 `_active_window()` 已经是目标，也**照样要 `_set_foreground`** —— 早退会让"HUD 抢走激活"那种情况点不动（就是用户报的 bug）。
- **`_jump` 按 session key 记，不是按 hwnd**：两个会话共用一个终端窗口时，按 hwnd 记会让第二张卡片变成"回退"而不是"拉起"。
  一跳即止，不做栈。Windows Terminal 一个窗口只有一个 HWND，标签页没有可寻址句柄，所以**不切 tab**，
  只用 `pid.console_of(pid)` 的标题（`AttachConsole` 要无控制台的调用方，pythonw 正好符合；控制台没人改名时它返回可执行文件路径，
  含 `\\` 就当空）和窗口标题比 —— 窗口标题只等于**当前激活那个 tab** 的标题，包含即算对上了（终端可能追加自己的后缀）。
- **点击反馈要在底栏驻留**（`_note` + `FOOTER_HOLD`）：`_render` 每 1.5s 覆盖底栏，直接 `configure` 的消息根本读不到。
- Tk 画布图元（oval/line/polygon）没有抗锯齿：圆形状态点靠 `mix()` 叠两圈中间色近似 AA，缩放把手改用点阵方块。
- **菜单里"当前值"不能用 radiobutton 的勾选态表示**：Windows 会把带勾的那条画成选中块，鼠标没移上去也亮着，
  读起来永远像在悬停。`indicatoron=False` 关掉勾选态，当前值用 `accelerator` 列的 `●`/`○` 表示——
  Tk 把 cascade 箭头也当 accelerator 计宽（tkWinMenu.c `GetMenuAccelGeometry`），两者同列起笔，
  `activeborderwidth=0` 时 accelerator 顶到条目右缘，圆点与箭头对齐；切换时 `entryconfigure(accelerator=...)` 刷新。
- 系统 DPI 非 100% 时，任何截图/鼠标自动化脚本要先 `SetProcessDPIAware()`，否则量到的是虚拟化的逻辑坐标；
  同一个 `GetWindowRect` 在感知/不感知的探针进程里读数不同，别拿它们互相印证。
- 用 bash `&` 拉起的子进程会随命令结束被回收；常驻窗口用 `Start-Process pythonw board.py`。

## 验证

改完至少跑：

```bash
python -m py_compile board.py config.py core/*.py sources/*.py
python board.py --once
python board.py --once --repeat    # 连刷两轮，计数必须稳定
```

改 i18n 时还要确认两份 `core/locales/*.json` key 集合一致、代码用到的 key 都在表里；切语言（右键菜单）后菜单文案、
卡片状态/详情、底栏都要跟着变，选择要写回 `config.json` 的 `language`。`display_width` 不要跟着语言切成单宽——
它算的是终端里字符的实际宽度（英文界面里的中文 prompt 仍占两格），与界面语言无关。

涉及绘制/交互时再用截图确认（悬停不透明、圆角、自动高度、锁切换、拖拽缩放、geometry 落盘），
跑完清理 `shot.ps1`、临时 png、`__pycache__`、测试产生的 `geometry.json`。
