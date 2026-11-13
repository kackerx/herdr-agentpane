#!/usr/bin/env python3
"""Agents 页的数据和头像 (panel.py 用), 不依赖 agmux。
会话: `claude agents --json` (前台 interactive + 后台 background) 并上 `herdr agent list` (哪个在哪个 pane、状态; 不是 claude 的 agent 也算);
模型 / 上下文: herdr pane 上报的 tokens.model / tokens.ctx 优先 (statusline 用 `herdr pane report-metadata` 报的), 没有就读 transcript 尾部估算。
头像绑定 (会话名 → 头像 id) 和置顶 (会话名 → RFC 3339 时间) 存在插件状态目录的 avatars.json / pins.json; 第一次运行时若装过 agmux,
从它的同名文件拷一份过来 (两边格式一样)。头像集: 配置目录 avatars/<id>/<state>/*.png 盖住插件自带的 avatars/。

头像显示用 kitty 图形协议的 Unicode 占位符 (和 agmux board 同一套): 图片上传一次拿到 id, 之后卡片里只是一串占位符字符,
前景色 = 图片 id。curses 没法写这种字符 (私有区码点 + 组合标记), 所以在 scr.refresh() 之后直接往 tty 写原始序列。
herdr 的 pane 实测会回应 kitty 图形查询 (OK), 占位符是否显示要看宿主终端; 不支持时画文字头像。
"""
import base64, datetime, glob, hashlib, json, os, re, shutil, struct, subprocess, time, zlib

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp"
CONF_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or HERE
AVATARS_JSON = os.path.join(STATE_DIR, "avatars.json")
PINS_JSON = os.path.join(STATE_DIR, "pins.json")
SET_DIRS = [os.path.join(CONF_DIR, "avatars"), os.path.join(HERE, "avatars")]  # 用户的盖住自带的
AGMUX_STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local/state"), "agmux")
for _f in (AVATARS_JSON, PINS_JSON):  # 一次性: 装过 agmux 就沿用它的头像绑定和置顶
    if not os.path.exists(_f) and os.path.isfile(os.path.join(AGMUX_STATE, os.path.basename(_f))):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            shutil.copyfile(os.path.join(AGMUX_STATE, os.path.basename(_f)), _f)
        except OSError:
            pass
SLOT, ROWS = 6, 3  # 头像: 6 列 × 3 行 (终端格子约 1:2, 所以是正方形); 所有图统一裁成正方形再放, 大小一致、不变形
DIAC = (0x0305, 0x030D, 0x030E, 0x0310, 0x0312, 0x033D, 0x033E, 0x033F, 0x0346, 0x034A, 0x034B, 0x034C)  # kitty 的行/列号 0..11 (官方 rowcolumn-diacritics 的前 12 个; 槽最宽 9 列)
STATE_DIRS = {"idle": ("idle",), "think": ("think", "working"), "hi": ("hi", "attention"), "success": ("success", "done")}


