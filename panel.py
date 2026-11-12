#!/usr/bin/env python3
"""右侧面板: 当前 agent (Claude Code) 会话的 Plugin / MCP / Skills / Context (占用 + agents + 记忆文件), 能开关, 能 reload。

数据 (只读, 全是 Claude Code 自己落盘或自己会算的东西, 不碰 herdr 侧栏):
  会话    pane 前台进程的 pid → `claude agents --json` 找到 sessionId / cwd
  加载    会话 transcript (~/.claude/projects/*/<sessionId>.jsonl) 里的 skill_listing / deferred_tools_delta /
          mcp_instructions_delta / instructions 附件
  占用    Context 页 (原 Usage): `claude -p /context --resume <sid> --fork-session --no-session-persistence` (2026-11-09 实测:
          不调 API、不落盘、约 4 秒, 输出和交互式 /context 同一份 markdown; 按模型默认值估算, 不含 claude.ai 连接器)
动作 (2026-11-09 在一次性会话里逐条实测过):
  MCP     给 agent 发 `/mcp disable|enable <名>` (官方做法, 自己落盘; 下一轮起工具列表里就没了, 即时)
  skill   写 <cwd>/.claude/settings.local.json 的 skillOverrides (调用即刻被拦; 已在上下文里的列表要新会话才消失)
  plugin  写同一文件的 enabledPlugins, 然后要 reload (13 plugins/18 skills/2 MCP → 12/12/1, 该插件 29 个工具下一轮移除)
  reload  给 agent 发 `/reload-plugins`
  Context 和 Skills 回车 = 浮窗 (overlay) 里用 nvim 编辑; Skills 只有 user/project 的有文件, 插件/内置的提示不可编辑
键位 (vim): j/k 移动 · gg/G 首尾 · ctrl-d/u 半页 · h/l (H/L) tab/shift-tab 切页 · t 按类型过滤 (all→built-in→user→project→plugin…) · 1-5 直达 · space/enter/x 开关 (Skills 按 on→name-only→user-only→off 轮转) · D 删除 (y 确认) · U 更新插件 · / 过滤 · r reload · u 刷新占用 · q 关闭
鼠标: 点页签切页, 点复选框开关, 点行选中, 点 [Reload]/[Refresh], 滚轮滚动
宽度: 拖动改了宽度会记住 (STATE_DIR/width), 下次 toggle 打开沿用; 默认值/是否记忆在配置目录 ($HERDR_PLUGIN_CONFIG_DIR) 的 config.json
"""
import agents as AG
import copy, curses, datetime, glob, json, os, re, shlex, shutil, subprocess, sys, textwrap, threading, time, unicodedata
from collections import Counter

H = os.environ.get("HERDR_BIN_PATH") or "herdr"
TARGET = os.environ.get("AGENT_PANEL_TARGET", "")
ME = os.environ.get("HERDR_PANE_ID", "")
HOME = os.path.expanduser("~")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp"
CONF_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or HERE
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "kackerx.agentpane"
POLL = 1.5
USAGE_TTL = 600  # 占用缓存多久内不自动重算 (秒); 按 u / 开关过 MCP·skill·plugin 会强制重算
TABS = ("Context", "Agents", "Plugin", "MCP", "Skills")
SNAP_V = 4  # 快照里的行结构变了就加一, 旧缓存整个作废 (1→2: skill 行多了 plugin_off, 旧缓存让面板一启动就 KeyError)
EARLY = {}  # 启动时同步取一次 pane 信息 (见 __main__): 有没有 agent 要在画第一帧之前就知道
AGMUX_URL = "https://github.com/kackerx/agmux"
STATES = ("on", "name-only", "user-invocable-only", "off")  # skillOverrides 的四档, /skills 里空格也是按这个顺序轮转
SHORT = {"user-invocable-only": "user-only"}
os.environ["PATH"] = ":".join([os.environ.get("PATH", ""), "/opt/homebrew/bin", os.path.join(HOME, ".local/bin")])


def run(*a, t=15):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=t).stdout
    except Exception:
        return ""


