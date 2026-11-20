# agent-board

English | [简体中文](README.zh-CN.md)

An always-on-top, transparent floating window that shows the live progress of your open agent CLI sessions: current state, active request, task checklist, tool-call turns, and context usage.

Built on the Python standard library only — zero dependencies, no network, no build step. All session data is read locally; nothing is uploaded.

## Supported CLIs

| Source | Data source |
| --- | --- |
| qodercli | jsonl logs under `~/.qoder-cn` / `~/.qoder` |
| VS Code Copilot | `events.jsonl` + `workspace.yaml` in `~/.copilot/session-state` |
| opencode | `~/.local/share/opencode/opencode.db` (read-only SQLite) |

The opencode adapter is developed and verified against **opencode v2.3.2**. Its SQLite schema is private and may change in later releases.

Identity probing, session retention, and process-liveness checks are shared across all three via `core/`; CLI-specific parsing stays inside each adapter.

## Running

On Windows, use the full floating-window experience:

```
start.cmd                :: launch with pythonw in the background, no console window
python board.py          :: launch with a console, useful for debugging
```

Headless text mode (any platform):

```
python board.py --once
python board.py --once --repeat
python board.py --once --interval-ms 800
python board.py --once --home <dir>
```

`--home` is appended to every `*_homes` key; each adapter decides via its own marker whether to accept a directory, and foreign ones are ignored automatically.

> Note: when `--once` runs in a terminal, click-to-focus degrades (all pids become 0). `AttachConsole` requires a caller with no console; only the pythonw process started by `start.cmd` has the full click chain. This is expected — don't use `--once` to verify clicking.

## Interaction

- Drag the header to move the window; drag the bottom-right grip to resize
- Double-click the header to lock / unlock opacity
- Hover to make the window opaque; it returns to the idle opacity when you leave
- Click a card to bring the terminal / editor hosting that session to the front; click the same card again to return to the previous window
- Right-click menu:
  - Always on top, auto height, keep recently exited sessions, click card to focus its window
  - Idle-opacity presets
  - Refresh now, copy top session's directory, restart board, quit

Cards are sorted by "needs your attention" first, then by most recent activity. States that need you (waiting for your answer, awaiting authorization, last turn failed) blink.

## States

| State | Meaning | Needs you |
| --- | --- | --- |
| Waiting for your answer | The session asked you a question via the question tool | Yes |
| Awaiting authorization | A tool call is stuck on a permission request | Yes |
| Last turn failed | The previous turn ended in failure | Yes |
| Compacting context | Context is being compacted | No |
| Subagent running | A subtask / fork is executing | No |
| Running tool | A tool is running | No |
| Requesting model | The model is generating a response | No |
| Running hook | A hook is executing | No |
| Starting | Session just discovered, no events read yet | No |
| Idle, awaiting input | The last turn ended; waiting for your next input | No |

Idle is green rather than gray: a finished turn waiting for input is a normal state you can spot from across the room, and gray is invisible at 38% opacity.

## Configuration

All tunables live in `config.json`; deleting a key restores the default in `config.py`'s `DEFAULTS`. Common ones:

| Key | Default | Description |
| --- | --- | --- |
| `qoder_homes` / `copilot_homes` / `opencode_homes` | `[]` | Extra directories to scan, ahead of auto-detection |
| `poll_interval_ms` | `1500` | Poll interval in milliseconds |
| `tail_kb` | `320` | Tail window for reading logs |
| `full_read_kb` | `8192` | Logs smaller than this are read from the top on first look, so nothing at the start is missed |
| `max_cards` | `5` | Maximum number of cards shown |
| `live_minutes` | `30` | Without an execution claim, a session still counts as live for this long |
| `max_idle_minutes` | `360` | Silence after which a session leaves the board |
| `include_recent_dead` / `recent_dead_minutes` | `true` / `10` | Whether to keep recently exited sessions |
| `alpha_idle` / `alpha_hover` | `0.38` / `1.0` | Idle / hover opacity |
| `idle_presets` | `[0.22, 0.38, 0.55, 0.75]` | Opacity presets in the right-click menu |
| `topmost` | `true` | Always on top |
| `click_to_focus` | `true` | Click a card to focus its window |
| `width` / `height` | `360` / `340` | Window size in design pixels (DPI-adjusted automatically) |
| `min_width` / `min_height` | `300` / `170` | Minimum size when resizing |
| `margin_right` / `margin_top` | `24` / `48` | Distance from screen edges |

`geometry.json` stores window position and size; the program writes it back itself, so you don't need to edit it.

## Development & extending

Architecture boundaries, hard constraints, known pitfalls, and verification steps are all in `AGENTS.md`. Key points:

- Data flows one way: `sources.scan_all() → list[AgentSession] → Board._render()`. The UI knows nothing about CLI-specific fields, and adapters know nothing about tkinter.
- To add a CLI: create `sources/xxx.py` implementing `id / label / scan(cache)`, then register one line in `SOURCES` in `sources/__init__.py`. No changes needed in `board.py`, `config.py`, or `core/state.py`. Name the home config key `<source>_homes`; `--home` and `config.load()` pick it up automatically.
- The state vocabulary lives only in `core/state.STATE_META`; text clipping and time / token formatting live only in `core/state.py`.

Verification:

```
python -m py_compile board.py config.py core/*.py sources/*.py
python board.py --once
python board.py --once --repeat        :: two consecutive polls, counters must stay stable
```

## Limitations

- **Windows only**: `winui` / `pid` depend on Windows APIs (DPI awareness, DWM rounded corners, `AttachConsole`, `EnumWindows`, PEB reads). The code won't crash on other platforms, but only the `--once` text mode is meaningful there.
- Adapters parse each CLI's private log / database formats, not public APIs. An upstream CLI upgrade may break the corresponding adapter and require maintenance.

## License

MIT, see `LICENSE`.