def jload(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def jsave(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(p + ".tmp", p)


def run(*a, t=10):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=t).stdout
    except Exception:
        return ""


# ── 头像集 ──
def sets():
    """id → 目录; 用户目录里同 id 的盖住内置的; 只算有 idle 帧的"""
    out = {}
    for base in reversed(SET_DIRS):
        for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            p = os.path.join(base, d)
            if frames(p, "idle"):
                out[d] = p
    return dict(sorted(out.items(), key=lambda kv: (kv[1].startswith(SET_DIRS[0]) is False, kv[0])))  # 用户的排前面


def frames(set_dir, state):
    """某个状态的帧文件 (排序; 跳过 idle_v2.png 这种「另一版」)"""
    for n in STATE_DIRS.get(state, (state,)):
        d = os.path.join(set_dir, n)
        if os.path.isdir(d):
            fs = sorted(f for f in os.listdir(d) if f.endswith(".png") and not f.startswith(".") and not re.search(r"_v\d+\.png$", f))
            if fs:
                return [os.path.join(d, f) for f in fs]
    return []


def fnv32a(s):
    h = 0x811C9DC5
    for b in s.encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def avatar_of(name, avail):
    """绑定的 (avatars.json) 优先, 没绑或绑的这台没有 → 按名字稳定挑一个; 没有任何头像集 → None"""
    bound = jload(AVATARS_JSON).get(name)
    if bound in avail:
        return bound
    ids = list(avail)
    return ids[fnv32a(name) % len(ids)] if ids else None


def bind(name, aid):
    d = jload(AVATARS_JSON)
    d[name] = aid
    jsave(AVATARS_JSON, d)


# ── 置顶 ──
def toggle_pin(name):
    d = jload(PINS_JSON)
    if name in d:
        d.pop(name)
        pinned = False
    else:
        d[name] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        pinned = True
    jsave(PINS_JSON, d)
    return pinned


# ── 会话 ──
_git = {}


def repo_key(cwd):
    """同一个项目 = 同一个 git common dir (worktree 也算); 不在 git 里就是目录本身"""
    if cwd not in _git:
        d = cwd
        while d and not os.path.isdir(d) and os.path.dirname(d) != d:  # 进程所在目录被删了 (cd 进去后删掉): 按还在的上级算
            d = os.path.dirname(d)
        out = run("git", "-C", d, "rev-parse", "--path-format=absolute", "--git-common-dir").strip() if d else ""
        _git[cwd] = out or cwd
    return _git[cwd]


def tree_key(cwd):
    """同一个 worktree = 同一个 git 顶层目录 (repo_key 是整个仓库, 各 worktree 算一个); 不在 git 里就是目录本身"""
    if ("tree", cwd) not in _git:
        d = cwd
        while d and not os.path.isdir(d) and os.path.dirname(d) != d:
            d = os.path.dirname(d)
        _git[("tree", cwd)] = (run("git", "-C", d, "rev-parse", "--show-toplevel").strip() if d else "") or cwd
    return _git[("tree", cwd)]


def branch_of(cwd):
    return run("git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD", t=3).strip()


def sort_rows(rows):
    rows.sort(key=lambda r: (r["bg"], not r["pinned"], r["pin_at"], r["name"].lower()))  # 前台在前; 置顶按置顶先后, 其余按名字
    return rows


STATUS = {"busy": "working", "running": "working", "waiting": "blocked", "needs_input": "blocked"}  # claude agents 的状态 → 卡片用的 (和 herdr 一致)


def pct(v):
    """"▰▱▱ 11%" / "11%" / 11 → 11; 认不出 → -1"""
    m = re.search(r"(\d+)\s*%", str(v or ""))
    return int(m.group(1)) if m else (int(v) if isinstance(v, int) else -1)


_stats = {}


def transcript_stats(sid, cwd):
    """-> (模型, 上下文 %, 角色) 从会话 transcript 估: 最后一条 assistant 的 usage (输入 + 缓存读写) / 窗口; 角色 = agent-setting。按 mtime 缓存"""
    if not sid:
        return "", -1, ""
    p = os.path.join(HOME, ".claude/projects", re.sub(r"[/.]", "-", cwd or ""), sid + ".jsonl")
    if not os.path.isfile(p):
        p = next(iter(glob.glob(os.path.join(HOME, ".claude/projects/*", sid + ".jsonl"))), "")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return "", -1, ""
    if _stats.get(p, (None,))[0] == mt:
        return _stats[p][1]
    model, used, role = "", -1, ""
    try:
        with open(p, "rb") as f:
            head = f.read(65536)
            f.seek(max(0, os.path.getsize(p) - 524288))
            tail = f.read()
        m = re.search(rb'"agentSetting":"([^"]+)"', head + tail)
        role = m.group(1).decode() if m else ""
        for line in reversed(tail.splitlines()):
            if b'"type":"assistant"' in line and b'"usage"' in line:
                msg = json.loads(line).get("message") or {}
                u = msg.get("usage") or {}
                used = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                model = msg.get("model") or ""
                break
    except (OSError, ValueError):
        pass
    # ponytail: 窗口按模型猜 (haiku 200k, 其它 1M, 和 Claude Code 5.x 的默认一致); 改了默认窗口或用了别的模型就会偏, 到时读 statusline 的 used_percentage
    win = 200_000 if "haiku" in model else 1_000_000
    nice = re.sub(r"^claude-([a-z]+)-(\d+)-(\d+).*", lambda mm: f"{mm.group(1).title()} {mm.group(2)}.{mm.group(3)}", model)
    out = (nice, min(100, used * 100 // win) if used >= 0 else -1, role)
    _stats[p] = (mt, out)
    return out


def fetch(cwd, herdr_bin="herdr", workspace=None):
    """面板所在 herdr workspace 里的 agent + 同一个 worktree 里不在 herdr pane 里的会话 (后台 / 别的终端里开的)。
    前台在前、后台在后, 各自置顶的在最前。前台的带 herdr pane id (点了跳过去); 后台的带 attach 用的短 id。
    不传 workspace (面板拿不到) 就只按 worktree 算"""
    tree = tree_key(cwd)

    def mine(a, c):  # 在 herdr pane 里: 按 workspace; 不在: 按 worktree
        return a.get("workspace_id") == workspace if a and workspace else tree_key(c or "") == tree
    panes, herd = {}, []
    try:
        herd = json.loads(run(herdr_bin, "agent", "list") or "{}")["result"].get("agents", [])
    except (ValueError, KeyError):
        pass
    for a in herd:
        tk = a.get("tokens") or {}
        if tk.get("kind") == "bg":
            continue
        a["_name"] = tk.get("name") or a.get("terminal_title_stripped") or a.get("agent") or a["pane_id"]
        panes[(a["_name"], a.get("cwd"))] = a
        panes.setdefault((a["_name"], repo_key(a.get("cwd") or "")), a)  # herdr 报进程当前 cwd (会话里 cd 过就变), claude 报启动目录: 退到同仓库同名
    rows, used = [], set()
    try:
        sessions = json.loads(run("claude", "agents", "--json", t=20) or "[]")
    except ValueError:
        sessions = []
    for s in sessions:
        if not s.get("cwd"):
            continue
        bg = s.get("kind") == "background"
        name = s.get("name") or (s.get("sessionId") or "")[:8]
        a = None if bg else panes.get((name, s["cwd"])) or panes.get((name, repo_key(s["cwd"])))
        if not mine(a, s["cwd"]):
            continue
        r = dict(id=s.get("id") or s.get("sessionId"), sid=s.get("sessionId"), name=name, cwd=s["cwd"], bg=bg,
                 status=STATUS.get(s.get("status"), s.get("status") or ""), model="", ctx=-1, role="", pane=None, tab=None)
        if a:
            used.add(a["pane_id"])
            r.update(pane=a["pane_id"], tab=a.get("tab_id"), status=a.get("agent_status") or r["status"])
        rows.append((r, a))
    for a in herd:  # herdr 里有、claude 列表里没有的 (别的 agent, 比如 pi)
        if "_name" in a and a["pane_id"] not in used and a["_name"] not in {r["name"] for r, _ in rows} and mine(a, a.get("cwd")):
            rows.append((dict(id=a["pane_id"], sid=None, name=a["_name"], cwd=a.get("cwd"), bg=False, status=a.get("agent_status") or "",
                              model="", ctx=-1, role="", pane=a["pane_id"], tab=a.get("tab_id")), a))
    pins, avail = jload(PINS_JSON), sets()
    out = []
    for r, a in rows:
        tk = (a or {}).get("tokens") or {}
        r["model"], r["ctx"] = tk.get("model") or "", pct(tk.get("ctx"))
        if not r["model"] or r["ctx"] < 0:
            m_, c_, role = transcript_stats(r["sid"], r["cwd"])
            r["model"], r["ctx"], r["role"] = r["model"] or m_, r["ctx"] if r["ctx"] >= 0 else c_, role
        else:
            r["role"] = transcript_stats(r["sid"], r["cwd"])[2]
        r["branch"] = branch_of(r["cwd"])
        r["pinned"] = r["name"] in pins
        r["pin_at"] = pins.get(r["name"], "")
        r["avatar"] = avatar_of(r["name"], avail)
        out.append(r)
    return sort_rows(out)


# ── kitty 图片 ──
def write_all(data):
    fd, view = 1, memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except BlockingIOError:
            time.sleep(0.002)
            continue
        view = view[n:]


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def crop_square(raw):
    """PNG 居中裁成正方形 (横图裁左右, 竖图裁上下); 只认 8 位 RGB / RGBA 非交错 (agmux 的头像集都是), 别的原样返回。
    kitty 对 Unicode 占位符的虚拟放置不认裁剪参数, 所以得自己裁像素 (agmux 也是这样)。没有 PIL, 手写: 解 filter → 切 → 不压缩 filter 重新编码"""
    if raw[:8] != PNG_SIG:
        return raw
    chunks, pos = [], 8
    while pos + 8 <= len(raw):
        n, typ = struct.unpack(">I4s", raw[pos:pos + 8])
        chunks.append((typ, raw[pos + 8:pos + 8 + n]))
        pos += 12 + n
    ihdr = next((d for t, d in chunks if t == b"IHDR"), None)
    if not ihdr:
        return raw
    w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", ihdr)
    if bd != 8 or ct not in (2, 6) or il or w == h:
        return raw
    bpp = 3 if ct == 2 else 4
    data = zlib.decompress(b"".join(d for t, d in chunks if t == b"IDAT"))
    stride, rows, prev = w * bpp, [], bytearray(w * bpp)
    for y in range(h):
        f, line = data[y * (stride + 1)], bytearray(data[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b_ = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b_) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b_) // 2) & 255
            elif f == 4:
                p = a + b_ - c
                pa, pb, pc = abs(p - a), abs(p - b_), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b_ if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    side = min(w, h)
    x0, y0 = (w - side) // 2, (h - side) // 2
    body = b"".join(b"\x00" + bytes(r[x0 * bpp:(x0 + side) * bpp]) for r in rows[y0:y0 + side])

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, ct, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(body, 6)) + chunk(b"IEND", b"")


def squared(path):
    """裁好的正方形 PNG 字节; 磁盘缓存 (按路径 + 修改时间), 解码不快, 每张只做一次"""
    cache = os.path.join(os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp", "avatar-square")
    key = hashlib.sha1(f"{path}|{os.path.getmtime(path)}|v1".encode()).hexdigest()[:16] + ".png"
    p = os.path.join(cache, key)
    try:
        return open(p, "rb").read()
    except OSError:
        pass
    out = crop_square(open(path, "rb").read())
    try:
        os.makedirs(cache, exist_ok=True)
        open(p, "wb").write(out)
    except OSError:
        pass
    return out


class Images:
    """图片上传一次, 之后只画占位符。id 取 1..250 (用 256 色索引当前景色, curses 能设; agmux 用 24 位 id, 两边撞上的可能很小)"""

    def __init__(self):
        self.ids, self.nxt = {}, 1  # path → (id, 列数)

    def id_for(self, path):
        """-> (id, 列数) 或 None; 图统一裁成正方形, 列数恒为 SLOT"""
        if path in self.ids:
            return self.ids[path]
        if self.nxt > 250:
            return None
        try:
            raw = squared(path)
        except (OSError, zlib.error, struct.error):
            return None
        cols = SLOT
        b64 = base64.b64encode(raw).decode()
        i, self.nxt = self.nxt, self.nxt + 1
        chunks = [b64[o:o + 4096] for o in range(0, len(b64), 4096)] or [""]
        for n, c in enumerate(chunks):
            more = 0 if n == len(chunks) - 1 else 1
            head = f"a=T,U=1,f=100,q=2,i={i},c={cols},r={ROWS},m={more}" if n == 0 else f"m={more}"
            write_all(f"\x1b_G{head};{c}\x1b\\".encode())
        self.ids[path] = (i, cols)
        return self.ids[path]

    def draw(self, cells):
        """cells: [(x, y, id, 列数)] 槽左上角的屏幕位置 (0 起), 图在槽里居中; 在 curses refresh 之后调。
        DECSC/DECRC 保存还原光标, 免得 curses 的光标记账错位"""
        if not cells:
            return
        out = ["\x1b7"]
        for x, y, i, cols in cells:
            out.append(f"\x1b[38;5;{i}m")
            for r in range(ROWS):
                out.append(f"\x1b[{y + r + 1};{x + (SLOT - cols) // 2 + 1}H" + "".join(chr(0x10EEEE) + chr(DIAC[r]) + chr(DIAC[c]) for c in range(cols)))
        out.append("\x1b[39m\x1b8")
        write_all("".join(out).encode())


def state_of(status):
    """会话状态 → 头像用的状态目录"""
    return "think" if status == "working" else "idle" if status in ("idle", "", "done") else "hi"


def frame_path(set_dir, status, tick):
    fs = frames(set_dir, state_of(status)) or frames(set_dir, "idle")
    return fs[(tick % len(fs)) if len(fs) > 1 and state_of(status) != "idle" else 0] if fs else None