def jload(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def lst(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


norm = lambda s: re.sub(r"[^A-Za-z0-9_-]", "_", s)  # MCP 服务器名 → 工具名里的那一段 (mcp__<这段>__<工具>)


def guess_real(n):
    """工具名里只剩规范化后的名字时, 反推 /mcp 认的名字 (config 里查不到才用): 实测 claude.ai 连接器和插件的写法"""
    if n.startswith("claude_ai_"):
        return "claude.ai " + n[10:].replace("_", " ")
    if n.startswith("plugin_") and n.count("_") >= 2:
        _, a, b = n.split("_", 2)
        return f"plugin:{a}:{b}"
    return n


class Tx:
    """增量读 transcript, 只留这几种 attachment 的结果"""

    def __init__(self):
        self.reset(None)

    def reset(self, path):
        self.path, self.off = path, 0
        self.skills, self.tools, self.real, self.files = [], set(), set(), {}
        self.reload_ts = 0.0  # 最近一次 /reload-plugins 的时间 (transcript 里的 local-command-stdout "Reloaded: …")

    def update(self):
        if not self.path:
            return
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.off:
            self.reset(self.path)
        with open(self.path, "rb") as f:
            f.seek(self.off)
            data = f.read()
        end = data.rfind(b"\n") + 1
        self.off += end
        for line in data[:end].split(b"\n"):
            if b"Reloaded:" in line and b"local-command-stdout" in line:
                # 注意: reload 之后 transcript 不会补发 skill_listing / 工具的「移除」记录, 只有这一行输出 (实测),
                # 所以 reload 后的真实状态只能靠它的时间 + 当前设置推出来 (见 Model.refresh 的 unloaded)
                try:
                    self.reload_ts = max(self.reload_ts, datetime.datetime.fromisoformat(json.loads(line)["timestamp"]).timestamp())
                except (ValueError, KeyError, TypeError):
                    pass
                continue
            if b'"attachment"' not in line or not any(k in line for k in (b"skill_listing", b"deferred_tools_delta", b"mcp_instructions_delta", b'"instructions"')):
                continue
            try:
                a = json.loads(line).get("attachment") or {}
            except ValueError:
                continue
            t = a.get("type")
            if t == "skill_listing":
                names = lst(a.get("names"))
                if a.get("isInitial") or not self.skills:
                    self.skills = names
                else:
                    gone = set(lst(a.get("removedNames")))
                    self.skills = [s for s in self.skills if s not in gone] + [n for n in names if n not in self.skills]
            elif t == "deferred_tools_delta":
                add = lst(a.get("addedNames")) + lst(a.get("surfacedNames")) + lst(a.get("readdedNames"))
                self.tools |= {x for x in add if x.startswith("mcp__")}
                self.tools -= set(lst(a.get("removedNames")))
            elif t == "instructions":  # 会话开头加载的 CLAUDE.md / 自动记忆等: files = [{path, type: Project|User|Local|AutoMem…}]
                for x in lst(a.get("files")):
                    if isinstance(x, dict) and x.get("path"):
                        self.files[x["path"]] = x.get("type", "")
            elif t == "mcp_instructions_delta":
                self.real |= set(lst(a.get("addedNames")))
                self.real -= set(lst(a.get("removedNames")))


def find_session():
    info = (json.loads(run(H, "pane", "process-info", "--pane", TARGET) or "{}").get("result") or {}).get("process_info") or {}
    pids = {p.get("pid") for p in info.get("foreground_processes") or []}
    try:
        for s in json.loads(run("claude", "agents", "--json") or "[]"):
            if s.get("kind") == "interactive" and s.get("pid") in pids:
                return s
    except ValueError:
        pass
    return None


# ── settings: 低 → 高 = user, project, local; 开关一律写 local (不入库) ──────────────────────────────
def sfiles(cwd):
    return (os.path.join(HOME, ".claude/settings.json"), os.path.join(cwd, ".claude/settings.json"), os.path.join(cwd, ".claude/settings.local.json"))


def eff(cwd, key):
    out = {}
    for p in sfiles(cwd):
        out.update(jload(p).get(key) or {})
    return out


def write_local(cwd, key, name, new, default):
    """写 local 设置; 和下层 (user/project) 的值一样就删掉这个键, 不留多余覆盖; 其它键原样保留"""
    p = sfiles(cwd)[2]
    lower = default
    for q in sfiles(cwd)[:2]:
        x = (jload(q).get(key) or {}).get(name)
        lower = lower if x is None else x
    d = jload(p)
    ov = d.get(key) or {}
    if new == lower:
        ov.pop(name, None)
    else:
        ov[name] = new
    d[key] = ov
    if not ov:
        d.pop(key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def skill_src(name, cwd):
    """-> (来源, 路径): 插件 / project (<cwd>/.claude/skills) / user (~/.claude/skills) / built-in"""
    if ":" in name:
        return "plugin", None
    for label, base in (("project", os.path.join(cwd, ".claude/skills")), ("user", os.path.join(HOME, ".claude/skills"))):
        p = os.path.join(base, name)
        if os.path.lexists(p):
            return label, p
    return "built-in", None


def mcp_rows(tx, cwd):
    cj = jload(CLAUDE_JSON)
    proj = (cj.get("projects") or {}).get(cwd) or (cj.get("projects") or {}).get(os.path.realpath(cwd)) or {}
    local = jload(sfiles(cwd)[2])
    off = set(proj.get("disabledMcpServers") or []) | set(proj.get("disabledMcpjsonServers") or []) | set(local.get("disabledMcpjsonServers") or [])
    known = set(tx.real) | set(cj.get("mcpServers") or {}) | set(proj.get("mcpServers") or {}) | set((jload(os.path.join(cwd, ".mcp.json")).get("mcpServers") or {}))
    by_norm = {norm(k): k for k in known}
    counts = Counter(t.split("__")[1] for t in tx.tools if t.count("__") >= 2)
    rows = {by_norm.get(n) or guess_real(n): c for n, c in counts.items()}
    for k in known | off:
        rows.setdefault(k, 0)
    def scope(k):  # claude mcp remove -s 用的作用域名; 其它来源 (插件 / claude.ai 连接器) 不能删
        if k in (cj.get("mcpServers") or {}):
            return "user"
        if k in (proj.get("mcpServers") or {}):
            return "local"
        if k in (jload(os.path.join(cwd, ".mcp.json")).get("mcpServers") or {}):
            return "project"
        return "plugin" if k.startswith("plugin:") else "claude.ai" if k.startswith("claude.ai") else "?"
    return sorted(({"name": k, "tools": c, "on": k not in off, "scope": scope(k)} for k, c in rows.items()), key=lambda r: (not r["on"], r["name"].lower()))


def plugin_rows(tx, cwd):
    en = eff(cwd, "enabledPlugins")
    ids = set(en) | set(jload(os.path.join(HOME, ".claude/plugins/installed_plugins.json")).get("plugins") or {})
    skills = Counter(n.split(":")[0] for n in tx.skills if ":" in n)
    tools = Counter(t.split("__")[1].split("_")[1] for t in tx.tools if t.count("__") >= 2 and t.split("__")[1].startswith("plugin_") and t.split("__")[1].count("_") >= 2)
    inst = jload(os.path.join(HOME, ".claude/plugins/installed_plugins.json")).get("plugins") or {}
    rows = []
    for i in ids:
        lv = [(lab, (jload(p).get("enabledPlugins") or {}).get(i)) for lab, p in zip(("user", "project", "local"), sfiles(cwd))]
        defined = [(lab, v) for lab, v in lv if v is not None]
        scope = defined[-1][0] if defined else "-"  # 生效值来自哪一层
        off_here = scope != "user" and not en.get(i) and any(v for _, v in defined[:-1])  # 上层开着, 本项目关了
        rows.append({"id": i, "name": i.split("@")[0], "on": bool(en.get(i)), "skills": skills.get(i.split("@")[0], 0), "tools": tools.get(i.split("@")[0], 0),
                     "scope": scope, "off_here": off_here, "inst": ((inst.get(i) or [{}])[0].get("scope") or "user")})
    return sorted(rows, key=lambda r: (not r["on"], r["name"].lower()))


# ── /context 占用 ───────────────────────────────────────────────────────────────────────────
def tok(s):
    m = re.match(r"~?\s*([\d.]+)\s*([kKmM]?)", s.strip())
    return int(float(m[1]) * {"": 1, "k": 1e3, "m": 1e6}[m[2].lower()]) if m else 0


def parse_usage(md):
    u = {"model": "", "tokens": "", "tables": {}}
    sec, nrows = None, 0
    for line in md.split("\n"):
        m = re.match(r"\*\*(Model|Tokens):\*\*\s*(.*?)\s*$", line)
        if m:
            u[m[1].lower()] = m[2]
        elif line.startswith("#"):
            sec, nrows = line.lstrip("# ").strip(), 0
            u["tables"][sec] = []
        elif line.startswith("|") and sec is not None:
            nrows += 1
            if nrows > 2:  # 第 1 行表头, 第 2 行 |---|
                u["tables"][sec].append([c.strip() for c in line.strip().strip("|").split("|")])
    return u


def usage_lines(u):
    """-> [(文本, 样式 h/d/n)]; 宽度按 ~50 列排"""
    if not u:
        return [("No data yet — press u to compute (~4 s)", "d")]
    T = u["tables"]
    L = [(u["model"].replace("anthropic/", "") + "  " + u["tokens"], "h")]
    cats = T.get("Estimated usage by category", [])
    free = ("Free space", "Autocompact buffer")
    mx = max([tok(c[1]) for c in cats if c[0] not in free] or [1])
    for c in cats:
        n = 0 if c[0] in free else max(1 if tok(c[1]) else 0, round(10 * tok(c[1]) / mx))
        L.append((f"{c[0][:22]:<22} {c[1]:>6} {c[2]:>6} {'█' * n}", "d" if c[0] in free else "n"))
    return L


def agent_path(name, src, cwd):
    """Custom agents 行 -> 能编辑的文件路径; 插件/内置的 agent 没有 (None)"""
    base = {"user": os.path.join(HOME, ".claude/agents"), "project": os.path.join(cwd, ".claude/agents")}.get(src.lower())
    if not base:
        return None
    p = os.path.join(base, name + ".md")
    if os.path.exists(p):
        return p
    for q in glob.glob(os.path.join(base, "*.md")):  # 文件名和 frontmatter 的 name 不一致时按内容找
        try:
            if re.search(rf"^name:\s*{re.escape(name)}\s*$", open(q).read(4000), re.M):
                return q
        except OSError:
            pass
    return None


def usage_cache(sid):
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, f"usage-{sid}.json")


def tok_of(u, sec, key, idx=0):
    """Usage 表里 sec 小节中第 idx 列 == key 的行的最后一列 → token 数; 没有 = 0"""
    for r in ((u or {}).get("tables", {}).get(sec) or []):
        if len(r) > idx and r[idx] == key:
            return tok(r[-1])
    return 0


class Model:
    """后台线程每 POLL 秒刷新一次快照, 界面只读快照"""

    def __init__(self):
        self.lock, self.tx, self.sid, self.sess, self.snap = threading.Lock(), Tx(), None, None, {"msg": "Finding session…"}
        self.status, self.status_ts = "", 0.0
        self.usage = {"data": None, "ts": 0, "busy": False}
        self.stale = False
        self.ctx = None  # 上下文百分比 (herdr pane 上的 tokens.ctx)
        self.nosess = None  # 第一次发现这个 pane 里没有 Claude 会话的时间
        self.my_tab = EARLY.get("tab_id")  # 面板服务的 agent 所在的 tab
        self.agents, self.agents_now = [], threading.Event()  # Agents 页: 当前项目的 agent 卡片 (后台线程每 5 秒刷新)
        self.agent, self.pane_cwd = EARLY.get("agent") or "", EARLY.get("foreground_cwd") or EARLY.get("cwd")  # herdr 认出的 agent 和这个 pane 的目录

    def refresh(self, resolve):
        if resolve or not self.sid:
            s = find_session()
            if not s:
                self.set(dict(sess=None, msg="No Claude Code session in this pane"))
                self.nosess = self.nosess or time.time()
                return
            self.nosess = None
            if s["sessionId"] != self.sid:
                sid = s["sessionId"]
                paths = glob.glob(os.path.join(HOME, ".claude/projects/*", sid + ".jsonl"))
                self.tx.reset(paths[0] if paths else None)
                c = jload(usage_cache(sid))  # 先放缓存再公布 sid, 主线程看到 sid 时数据已在
                self.usage.update(data=c.get("data"), ts=c.get("ts", 0) if c.get("data") else 0)
                self.sid = sid
            self.sess = s
        self.tx.update()
        cwd = self.sess["cwd"]
        files = [{"path": p, "type": t, "loaded": True} for p, t in self.tx.files.items()]
        have = {os.path.realpath(p) for p in self.tx.files}
        for p, t in ((os.path.join(cwd, "CLAUDE.md"), "Project"), (os.path.join(cwd, "CLAUDE.local.md"), "Local"), (os.path.join(cwd, "AGENTS.md"), "Project"),
                     (os.path.join(cwd, ".claude/CLAUDE.md"), "Project"), (os.path.join(HOME, ".claude/CLAUDE.md"), "User")):
            if os.path.isfile(p) and os.path.realpath(p) not in have:  # 磁盘上有但这个会话没加载的 (比如有 CLAUDE.md 时 AGENTS.md 不会读)
                files.append({"path": p, "type": t, "loaded": False})
        ov = eff(cwd, "skillOverrides")
        lov = jload(sfiles(cwd)[2]).get("skillOverrides") or {}
        cur = sorted(r["id"] for r in plugin_rows(self.tx, cwd) if not r["on"])
        changes = jload(os.path.join(STATE_DIR, "plugin-changes.json"))
        mt = max((os.path.getmtime(p) for p in sfiles(cwd) if os.path.exists(p)), default=0)
        # 关了且之后 reload 过 = 已卸载: 从面板看到的 skill / 工具里去掉 (transcript 里它们还在, 因为 reload 不写移除记录)
        gone = {i.split("@")[0] for i in cur if self.tx.reload_ts and self.tx.reload_ts >= changes.get(cwd + "|" + i, mt)}
        view = copy.copy(self.tx)
        view.skills = [n for n in self.tx.skills if n.split(":")[0] not in gone or ":" not in n]
        view.tools = {t for t in self.tx.tools if not (t.count("__") >= 2 and t.split("__")[1].startswith("plugin_") and t.split("__")[1].count("_") >= 2 and t.split("__")[1].split("_")[1] in gone)}
        plugins = plugin_rows(view, cwd)
        off = {r["name"] for r in plugins if not r["on"]}  # 已关掉但这个会话还没 reload 的插件, 它的 skill 还在列表里
        self.set(dict(sess=self.sess, cwd=cwd, msg="", agents=self.agents, mcp=mcp_rows(view, cwd), files=files, plugins=plugins,
                      skills=[dict(name=n, state=ov.get(n, "on"), src=skill_src(n, cwd)[0], path=skill_src(n, cwd)[1], local=n in lov, plugin_off=":" in n and n.split(":")[0] in off)
                              for n in sorted(set(view.skills), key=str.lower)]))

    def set(self, snap):
        with self.lock:
            if snap != self.snap:
                self.snap = snap

    def say(self, s):
        with self.lock:
            self.status, self.status_ts = s, time.time()

    def agent_cmd(self, cmd, ok):
        """给 agent 发一条斜杠命令 (后台线程里调, 不卡界面)"""
        def go():
            out = run(H, "agent", "prompt", TARGET, cmd)
            err = re.search(r'"message":"([^"]*)"', out)
            self.say("✗ " + (err.group(1) if err else "no response from herdr") if err or not out else ok)
            self.stale = True
            self.refresh(False)
        threading.Thread(target=go, daemon=True).start()

    def toggle(self, kind, row):
        cwd = self.snap.get("cwd")
        if kind == "mcp":
            name, on = row["name"], row["on"]
            self.say(f"… /mcp {'disable' if on else 'enable'} {name}")
            self.agent_cmd(f"/mcp {'disable' if on else 'enable'} {name}", f"✓ {name} {'disabled' if on else 'enabled'}")
        elif kind == "plugin":
            write_local(cwd, "enabledPlugins", row["id"], not row["on"], False)
            ch = jload(os.path.join(STATE_DIR, "plugin-changes.json"))
            ch[cwd + "|" + row["id"]] = time.time()
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "plugin-changes.json"), "w") as f:
                json.dump(ch, f)
            self.say(f"✓ {row['name']} {'disabled' if row['on'] else 'enabled'} in this project · press r to reload")
            self.stale = True
            self.refresh(False)
        else:  # skill: on → name-only → user-only → off 轮转 (和 /skills 一致, 一律写本项目的 settings.local.json)
            new = STATES[(STATES.index(row["state"]) + 1) % 4] if row["state"] in STATES else "on"
            write_local(cwd, "skillOverrides", row["name"], new, "on")
            self.say(f"✓ {row['name']} → {SHORT.get(new, new)}" + (" · blocked now; still listed (and costing tokens) until a new session" if new == "off" else ""))
            self.stale = True
            self.refresh(False)

    def ask_delete(self, kind, row):
        """-> (确认提示, 执行函数) 或 (原因, None); 不能删的 (插件自带的 skill、插件/连接器带的 MCP) 直接说明"""
        cwd = self.snap.get("cwd")
        if kind == "plugin":
            sc = row["inst"]
            return f"Uninstall plugin {row['name']} (installed at {sc} scope, for all projects)? y = yes", lambda: self.cli(["claude", "plugin", "uninstall", row["id"], "--scope", sc], cwd, f"{row['name']} uninstalled · press r to reload")
        if kind == "mcp":
            sc = row["scope"]
            if sc not in ("user", "local", "project"):
                return f"Can't delete {row['name']}: it comes from {sc} — disable or uninstall that instead", None
            return f"Remove MCP server {row['name']} ({sc} scope)? y = yes", lambda: self.cli(["claude", "mcp", "remove", row["name"], "-s", sc], cwd, f"{row['name']} removed")
        if not row["path"]:
            return f"Can't delete {row['name']}: it comes from {row['src']} — uninstall that instead", None
        real = os.path.realpath(row["path"])
        how = "remove the symlink" if os.path.islink(row["path"]) else "move to Trash"
        return f"Delete skill {row['name']} ({row['src']}: {real.replace(HOME, '~')}) — {how}? y = yes", lambda: self.rm_skill(row)

    def update(self, kind, row):
        """U: 更新当前插件到最新版 (没加 -y: 市场声明了要执行的命令时让它拒绝, 别替用户点同意); MCP / skill 没有统一的更新入口"""
        if kind != "plugin":
            self.say("✗ Update only works on plugins (MCP servers and skills have no update command)")
            return
        self.cli(["claude", "plugin", "update", row["id"], "-s", row["inst"]], self.snap.get("cwd"), f"{row['name']} updated · restart the session to apply")

    def rm_skill(self, row):
        p = row["path"]
        try:
            if os.path.islink(p):
                os.unlink(p)
            else:
                trash = os.path.join(HOME, ".Trash")
                dst = os.path.join(trash, f"{row['name']}-{int(time.time())}")
                shutil.move(p, dst)
            self.say(f"✓ {row['name']} deleted (new sessions won't list it)")
        except OSError as e:
            self.say("✗ " + str(e)[:50])
        self.refresh(False)

    def cli(self, cmd, cwd, ok):
        def go():
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd)
                self.say("✓ " + ok if r.returncode == 0 else "✗ " + ((r.stderr or r.stdout).strip().split("\n")[-1][:60] or "failed"))
            except Exception as e:
                self.say("✗ " + str(e)[:50])
            self.stale = True
            self.refresh(True)
        self.say("… " + " ".join(cmd[1:4]))
        threading.Thread(target=go, daemon=True).start()

    def refresh_agents(self):
        cwd = self.pane_cwd or (self.sess or {}).get("cwd") or os.getcwd()
        self.agents = AG.fetch(cwd, H)
        with self.lock:
            self.snap = dict(self.snap, agents=self.agents)

    def patch_agent(self, name, **kw):
        """置顶 / 换头像先改本地列表立刻刷新 (重新取一遍要 0.5~1 秒), 后台随后会再取一次校正"""
        for r in self.agents:
            if r["name"] == name:
                r.update(kw)
        AG.sort_rows(self.agents)
        with self.lock:
            self.snap = dict(self.snap, agents=list(self.agents))

    def go_card(self, r):
        """点 agent: 就是本 tab 里的 (或是本面板服务的那个) → 直接跳到它的 pane; 其它不管前台后台都开浮窗 (标题 = agent 名)"""
        if not r["bg"] and r.get("pane") and (r["pane"] == TARGET or r.get("tab") == self.my_tab):
            self.say("→ " + r["name"])
            run(H, "agent", "focus", r["pane"])
            return
        if r["bg"]:
            cmd = "claude attach " + shlex.quote(r["id"])
        elif r.get("pane"):
            cmd = "herdr agent attach " + shlex.quote(r["pane"])
        else:
            self.say("✗ " + r["name"] + " is not in a herdr pane (nothing to attach)")
            return
        toml = os.path.join(HERE, "herdr-plugin.toml")
        src = open(toml).read()
        title = (re.sub(r'["\\\n]', "", r["name"]) or "agent") + "  ·  ^q q close"
        # herdr 浮窗标题只认清单里的 title (开窗时才读, 没有开窗参数): 开之前改写, 开完立刻还原。
        # 只对 link 的本地目录这么做; plugin install 装的是 herdr 托管的 checkout, 不能改, 标题就是清单里的 "agent  ·  ^q q close"
        retitle = "/github-installations/" not in HERE and os.access(toml, os.W_OK)
        if retitle:
            open(toml, "w").write(re.sub(r'(id = "attach"\ntitle = )"[^"]*"', lambda mm: mm.group(1) + '"' + title + '"', src))
        try:
            out = run(H, "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "attach", "--env", "ATTACH_CMD=" + cmd, "--focus")
        finally:
            if retitle:
                threading.Timer(1.5, lambda: open(toml, "w").write(src)).start()
        self.say(("✓ opened " if '"type":"ok"' in out.replace(" ", "") else "✗ failed to open ") + r["name"])

    def close_agent(self, r):
        """x: 后台会话 = `claude stop <id>` (和 agmux 一样); 前台 = 关掉它的 herdr pane"""
        if r["bg"]:
            self.cli(["claude", "stop", r["id"]], r["cwd"], r["name"] + " stopped")
        elif r.get("pane"):
            run(H, "pane", "close", r["pane"])
            self.say("✓ closed " + r["name"])
        else:
            self.say("✗ " + r["name"] + " is not in a herdr pane (can't close it from here)")
        self.agents_now.set()

    def edit(self, path):
        """在浮窗 (popup, 清单里设 80%×80%) 里用 nvim 改这个文件, nvim 退出浮窗跟着关"""
        if not path or not os.path.exists(path):
            self.say("✗ nothing to edit here (plugin / built-in items live outside your files)")
            return
        out = run(H, "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "editor", "--env", "EDIT_FILE=" + path, "--focus")  # 尺寸/popup 在清单里
        self.say("✓ editing " + os.path.basename(path) if '"type":"ok"' in out.replace(" ", "") else "✗ failed to open the editor")

    def reload(self):
        self.say("… /reload-plugins")
        self.agent_cmd("/reload-plugins", "✓ reloaded")

    def fetch_usage(self):
        if self.usage["busy"] or not self.sid:
            return
        self.usage["busy"], self.stale = True, False
        self.say("… computing /context (~4 s)")

        def go():
            try:
                out = subprocess.run(["claude", "-p", "/context", "--resume", self.sid, "--fork-session", "--no-session-persistence", "--output-format", "json"],
                                     capture_output=True, text=True, timeout=90, cwd=self.sess["cwd"]).stdout
                self.usage.update(data=parse_usage(json.loads(out)["result"]), ts=time.time())
                with open(usage_cache(self.sid), "w") as f:
                    json.dump({"data": self.usage["data"], "ts": self.usage["ts"]}, f)
                self.say("✓ usage updated")
            except Exception as e:
                self.say("✗ /context failed: " + str(e)[:40])
            self.usage["busy"] = False
        threading.Thread(target=go, daemon=True).start()


TYPE_ORDER = ("built-in", "user", "project", "local", "plugin", "claude.ai", "autoMem", "none")  # t 键轮转时的先后


def row_type(x):
    """一行的「类型」(t 键过滤用): skill/agent 的来源, 插件/MCP 的作用域, 记忆文件的类型"""
    kind, r = x
    t = {"skill": r.get("src"), "agent": r.get("src"), "file": r.get("type"), "plugin": r.get("scope"), "mcp": r.get("scope")}.get(kind) or ""
    t = t.lower()
    return "none" if t in ("-", "?", "") else "automem" if t == "automem" else t


def tab_items(snap, u, tab, f, typ="all"):
    """当前页的行; typ != all 时只留这个类型 (文字行不算)"""
    rows = _rows(snap, u, tab, f)
    return rows if typ == "all" else [x for x in rows if x[0] != "text" and row_type(x) == typ]


def type_cycle(snap, u, tab):
    """这一页现有的类型: all + 出现过的 (按 TYPE_ORDER 排)"""
    have = {row_type(x) for x in _rows(snap, u, tab, "") if x[0] != "text"}
    order = [t.lower() for t in TYPE_ORDER]
    return ["all"] + sorted(have, key=lambda t: order.index(t) if t in order else 99)


def _rows(snap, u, tab, f):
    """当前页的行: (kind, 数据); f = 过滤串 (小写)"""
    if not snap.get("sess"):
        return []
    if tab == "Agents":
        return [("card", r) for r in snap.get("agents", []) if f in r["name"].lower()]
    if tab == "Plugin":
        return [("plugin", r) for r in snap["plugins"] if f in r["id"].lower()]
    if tab == "MCP":
        return [("mcp", r) for r in snap["mcp"] if f in r["name"].lower()]
    if tab == "Skills":
        return [("skill", r) for r in snap["skills"] if f in r["name"].lower()]
    rows = [("text", x) for x in usage_lines(u)]
    agents = sorted((r for r in ((u or {}).get("tables", {}).get("Custom Agents") or []) if len(r) >= 3), key=lambda r: -tok(r[-1]))
    rows += [("text", ("", "n")), ("text", (f"Custom agents · {len(agents)}", "h"))] if agents else []
    rows += [("agent", {"name": r[0], "src": r[1], "tok": r[-1], "path": agent_path(r[0], r[1], snap["cwd"])}) for r in agents]
    rows += [("text", ("", "n")), ("text", (f"Memory files · {len(snap['files'])}", "h"))] if snap["files"] else []
    rows += [("file", r) for r in snap["files"]]
    return [x for x in rows if x[0] == "text" and not f or f in (x[1][0] if x[0] == "text" else x[1].get("name") or x[1].get("path") or "").lower()]


def has_handoff(cwd):
    """有名为 handoff 的 skill (用户级或这个项目的 .claude/skills/handoff) 才给菜单项: /handoff 不是 Claude Code 内置命令"""
    return any(os.path.isfile(os.path.join(d, ".claude/skills/handoff/SKILL.md")) for d in (HOME, cwd or ""))


def agmux_bin():
    return shutil.which(os.environ.get("AGMUX_BIN") or "agmux")


def launch_recall(cwd):
    """pane 里没有 agent: 面板整个换成 `agmux recall` (范围默认当前项目, 预览放下面); 选中会话由 resume.py 发进 agent 那个 pane。
    成功不会返回 (exec); 没装 agmux 返回 False"""
    agmux = agmux_bin()
    if not agmux or not cwd or not os.path.isdir(cwd):
        return False
    shim = os.path.join(STATE_DIR, "shim")
    os.makedirs(shim, exist_ok=True)
    for n in ("claude", "pi"):
        link = os.path.join(shim, n)
        if os.path.realpath(link) != os.path.join(HERE, "resume.py"):
            try:
                os.remove(link)
            except OSError:
                pass
            os.symlink(os.path.join(HERE, "resume.py"), link)
    env = dict(os.environ, PATH=shim + ":" + os.environ.get("PATH", ""), AGMUX_RECALL_PREVIEW="below", AGMUX_RECALL_SCOPE="project")
    try:
        curses.endwin()
    except curses.error:  # 启动早期还没 initscr
        pass
    os.chdir(cwd)
    os.execve(agmux, [agmux, "recall"], env)


def theme_blue():
    """herdr 给 done 用的颜色 = 主题的 blue 槽: 读 herdr config.toml 里自定义的 blue (没有就用 herdr 默认主题的 #89b4fa), 换成最近的 256 色"""
    herdr_dir = os.path.dirname(os.path.dirname(os.path.dirname(CONF_DIR))) if os.environ.get("HERDR_PLUGIN_CONFIG_DIR") else os.path.join(HOME, ".config/herdr")
    try:
        with open(os.path.join(herdr_dir, "config.toml")) as f:
            m = re.search(r'^\s*blue\s*=\s*"#([0-9a-fA-F]{6})"', f.read(), re.M)  # ponytail: 只认 [theme.custom] 的 blue; 换了别的内置主题仍按 catppuccin 的蓝
    except OSError:
        m = None
    rgb = [int((m.group(1) if m else "89b4fa")[i:i + 2], 16) for i in (0, 2, 4)]
    lv = (0, 95, 135, 175, 215, 255)
    pal = {16 + 36 * a + 6 * b + c: (lv[a], lv[b], lv[c]) for a in range(6) for b in range(6) for c in range(6)}
    pal.update({232 + i: (8 + 10 * i,) * 3 for i in range(24)})
    return min(pal, key=lambda k: sum((x - y) ** 2 for x, y in zip(pal[k], rgb)))


def main(scr):
    curses.set_escdelay(25)
    curses.curs_set(0)
    curses.use_default_colors()
    for i, c in enumerate((curses.COLOR_GREEN, 208 if curses.COLORS > 208 else curses.COLOR_YELLOW, curses.COLOR_CYAN, curses.COLOR_RED,
                           theme_blue() if curses.COLORS >= 256 else curses.COLOR_BLUE), 1):
        curses.init_pair(i, c, -1)
    GREEN, ORANGE, CYAN, RED, BLUE = (curses.color_pair(i) for i in range(1, 6))
    if curses.COLORS >= 256:  # 页签胶囊: 字 + 底色, 两头的半圆用底色当前景色
        curses.init_pair(6, 16, 208)
        curses.init_pair(7, 208, -1)
        curses.init_pair(8, 252, 239)
        curses.init_pair(9, 239, -1)
        PILL_ON, PILL_ON_CAP, PILL, PILL_CAP = (curses.color_pair(i) for i in range(6, 10))
    else:
        PILL_ON, PILL_ON_CAP, PILL, PILL_CAP = curses.A_REVERSE | ORANGE, ORANGE, curses.A_REVERSE, 0
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    scr.keypad(True)
    scr.timeout(250)

    m = Model()
    stop = threading.Event()

    def poll():
        n = 0
        while not stop.is_set():
            try:
                m.refresh(n % 4 == 0)
                got = run(H, "pane", "get", TARGET)
                if got.count("pane_not_found"):  # agent pane 没了面板跟着关
                    os._exit(0)
                c = re.search(r'"ctx":"[^"]*?(\d+)%"', got)
                m.ctx = int(c[1]) if c else m.ctx
                try:
                    pane = json.loads(got)["result"]["pane"]
                    m.agent, m.pane_cwd, m.my_tab = pane.get("agent") or "", pane.get("foreground_cwd") or pane.get("cwd"), pane.get("tab_id")
                except (ValueError, KeyError):
                    pass
            except Exception as e:  # 后台线程不能把面板带崩
                m.say(f"✗ {e}")
            n += 1
            stop.wait(POLL)

    threading.Thread(target=poll, daemon=True).start()

    def agents_loop():
        while not stop.is_set():
            try:
                m.refresh_agents()
            except Exception as e:
                m.say(f"✗ agents: {e}")
            m.agents_now.wait(5)
            m.agents_now.clear()

    threading.Thread(target=agents_loop, daemon=True).start()
    imgs, picker, menu, av_cells, avail, avail_at = AG.Images(), None, None, [], {}, 0.0  # picker: 头像选择框; menu: 右键菜单
    if ME:
        run(H, "pane", "input", "--pane", ME, "--right-click", "pane")  # herdr 默认自己吃右键 (弹它的 pane 菜单), 改成把右键交给本 pane
    IMG = os.environ.get("AGENT_PANEL_AVATAR") != "text" and os.environ.get("TERM_PROGRAM") == "herdr"  # herdr 的 pane 答应 kitty 图形; 别的终端先画文字头像

    typ = {t: "all" for t in TABS}  # 每页的类型过滤 (t 键)
    sel, top, flt, psel = {t: 0 for t in TABS}, {t: 0 for t in TABS}, {t: "" for t in TABS}, {t: 0 for t in TABS}
    typing = pend_g = False
    pending = None  # (确认提示, 执行函数)
    hit, tab_hits, btns = {}, [], []
    t0, last_w, auto_usage = time.time(), None, False
    remember = jload(os.path.join(CONF_DIR, "config.json")).get("remember", True)
    keep = os.path.join(STATE_DIR, "last-" + re.sub(r"[^A-Za-z0-9]", "_", TARGET) + ".json")  # 按它服务的 agent pane 存
    old = jload(keep)
    if old.get("snap") and old.get("v") == SNAP_V:
        m.snap = old["snap"]  # 先用上次的内容画第一帧, 后台线程随后刷新
        m.ctx = old.get("ctx")
    ui = old.get("ui") or {}
    tab = ui.get("tab", 0) if isinstance(ui.get("tab"), int) and 0 <= ui.get("tab", 0) < len(TABS) else 0
    for d, key in ((sel, "sel"), (top, "top"), (flt, "flt"), (typ, "typ")):
        d.update({t: v for t, v in (ui.get(key) or {}).items() if t in TABS})
    psel.update(sel)
    saved, saved_at = None, 0.0
    while True:
        T = TABS[tab]
        with m.lock:
            snap, status, sts = m.snap, m.status, m.status_ts
        if status and not status.startswith("…") and time.time() - sts > 6:  # 提示只留几秒, 进行中的 (… 开头) 留到有结果
            status = ""
        if m.sid and not auto_usage:  # 开面板后算一次, Skills/MCP/Context 页的 token 数才有
            auto_usage = True
            if time.time() - m.usage["ts"] > USAGE_TTL:  # /context 本身不调 API 不花 token, 缓存只为省掉每次打开等的 4 秒; 缓存还新就直接用
                m.fetch_usage()
        if m.nosess and not m.agent and time.time() - m.nosess > 1.2 and time.time() > float(os.environ.get("AP_RECALL_HOLD", 0)):
            launch_recall(m.pane_cwd)  # exec, 成功就不回来; 没装 agmux 才继续往下画「没有会话」
            m.nosess = None
        u = m.usage["data"]
        items = tab_items(snap, u, T, flt[T].lower(), typ[T])
        sel[T] = max(0, min(sel[T], len(items) - 1))
        h, w = scr.getmaxyx()
        if remember and last_w is not None and w != last_w and time.time() - t0 > 1.5:  # 拖动改了宽度 → 记下来, 下次打开沿用
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(os.path.join(STATE_DIR, "width"), "w") as f:
                    f.write(str(w))
            except OSError:
                pass
        last_w = w
        stext = ("/" + flt[T] + ("█" if typing else "")) if (flt[T] or typing) else status
        slines = textwrap.wrap(stext, max(10, w - 5))[:3] if stext else []  # 状态/提示放到底部的 tips 框里, 太长折行 (最多 3 行)
        box_h = len(slines) + 2 if slines and h >= 14 else 0
        # 页签: 每个一个圆角框, 等宽网格排, 放不下一行就折行 (窄面板 5 个页签也都看得见)
        cnts = {"Plugin": sum(r["on"] for r in snap.get("plugins", [])), "MCP": len(snap.get("mcp", [])), "Skills": len(snap.get("skills", [])), "Agents": len(snap.get("agents", [])), "Context": len(snap.get("files", [])) + len((u or {}).get("tables", {}).get("Custom Agents") or [])}
        tlabels = [f"{n}{'' if cnts.get(n) is None else ' ' + str(cnts[n])}" for n in TABS]
        tpos, tx, ty = [], 1, 1  # 页签 = 一行高的圆头胶囊 (nerd font 的半圆头), 按内容宽度从左往右排, 排不下就折行
        for label in tlabels:
            tw = len(label) + 4  # 左右半圆头各 1 + 字两边各 1 空格
            if tx > 1 and tx + tw > w - 1:
                tx, ty = 1, ty + 1
            tpos.append((ty, tx, tw))
            tx += tw + 1
        body0 = ty + 2  # 头 1 行 + 页签行 + 分割线
        body_h = max(1, h - body0 - 4 - box_h)  # 底下: tips 框 + 分割线 + 按钮 + 说明 2 行
        rowh = 5 if T == "Agents" else 1  # Agents 页一张卡: 上边框 + 3 行内容 + 下边框
        vis = max(1, body_h // rowh)  # 一屏能放几项
        av_cells.clear()
        if time.time() - avail_at > 8:
            avail, avail_at = AG.sets(), time.time()
        if items and items[sel[T]][0] == "text":  # 文字行 (类别表/小节标题) 不可选: 顺着移动方向找下一个可选行, 找不到就反向
            d = 1 if sel[T] >= psel[T] else -1
            for dd in (d, -d):
                k2 = sel[T]
                while 0 <= k2 < len(items) and items[k2][0] == "text":
                    k2 += dd
                if 0 <= k2 < len(items):
                    sel[T] = k2
                    break
        psel[T] = sel[T]
        if sel[T] < top[T]:
            top[T] = sel[T]
        if sel[T] >= top[T] + vis:
            top[T] = sel[T] - vis + 1
        if items and all(x[0] == "text" for x in items[:sel[T]]):  # 选中的是第一个可选行 → 把上面的文字也露出来
            top[T] = 0
        scr.erase()
        s = snap.get("sess")
        def put(y, x, t, a=0):
            # addnstr 的 n 数的是字符不是列: 中日韩字符占 2 列, 会越过右边界折到下一行, 末行则写到右下角 → curses.error 崩整个面板
            if not (0 <= y < h and 0 <= x < w):
                return
            room, out = max(0, w - x - 1), []
            for ch in t:
                room -= 2 if unicodedata.east_asian_width(ch) in "WF" else 1
                if room < 0:
                    break
                out.append(ch)
            try:
                scr.addstr(y, x, "".join(out), a)
            except curses.error:
                pass
        if s:
            pc = re.search(r"\((\d+)%\)", (u or {}).get("tokens", ""))
            pct = m.ctx if m.ctx is not None else int(pc[1]) if pc else None
            bar = f"{'▰' * round(pct / 10):▱<10} {pct}%" if pct is not None else ""
            name = s.get("name") or s["sessionId"][:8]
            put(0, 1, name, curses.A_BOLD | ORANGE)
            room = w - 2 - len(bar) - (2 if bar else 0)  # 目录放名字后面, 占用条贴右边; 目录太长从左边截
            cwd = snap["cwd"].replace(HOME, "~")
            left = room - len(name) - 2
            if left >= 4:
                put(0, 3 + len(name), cwd if len(cwd) <= left else "…" + cwd[len(cwd) - left + 1:], curses.A_DIM)
            if bar:
                put(0, w - 1 - len(bar), bar, GREEN if pct < 60 else ORANGE if pct < 80 else RED)
        else:
            put(0, 1, snap.get("msg", ""), RED)
        def sep(y, text="", a=curses.A_DIM):
            put(y, 0, "─" * w, curses.A_DIM)
            if text:
                put(y, 2, " " + text + " ", a)
        # 当前页 = 橙底胶囊, 其它 = 灰底; 点胶囊切页
        tab_hits = []
        for i, (label, (ty, tx, tw)) in enumerate(zip(tlabels, tpos)):
            cap, body = (PILL_ON_CAP, PILL_ON) if i == tab else (PILL_CAP, PILL)
            put(ty, tx, "\ue0b6", cap)
            put(ty, tx + 1, f" {label} ", body | (curses.A_BOLD if i == tab else 0))
            put(ty, tx + tw - 1, "\ue0b4", cap)
            tab_hits.append((ty, tx, tw, i))
        busy = m.usage["busy"]
        sattr = CYAN if (flt[T] or typing) else GREEN if status.startswith("✓") else RED if status.startswith("✗") else curses.A_DIM
        sep(body0 - 1)
        hit = {}
        tick = int(time.time() * 2)  # 头像动画: 每 0.5 秒换一帧

        def face(x, y, aid, status):
            """画头像 (槽 SLOT×ROWS 格, 图按自己的宽高比居中): 图片模式记下位置, curses 先在图的位置填底色字符 (refresh 之后原始序列盖上去); 否则文字头像"""
            p = AG.frame_path(avail[aid], status, tick) if aid in avail else None
            got = imgs.id_for(p) if (IMG and p) else None
            if got:
                iid, cols = got
                av_cells.append((x, y, iid, cols))
                for r_ in range(AG.ROWS):
                    put(y + r_, x + (AG.SLOT - cols) // 2, "█" * cols, curses.A_DIM)
                return
            lt = ((aid or "?")[:1] or "?").upper()
            x += (AG.SLOT - 6) // 2
            for r_, ln in enumerate(("▗▄▄▄▄▖", "▐" + lt.center(4) + "▌", "▝▀▀▀▀▘")):
                put(y + r_, x, ln, (GREEN, ORANGE, CYAN, RED)[AG.fnv32a(aid or "") % 4])

        def card(y, r, on):
            """一张卡片 (agmux board 风格): 边框 + 头像槽 | 名字 / 分支·角色 / 模型 + 上下文条; 顶边框的标题是状态, 选中的是橙色双线框"""
            stc = GREEN if r["status"] == "working" else BLUE if r["status"] == "done" else curses.A_DIM if r["status"] in ("idle", "") else RED  # done 和 herdr 侧栏的圆点同色
            bc = (ORANGE | curses.A_BOLD) if on else stc
            tl, hz, tr, vt, bl, br = "╭─╮│╰╯"  # 选中和没选中都是圆角, 选中靠橙色加粗 + ❯ 区分 (Unicode 没有圆角双线)
            wid = w - 1  # 框占 0..w-2
            title = f" {r['status'] or '?'} "
            put(y, 0, tl + hz + title + hz * max(0, wid - 3 - len(title)) + tr, bc)
            for k_ in range(1, 4):
                put(y + k_, 0, vt, bc)
                put(y + k_, wid - 1, vt, bc)
            put(y + 4, 0, bl + hz * (wid - 2) + br, bc)
            if on:
                put(y + 2, 0, "❯", ORANGE | curses.A_BOLD)  # 选中的箭头, 压在左框上
            face(2, y + 1, r.get("avatar"), r["status"])  # 头像离边框 1 格, 离文字 1 格
            x0 = 2 + AG.SLOT + 1
            room = wid - 2 - x0
            tag = "bg" if r["bg"] else ""
            put(y + 1, x0, (("▲ " if r["pinned"] else "") + r["name"])[:max(1, room - 3)], curses.A_BOLD | (ORANGE if on else 0))
            if tag:
                put(y + 1, wid - 2 - len(tag), tag, curses.A_BOLD | ORANGE)
            br_ = ("\ue0a0 " + r["branch"]) if r.get("branch") else ""
            put(y + 2, x0, br_, RED)
            if r["role"]:
                put(y + 2, x0 + len(br_) + 2, r["role"], curses.A_DIM)
            if r["model"]:
                put(y + 3, x0, r["model"], curses.A_DIM)
            if r["ctx"] >= 0:
                cb = f"{'▰' * round(r['ctx'] / 10):▱<10} {r['ctx']}%"
                put(y + 3, wid - 2 - len(cb), cb, GREEN if r["ctx"] < 60 else ORANGE if r["ctx"] < 80 else RED)

        def overlay_box(x, y, bw, bh, title="", color=ORANGE):
            """在 (x,y) 起画一个 bw×bh 的圆角框并清空里面 (压在卡片上); 被盖住的头像图片从 av_cells 里去掉"""
            av_cells[:] = [c for c in av_cells if not (c[1] + AG.ROWS > y and c[1] < y + bh and c[0] < x + bw and c[0] + AG.SLOT > x)]
            for k_ in range(bh):
                put(y + k_, x, " " * bw)
            put(y, x, "╭─" + (" " + title + " " if title else "") + "─" * max(0, bw - 3 - (len(title) + 2 if title else 0)) + "╮", color)
            for k_ in range(1, bh - 1):
                put(y + k_, x, "│", color)
                put(y + k_, x + bw - 1, "│", color)
            put(y + bh - 1, x, "╰" + "─" * (bw - 2) + "╯", color)

        for i, it in enumerate(items[top[T]:top[T] + vis]):
            y, idx = body0 + i * rowh, top[T] + i
            for k2 in range(rowh):
                hit[y + k2] = idx
            if it[0] == "card":
                card(y, it[1], idx == sel[T])
                continue
            a = curses.A_REVERSE if idx == sel[T] and it[0] != "text" else 0
            if a:
                put(y, 0, " " * w, a)
            col = lambda c: a or c  # 选中时整行只用反色, 别让方块/尾巴各染各的颜色
            kind, r = it
            if kind == "text":
                put(y, 1, r[0], {"h": curses.A_BOLD | ORANGE, "d": curses.A_DIM}.get(r[1], 0))
                continue
            if kind == "agent":
                extra = f"{r['src']} · {r['tok']}"
                put(y, 2, "●" if r["path"] else "○", col(GREEN if r["path"] else curses.A_DIM))
                put(y, 5, r["name"], col(0 if r["path"] else curses.A_DIM))
                put(y, w - len(extra) - 2, extra, col(curses.A_DIM))
                continue
            if kind == "file":
                shown = os.path.relpath(r["path"], snap["cwd"]) if r["path"].startswith(snap["cwd"] + "/") else r["path"].replace(HOME, "~")
                t_ = tok_of(u, "Memory Files", r["path"], 1)
                extra = r["type"] + (f" {t_ / 1000:.1f}k" if t_ else "")
                room = w - 7 - len(extra) - 2
                shown = shown if len(shown) <= room else "…" + shown[len(shown) - room + 1:]
                put(y, 2, "●" if r["loaded"] else "○", col(GREEN if r["loaded"] else curses.A_DIM))
                put(y, 5, shown, col(0 if r["loaded"] else curses.A_DIM))
                put(y, w - len(extra) - 2, extra, col(curses.A_DIM))
                continue
            if kind == "plugin":
                name, on = r["name"], r["on"]
                parts = ["off here (user)" if r["off_here"] else r["scope"], f"{r['skills']} skills" if r["skills"] else "", f"{r['tools']} tools" if r["tools"] else ""]
                if not on and (r["skills"] or r["tools"]):
                    parts.insert(1, "still loaded: r")  # 设置已关, 但这个会话里还加载着, 按 r (/reload-plugins) 才卸掉
            elif kind == "mcp":
                name, on = r["name"], r["on"]
                big = tok_of(u, "Other MCP tools, by server", norm(name), 0)
                parts = [r["scope"], f"{r['tools']} tools" if r["tools"] else "-", f"{big / 1000:.1f}k" if big else ""]
            else:
                name, on = r["name"], r["state"] == "on" and not r.get("plugin_off")
                t_ = tok_of(u, "Skills", name, 0)
                parts = ["plugin off: r" if r.get("plugin_off") else (SHORT.get(r["state"], r["state"]) + (" (local)" if r["local"] else "")) if (r["state"] != "on" or r["local"]) else "", r["src"], f"~{t_} tok" if t_ else ""]
            room = w - 5 - len(name) - 4
            parts = [p for p in parts if p]
            while parts and len(" · ".join(parts)) > room:  # 放不下就从右边起丢 (最后丢状态/来源)
                parts.pop()
            extra = " · ".join(parts)
            box = "[x]" if on else ("[~]" if kind == "skill" and r["state"] in STATES[1:3] else "[ ]")
            put(y, 1, box, col(GREEN if on else RED))
            put(y, 5, name, col(0 if on else curses.A_DIM))
            if extra:
                put(y, w - len(extra) - 2, extra, col(curses.A_DIM))
        if not snap.get("sess") and not m.agent and not agmux_bin():
            for j, (txt, a_) in enumerate((("No agent in this pane.", 0), ("", 0), ("Install agmux to resume past sessions here:", curses.A_DIM), ("", 0),
                                           ("brew install kackerx/tap/agmux", GREEN), ("npm i -g @kackerx/agmux", GREEN), ("go install github.com/kackerx/agmux@latest", GREEN),
                                           ("", 0), (AGMUX_URL, CYAN))):
                put(4 + j, 1, txt, a_)
        if T == "Agents" and (picker or menu):
            ci = picker["card"] if picker else menu["card"]
            cy = body0 + (ci - top[T]) * rowh
            if picker:  # 头像选择框: 在这张卡正下方 (放不下就在上方), 5 列格子, 当前的先选着
                cw_, ch_ = 9, AG.ROWS + 1  # 一格 9 列: 头像 6 列居中, 标签 8 列
                ncol = max(1, min(5, (w - 4) // cw_))
                picker["ncol"] = ncol
                need = (len(picker["ids"]) + ncol - 1) // ncol
                below, above = body0 + body_h - (cy + rowh), cy - body0
                room_ = max(below, above)
                prow = max(1, min(need, (room_ - 2) // ch_))
                bh, bw = prow * ch_ + 2, w - 1  # 整行宽, 免得两边露出被盖住的卡片
                by = cy + rowh if below >= bh or below >= above else cy - bh
                rsel = picker["i"] // ncol
                picker["top"] = rsel if rsel < picker["top"] else rsel - prow + 1 if rsel >= picker["top"] + prow else picker["top"]
                overlay_box(0, by, bw, bh, "avatar", ORANGE)
                for k2, aid in enumerate(picker["ids"]):
                    rr, cc = k2 // ncol - picker["top"], k2 % ncol
                    if not 0 <= rr < prow:
                        continue
                    y, x = by + 1 + rr * ch_, 2 + cc * cw_
                    p_ = (AG.frames(avail[aid], "idle") or [None])[0]
                    got = imgs.id_for(p_) if (IMG and p_) else None
                    if got:
                        av_cells.append((x + 1, y, got[0], got[1]))
                        for r_ in range(AG.ROWS):
                            put(y + r_, x + 1, "█" * got[1], curses.A_DIM)
                    else:
                        face(x + 1, y, aid, "idle")
                    lab = aid[:8].center(8)
                    put(y + AG.ROWS, x, lab, curses.A_REVERSE | ORANGE if k2 == picker["i"] else curses.A_DIM)
                picker["rect"] = (0, by, bw, bh)
            else:  # 右键菜单: 图标 + 文字, 在点的位置附近
                labels = [f"{ic} {lb}" for ic, lb, _ in menu["items"]]
                bw, bh = max(len(x_) for x_ in labels) + 4, len(labels) + 2
                bx = max(0, min(menu["x"], w - 1 - bw))
                by = max(body0, min(menu["y"], h - 5 - bh))
                overlay_box(bx, by, bw, bh, "", ORANGE)
                for k2, lb in enumerate(labels):
                    put(by + 1 + k2, bx + 1, (" " + lb).ljust(bw - 2), (curses.A_REVERSE | ORANGE) if k2 == menu["i"] else 0)
                menu["rect"] = (bx, by, bw, bh)
        btns = []
        if h >= 10:
            if box_h:  # tips 框: 圆角边框, 标题 tips (打字过滤时标题是 filter)
                y0, title = h - 4 - box_h, "filter" if (flt[T] or typing) else "tips"
                put(y0, 0, "╭─ " + title + " " + "─" * max(0, w - 6 - len(title)) + "╮", curses.A_DIM)
                for j, ln in enumerate(slines):
                    put(y0 + 1 + j, 0, "│", curses.A_DIM)
                    put(y0 + 1 + j, 2, ln, sattr)
                    put(y0 + 1 + j, w - 2, "│", curses.A_DIM)
                put(y0 + box_h - 1, 0, "╰" + "─" * max(0, w - 3) + "╯", curses.A_DIM)
            sep(h - 4)
            if typ[T] != "all":
                put(h - 4, 2, f" type: {typ[T]} · t ", CYAN | curses.A_BOLD)
            if len(items) > vis:
                pos = f" {top[T] + 1}-{min(len(items), top[T] + vis)}/{len(items)} "
                put(h - 4, w - len(pos) - 2, pos, curses.A_DIM)
            if pending:  # 删除确认: 占掉按钮和说明区, 红字, y 执行, 其它键取消
                for j, ln in enumerate(textwrap.wrap(pending[0], max(10, w - 3))[:3]):
                    put(h - 3 + j, 1, ln, curses.A_BOLD | RED)
            else:
                put(h - 3, 1, "[ Reload ]", curses.A_REVERSE | ORANGE)
                btns.append((h - 3, 1, 11, "reload"))
                if T == "Context":
                    put(h - 3, 13, "[ Refresh ]" if not busy else "[ ... ]", curses.A_REVERSE | CYAN)
                    btns.append((h - 3, 13, 12, "usage"))
                helps = [("j/k", "move"), ("h/l", "tab")] + ([("space", "cycle" if T == "Skills" else "toggle")] if T in ("Plugin", "MCP", "Skills") else []) + ([("enter", "edit")] if T in ("Skills", "Context") else []) + ([("enter", "open"), ("t", "pin"), ("a", "avatar"), ("x", "close"), ("m", "menu")] if T == "Agents" else []) \
                    + [("q", "close"), ("/", "filter")] + ([("t", "type")] if T != "Agents" else []) + [("r", "reload"), ("u", "refresh")] + ([("U", "update")] if T == "Plugin" else []) + ([("D", "delete")] if T in ("Plugin", "MCP", "Skills") else [])  # 最不常用的放最后: 窄了先丢它们
                line, ly = 1, h - 2
                for key, what in helps:
                    n = len(key) + 1 + len(what) + 1
                    if line + n > w - 1 and line > 1:
                        ly, line = ly + 1, 1
                    if ly > h - 1:
                        break
                    put(ly, line, key, curses.A_BOLD)
                    put(ly, line + len(key) + 1, what, curses.A_DIM)
                    line += n
        scr.refresh()
        if IMG:
            imgs.draw(av_cells)  # 占位符不是 curses 能写的字符: refresh 之后直接写进 tty

        def act(idx, enter=False):
            kind, r = items[idx]
            if kind == "card":
                m.go_card(r)
            elif kind in ("file", "agent") or (kind == "skill" and enter):  # 回车: 编辑 (skill 只有自己的/项目的才有文件)
                p = r["path"]
                m.edit(os.path.join(p, "SKILL.md") if kind == "skill" and p and os.path.isdir(p) else p)
            elif kind == "mcp":
                m.toggle("mcp", r)
            elif kind == "plugin":
                m.toggle("plugin", r)
            elif kind == "skill":
                m.toggle("skill", r)

        def card_menu(r_):
            """右键 / m 菜单的项: (nerd font 图标, 文字, 动作); Handoff 只给 herdr pane 里、装了 handoff skill 的 Claude 会话 (后台会话没法从外面发命令)"""
            return [("\uf08e", "Open", "open"), ("\uf08d", "Unpin" if r_["pinned"] else "Pin", "pin"), ("\uf2bd", "Change avatar", "avatar")] + \
                ([("\uf064", "Handoff", "handoff")] if r_.get("sid") and r_.get("pane") and has_handoff(r_["cwd"]) else []) + [("\uf00d", "Close agent", "close")]

        def card_action(what, idx):
            """卡片上的动作 (键位和右键菜单共用): open / pin / avatar / handoff / close"""
            nonlocal picker, pending
            if not items or idx >= len(items) or items[idx][0] != "card":
                return
            r_ = items[idx][1]
            if what == "open":
                m.go_card(r_)
            elif what == "pin":
                on_ = AG.toggle_pin(r_["name"])
                m.patch_agent(r_["name"], pinned=on_, pin_at=(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if on_ else ""))
                m.say(("✓ pinned " if on_ else "✓ unpinned ") + r_["name"])
            elif what == "avatar":
                if avail:
                    ids = list(avail)
                    picker = {"ids": ids, "i": ids.index(r_.get("avatar")) if r_.get("avatar") in ids else 0, "top": 0, "name": r_["name"], "card": idx, "ncol": 5}
                else:
                    m.say("✗ no avatar sets found (plugin avatars/ or config dir avatars/)")
            elif what == "handoff":
                def go(r_=r_):  # agent 忙时 herdr 会排队、等权限时拒绝, 不卡界面
                    out = run(H, "agent", "prompt", r_["pane"], "/handoff")
                    err = re.search(r'"message":"([^"]*)"', out)
                    m.say("✗ " + (err.group(1) if err else "no response from herdr") if err or not out else "✓ /handoff sent to " + r_["name"])
                threading.Thread(target=go, daemon=True).start()
            elif what == "close":
                how = "stop this background session" if r_["bg"] else "close its herdr pane (ends the session)"
                pending = (f"Close agent {r_['name']}: {how}? y = yes", lambda r_=r_: m.close_agent(r_))

        def go_tab(i):
            nonlocal tab
            tab = i % len(TABS)
            if TABS[tab] == "Agents":
                m.agents_now.set()
            if TABS[tab] == "Context" and m.sid and (m.stale or time.time() - m.usage["ts"] > USAGE_TTL):
                m.fetch_usage()

        now = time.time()
        if now - saved_at > 0.3:  # 变了才落盘 (每 0.3 秒最多一次); toggle 关面板是直接杀进程, 没有退出时机, 所以只能边用边存
            blob = json.dumps({"ui": {"tab": tab, "sel": sel, "top": top, "flt": flt, "typ": typ}, "snap": snap, "ctx": m.ctx, "v": SNAP_V}, ensure_ascii=False, sort_keys=True)
            if blob != saved:
                try:
                    os.makedirs(STATE_DIR, exist_ok=True)
                    with open(keep + ".tmp", "w") as f:
                        f.write(blob)
                    os.replace(keep + ".tmp", keep)
                    saved = blob
                except OSError:
                    pass
            saved_at = now
        k = scr.getch()
        if k == -1:
            continue
        if pending and k != curses.KEY_MOUSE:
            if k == ord("y"):
                pending[1]()
            else:
                m.say("cancelled")
            pending = None
            continue
        if picker and k != curses.KEY_MOUSE:
            nc = picker.get("ncol", 5)
            step = {ord("h"): -1, curses.KEY_LEFT: -1, ord("l"): 1, curses.KEY_RIGHT: 1, ord("j"): nc, curses.KEY_DOWN: nc, ord("k"): -nc, curses.KEY_UP: -nc}.get(k)
            if step:
                picker["i"] = max(0, min(len(picker["ids"]) - 1, picker["i"] + step))
                m.say("avatar: " + picker["ids"][picker["i"]])
            elif k in (10, 13, curses.KEY_ENTER):
                aid = picker["ids"][picker["i"]]
                AG.bind(picker["name"], aid)
                m.patch_agent(picker["name"], avatar=aid)
                m.say(f"✓ {picker['name']} → {aid}")
                picker = None
            elif k in (27, ord("q"), ord("a")):
                picker = None
            continue
        if menu and k != curses.KEY_MOUSE:
            if k in (ord("j"), curses.KEY_DOWN):
                menu["i"] = (menu["i"] + 1) % len(menu["items"])
            elif k in (ord("k"), curses.KEY_UP):
                menu["i"] = (menu["i"] - 1) % len(menu["items"])
            elif k in (10, 13, curses.KEY_ENTER):
                what, ci = menu["items"][menu["i"]][2], menu["card"]
                menu = None
                card_action(what, ci)
            elif k in (27, ord("q")):
                menu = None
            continue
        if k == curses.KEY_MOUSE:
            pending = None
            try:
                _, mx, my, _, b = curses.getmouse()
            except curses.error:
                continue
            if b & getattr(curses, "BUTTON4_PRESSED", 0):
                sel[T] = max(0, sel[T] - 3)
            elif b & getattr(curses, "BUTTON5_PRESSED", 0):
                sel[T] += 3
            elif b & (curses.BUTTON3_PRESSED | curses.BUTTON3_CLICKED | curses.BUTTON3_RELEASED):  # 右键: 卡片上弹菜单
                if T == "Agents" and my in hit and not picker:
                    sel[T] = hit[my]
                    r_ = items[sel[T]][1]
                    menu = {"card": sel[T], "i": 0, "x": mx, "y": my, "items": card_menu(r_)}
            elif b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):  # 按下和释放贴得很近时 ncurses 合成 CLICKED 而不报 PRESSED
                if picker:  # 头像框: 点格子 = 选中, 再点同一个 = 绑定; 点框外 = 关
                    bx, by, bw_, bh_ = picker.get("rect", (0, 0, 0, 0))
                    if bx <= mx < bx + bw_ and by <= my < by + bh_:
                        cw_, ch_ = 9, AG.ROWS + 1
                        k2 = ((my - by - 1) // ch_ + picker["top"]) * picker["ncol"] + (mx - 2) // cw_
                        if 0 <= k2 < len(picker["ids"]) and (mx - 2) // cw_ < picker["ncol"]:
                            if k2 == picker["i"]:
                                aid = picker["ids"][k2]
                                AG.bind(picker["name"], aid)
                                m.patch_agent(picker["name"], avatar=aid)
                                m.say(f"✓ {picker['name']} → {aid}")
                                picker = None
                            else:
                                picker["i"] = k2
                    else:
                        picker = None
                    continue
                if menu:  # 右键菜单: 点项 = 执行, 点框外 = 关
                    bx, by, bw_, bh_ = menu.get("rect", (0, 0, 0, 0))
                    if bx < mx < bx + bw_ - 1 and by < my < by + bh_ - 1:
                        what, ci = menu["items"][my - by - 1][2], menu["card"]
                        menu = None
                        card_action(what, ci)
                    else:
                        menu = None
                    continue
                bt = next((x for x in btns if my == x[0] and x[1] <= mx < x[1] + x[2]), None)
                if bt:
                    m.reload() if bt[3] == "reload" else m.fetch_usage()
                elif 1 <= my < body0 - 1:
                    t_ = next((x[3] for x in tab_hits if x[0] == my and x[1] <= mx < x[1] + x[2]), None)
                    if t_ is not None:
                        go_tab(t_)
                elif my in hit:
                    prev, sel[T] = sel[T], hit[my]
                    kind_ = items[sel[T]][0]
                    if kind_ in ("file", "agent", "card"):  # 打开类: 第一次点只选中, 在已选中的上再点才打开
                        if prev == sel[T]:
                            act(sel[T])
                    elif mx <= 5 and kind_ != "text":  # 复选框: 点一下就切换
                        act(sel[T])
            continue
        if typing:
            if k in (10, 13):
                typing = False
            elif k == 27:
                typing, flt[T] = False, ""
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                flt[T] = flt[T][:-1]
            elif 32 <= k < 127:
                flt[T] += chr(k)
            sel[T] = 0
            continue
        c = chr(k) if 0 <= k < 256 else ""
        if c != "g":
            pend_g = False
        if c == "j" or k == curses.KEY_DOWN:
            sel[T] += 1
        elif c == "k" or k == curses.KEY_UP:
            sel[T] -= 1
        elif c == "g":
            if pend_g:
                sel[T], pend_g = 0, False
            else:
                pend_g = True
        elif c == "G":
            sel[T] = len(items) - 1
        elif k == 4:
            sel[T] += max(1, vis // 2)
        elif k == 21:
            sel[T] -= max(1, vis // 2)
        elif c in ("l", "L", "\t") or k == curses.KEY_RIGHT:
            go_tab(tab + 1)
        elif c in ("h", "H") or k in (curses.KEY_BTAB, curses.KEY_LEFT):
            go_tab(tab - 1)
        elif c in "12345" and c:
            go_tab(int(c) - 1)
        elif (c in (" ", "x", "\n", "\r") and not (c == "x" and T == "Agents")) or k == curses.KEY_ENTER:
            if items and items[sel[T]][0] != "text":
                act(sel[T], enter=c in ("\n", "\r") or k == curses.KEY_ENTER)
        elif c == "/":
            typing, flt[T] = True, ""
        elif c == "r":
            m.reload()
        elif c == "u":
            m.fetch_usage()
        elif c == "m" and T == "Agents" and items and items[sel[T]][0] == "card":
            r_ = items[sel[T]][1]
            menu = {"card": sel[T], "i": 0, "x": 4, "y": body0 + (sel[T] - top[T]) * rowh + 2, "items": card_menu(r_)}
        elif c in ("t", "a", "x") and T == "Agents":
            card_action({"t": "pin", "a": "avatar", "x": "close"}[c], sel[T])
        elif c == "t" and T != "":
            cyc = type_cycle(snap, u, T)
            typ[T] = cyc[(cyc.index(typ[T]) + 1) % len(cyc)] if typ[T] in cyc else "all"
            sel[T] = top[T] = 0
        elif c == "U" and items and items[sel[T]][0] != "text" and items[sel[T]][0] != "file":
            m.update(items[sel[T]][0], items[sel[T]][1])
        elif c == "D" and items and items[sel[T]][0] in ("plugin", "mcp", "skill"):
            msg, fn = m.ask_delete(items[sel[T]][0], items[sel[T]][1])
            if fn:
                pending = (msg, fn)
            else:
                m.say("✗ " + msg)
        elif k == 27:
            flt[T] = ""  # ESC 只清过滤, 不退出: 认不出的转义序列 (如终端发了没协商过的鼠标编码) 开头也是 ESC, 不能因此关面板
        elif c == "q":
            break
    stop.set()
    if ME:
        run(H, "pane", "close", ME)


if __name__ == "__main__":
    if not TARGET:
        print("AGENT_PANEL_TARGET is not set (open the panel with the toggle action)")
        raise SystemExit(1)
    try:
        try:
            EARLY = json.loads(run(H, "pane", "get", TARGET))["result"]["pane"]
        except (ValueError, KeyError):
            EARLY = {}
        if not EARLY.get("agent") and time.time() > float(os.environ.get("AP_RECALL_HOLD", 0)):  # 没有 agent: 不画空面板, 直接换成 recall (没装 agmux 才继续往下)
            launch_recall(EARLY.get("foreground_cwd") or EARLY.get("cwd"))
        curses.wrapper(main)
    except Exception:  # 崩了 pane 会跟着消失, 留个日志好查
        import traceback
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "panel.log"), "a") as f:
            f.write(traceback.format_exc() + "\n")
        raise
