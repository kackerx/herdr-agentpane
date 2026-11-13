# herdr-agentpane

A [herdr](https://herdr.dev) plugin: a panel on the right edge of the tab for the focused
[Claude Code](https://claude.com/claude-code) agent. Keyboard (vim keys) and mouse.

| Page | What it shows / does |
|---|---|
| **Context** | context usage by category (headless `/context`, no API call), the agent's memory and instruction files — `enter` edits one in a popup |
| **Agents** | the agents of this herdr workspace, plus background or out-of-herdr sessions in the same worktree, as cards with avatars (kitty graphics), status, branch, model and context bar. Click once to select, again to open: an agent in this tab gets focus, any other opens in a popup (`ctrl+q q` closes it). `t` pin, `a` change avatar, `x` stop, right click / `m` menu (also **Handoff**: sends `/handoff` to a foreground Claude Code agent when a `handoff` skill is installed) |
| **Plugin** | Claude Code plugins: toggle per project, `D` uninstall, `U` update, `r` reload |
| **MCP** | MCP servers of the session: toggled with `/mcp enable` / `/mcp disable` |
| **Skills** | skills: `space` cycles on → name-only → user-only → off, `enter` edits `SKILL.md` |

Keys: `j/k` move · `gg/G` · `ctrl-d/u` · `h/l` or `tab/shift-tab` switch page · `1-5` jump · `/` filter ·
`t` filter by type · `r` reload plugins · `u` refresh usage · `q` close. The panel remembers page, selection and width.

Switches go only through Claude Code's own paths: slash commands sent to the agent, or `<cwd>/.claude/settings.local.json`
(the file `/skills` and `/plugin` write). Your user settings are never written.

## Requirements

- herdr ≥ 0.9.5, macOS or Linux
- `python3` (standard library only) and `claude` on `PATH`
- avatars: an outer terminal with kitty graphics Unicode placeholders (kitty, Ghostty) and `kitty_graphics = true`
  under `[experimental]` in herdr's `config.toml`; without them the cards show no picture
- optional: [agmux](https://github.com/kackerx/agmux) — when the focused pane has no agent, the panel turns into `agmux recall`
  to resume a past session there; pins and avatar choices are copied from agmux once on first run

## Install

```bash
herdr plugin install kackerx/herdr-agentpane
```

or, from a checkout: `herdr plugin link /path/to/herdr-agentpane`.

Bind the toggle in herdr's `config.toml` (plugins cannot bind keys themselves):

```toml
[[keys.command]]
key = "prefix+shift+b"
type = "plugin_action"
command = "kackerx.agentpane.toggle"
description = "agent pane"
```

Press it again (or `q` in the panel) to close.

## Configuration

`config.json` in the plugin's config directory (`$HERDR_PLUGIN_CONFIG_DIR`, by default
`~/.config/herdr/plugins/config/kackerx.agentpane/`):

```json
{
  "width": 52,
  "remember": true
}
```

- `width`: inner width in columns, or a share of the tab such as `"30%"`
- `remember`: after you drag the panel wider or narrower it reopens at that width; `false` always uses `width`

Your own avatar sets go in `avatars/<id>/{idle,think,hi}/*.png` under the same directory and take precedence
over the bundled ones with the same id.

State (remembered width, page, pins, avatar choices, caches, `panel.log`) lives in `$HERDR_PLUGIN_STATE_DIR`.

## License

MIT. Bundled avatars: see [avatars/CREDITS.md](avatars/CREDITS.md).
