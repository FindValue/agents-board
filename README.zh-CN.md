# agent-board

[English](README.md) | 简体中文

常驻置顶的透明悬浮窗，实时展示当前打开的 agent CLI 会话进展：状态、当前请求、任务清单、工具调用轮次、上下文用量。

纯 Python 标准库实现——零依赖、零网络、无构建。所有会话数据都在本地读取，不上传任何内容。

## 支持的 CLI

| 来源 | 数据来源 |
| --- | --- |
| qodercli | `~/.qoder-cn` / `~/.qoder` 下的 jsonl 日志 |
| VS Code Copilot | `~/.copilot/session-state` 的 `events.jsonl` + `workspace.yaml` |
| opencode | `~/.local/share/opencode/opencode.db`（只读 SQLite） |

opencode 适配器基于 **opencode v2.3.2** 开发与验证。它的 SQLite 结构是私有的，后续版本可能变化。

三者的身份探测、去留判定、进程存活检查都复用 `core/` 里的同一套工具；各 CLI 特有的解析逻辑留在自己的适配器里。

## 运行

Windows 下推荐使用完整的悬浮窗交互：

```
start.cmd                :: pythonw 后台启动，无控制台窗口
python board.py          :: 带控制台启动，便于调试
```

无界面文本模式（任何平台）：

```
python board.py --once
python board.py --once --repeat
python board.py --once --interval-ms 800
python board.py --once --home <dir>
```

`--home` 会追加到所有 `*_homes`，各适配器用自己的 marker 决定认不认，不是它的目录会被自动忽略。

> 注意：`--once` 在终端里运行时，点击卡片聚焦窗口会退化（pid 全为 0）。`AttachConsole` 要求调用方没有控制台，`start.cmd` 启动的 pythonw 才有完整的点击链路。这是预期行为，不要拿 `--once` 验证点击。

## 交互

- 拖动顶部标题栏移动窗口，拖动右下角把手缩放
- 双击标题栏锁定 / 解锁透明度
- 鼠标悬停时窗口变不透明，移开恢复常态透明度
- 点卡片：把承载该会话的终端 / 编辑器窗口切到前台；再点同一张卡片回到上一个窗口
- 右键菜单：
  - 永远置顶、高度自适应、保留刚退出的会话、点击卡片切到它的窗口
  - 常态透明度档位
  - 立即刷新、复制最上方会话目录、重启看板、退出

卡片按「需要你处理」优先、其次最近活动排序。需要用户介入的状态（等你回答、等待你授权、上一轮异常）会闪烁提示。

## 状态

| 状态 | 含义 | 需要你处理 |
| --- | --- | --- |
| 等你回答 | 会话用 question 工具向你提问 | 是 |
| 等待你授权 | 工具调用卡在权限请求上 | 是 |
| 上一轮异常 | 上一轮以失败告终 | 是 |
| 压缩上下文 | 正在压缩上下文 | 否 |
| 子代理运行中 | 子任务 / fork 正在执行 | 否 |
| 执行工具 | 工具正在运行 | 否 |
| 请求模型中 | 模型正在生成回复 | 否 |
| 运行钩子 | 钩子正在执行 | 否 |
| 启动中 | 会话刚被发现，还没读到事件 | 否 |
| 空闲待输入 | 上一轮已结束，等待你的下一条输入 | 否 |

空闲是绿色而不是灰色：回合结束等待输入是你在远处也能一眼看到的正常状态，而且 38% 透明度下灰色根本看不见。

## 配置

可调项都在 `config.json`；删掉某项即回到 `config.py` 的 `DEFAULTS`。常用项：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `qoder_homes` / `copilot_homes` / `opencode_homes` | `[]` | 额外扫描的目录，优先于自动探测 |
| `poll_interval_ms` | `1500` | 轮询间隔（毫秒） |
| `tail_kb` | `320` | 日志尾部读取窗口 |
| `full_read_kb` | `8192` | 小于该尺寸的日志首次从头读，避免丢掉开头 |
| `max_cards` | `5` | 最多显示几张卡片 |
| `live_minutes` | `30` | 无执行标记时，会话在这段时间内仍算活跃 |
| `max_idle_minutes` | `360` | 多久无活动就移出看板 |
| `include_recent_dead` / `recent_dead_minutes` | `true` / `10` | 是否保留刚退出的会话 |
| `alpha_idle` / `alpha_hover` | `0.38` / `1.0` | 常态 / 悬停透明度 |
| `idle_presets` | `[0.22, 0.38, 0.55, 0.75]` | 右键菜单里的透明度档位 |
| `topmost` | `true` | 永远置顶 |
| `click_to_focus` | `true` | 点卡片切到它的窗口 |
| `width` / `height` | `360` / `340` | 窗口尺寸（设计像素，自动换算 DPI） |
| `min_width` / `min_height` | `300` / `170` | 缩放下限 |
| `margin_right` / `margin_top` | `24` / `48` | 距离屏幕边缘的间距 |

`geometry.json` 保存窗口位置和尺寸，由程序自己写回，不需要手动编辑。

## 开发与扩展

架构边界、硬约束、已知坑和验证方法都写在 `AGENTS.md`。要点：

- 数据流单向：`sources.scan_all() → list[AgentSession] → Board._render()`。UI 不认识任何 CLI 特有字段，适配器不认识 tkinter。
- 加一个 CLI：新增 `sources/xxx.py`，实现 `id / label / scan(cache)`，再到 `sources/__init__.py` 的 `SOURCES` 注册一行。不需要改 `board.py`、`config.py` 或 `core/state.py`。home 配置键命名为 `<source>_homes`，`--home` 和 `config.load()` 会自动生效。
- 状态词汇表只在 `core/state.STATE_META`；文本截断和时间 / 用量格式化只在 `core/state.py`。

验证：

```
python -m py_compile board.py config.py core/*.py sources/*.py
python board.py --once
python board.py --once --repeat        :: 连刷两轮，计数必须稳定
```

## 限制

- **仅限 Windows**：`winui` / `pid` 依赖 Windows API（DPI 感知、DWM 圆角、`AttachConsole`、`EnumWindows`、PEB 读取）。非 Windows 上代码不会崩，但实际只有 `--once` 文本模式有意义。
- 适配器解析的是各 CLI 的私有日志 / 数据库格式，不是公开 API。上游 CLI 升级可能导致对应适配器失效，需要跟进维护。

## 许可

MIT，见 `LICENSE`。
