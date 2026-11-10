#!/usr/bin/env python3
"""开/关当前聚焦 agent 的右侧面板 (面板 pane 里再按一次 = 关)。"""
import json, os, subprocess, sys

H = os.environ.get("HERDR_BIN_PATH") or "herdr"
STATE = os.path.join(os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp", "panels.json")
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "kackerx.agentpane"  # 面板 pane → 它服务的 agent pane


def herdr(*a):
    r = subprocess.run([H, *a], capture_output=True, text=True, timeout=15)
    return json.loads(r.stdout)["result"] if r.stdout.strip().startswith("{") else {}


BORDER = 2  # pane 外框占的列数: 面板进程里量到的宽度 (curses) = pane 外宽 - 2


def want_rect(area):
    """目标 pane 外宽: 记住的上次宽度 (拖动后面板自己写进 STATE/width, 是内宽) > 配置目录 config.json 的 width (数字 = 内宽列数, "30%" = 占整个区域宽度的百分比)"""
    conf_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(conf_dir, "config.json")) as f:
            conf = json.load(f)
    except Exception:
        conf = {}
    w = conf.get("width", 52)
    if conf.get("remember", True):
        try:
            with open(os.path.join(os.path.dirname(STATE), "width")) as f:
                w = int(f.read().strip())
        except (OSError, ValueError):
            pass
    w = area * float(w[:-1]) / 100 if isinstance(w, str) and w.endswith("%") else float(w) + BORDER
    return int(max(30, min(w, area - 40)))


def fit(panel):
    """新开的右分屏默认占一半, 调到目标宽度; resize 的 amount 是被拖动那条分割线所在区域宽度的比例, 取整有 ±1 列误差, 所以量了再补一轮; 差一列时只挪半列 (比例是连续值, 整列会在 x.5 的取整边界上来回跳 53↔55)"""
    for _ in range(3):
        lay = herdr("pane", "layout", "--pane", panel).get("layout", {})
        rect = next((p["rect"] for p in lay.get("panes", []) if p["pane_id"] == panel), None)
        if not rect:
            return
        delta = rect["width"] - want_rect(lay.get("area", {}).get("width", 200))
        parents = [s["rect"]["width"] for s in lay.get("splits", []) if s["direction"] == "right" and s["rect"]["x"] <= rect["x"] < s["rect"]["x"] + s["rect"]["width"]]
        if not delta or not parents:
            return
        subprocess.run([H, "pane", "resize", "--pane", panel, "--direction", "right" if delta > 0 else "left", "--amount", f"{(abs(delta) if abs(delta) > 1 else 0.5) / min(parents):.4f}"], capture_output=True)


def cut(leaves):
    """在叶子的矩形里找一条贯穿的切线 → (方向, 前半, 后半, 前半占比); 方向 right = 竖线左右分, down = 横线上下分。优先竖线"""
    for d, pos, size in (("right", "x", "width"), ("down", "y", "height")):
        lo = min(l["rect"][pos] for l in leaves)
        hi = max(l["rect"][pos] + l["rect"][size] for l in leaves)
        for c in sorted({l["rect"][pos] + l["rect"][size] for l in leaves} - {hi}):
            a = [l for l in leaves if l["rect"][pos] + l["rect"][size] <= c]
            b = [l for l in leaves if l["rect"][pos] >= c]
            if a and b and len(a) + len(b) == len(leaves):
                return d, a, b, (c - lo) / (hi - lo)
    return None


def first(leaves):
    return min(leaves, key=lambda l: (l["rect"]["y"], l["rect"]["x"]))["pane_id"]


def regroup(node, x, tab):
    """把 node (一组叶子, 几何上是一棵分割树) 重建到 x 所在的格子里。herdr 同 tab 内的 move 是空操作, 必须先弹到临时 tab 再回来"""
    c = cut(node) if len(node) > 1 else None
    if not c:
        return
    d, c1, c2, ratio = c
    y = first(c2)
    herdr("pane", "move", y, "--new-tab", "--no-focus")
    for _ in range(3):  # 回不来 pane 就困在临时 tab 里, 重试
        r = herdr("pane", "move", y, "--tab", tab, "--split", d, "--target-pane", x, "--ratio", f"{ratio:.4f}", "--no-focus")
        if (r.get("move_result") or {}).get("changed"):
            break
    regroup(c1, x, tab)
    regroup(c2, y, tab)


def open_panel(pane):
    """面板永远开成 tab 最右侧的整列。herdr 没有「在根上分屏」, 只能分屏某个叶子:
    1. 竖线切开的部分整体在左, 顺着右半往下找 (右半一定是整个高度);
    2. 找到的右半若是单个 pane, 直接在它右边分屏; 若是上下堆叠的一组, 先在其中一格右边分屏, 再把同组其余 pane 逐个搬进那一格, 让面板包住整组"""
    lay = herdr("pane", "layout", "--pane", pane).get("layout", {})
    region = lay.get("panes", [])
    while len(region) > 1 and (c := cut(region)) and c[0] == "right":
        region = c[2]
    x = first(region) if region else pane
    res = herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "panel", "--placement", "split",
                "--target-pane", x, "--direction", "right", "--focus", "--env", "AGENT_PANEL_TARGET=" + pane)
    new = ((res.get("plugin_pane") or {}).get("pane") or {}).get("pane_id")
    if new and len(region) > 1:
        regroup(region, x, lay.get("tab_id"))
    return new


def main():
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    pane = ctx.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
    if not pane:
        print("no focused pane", file=sys.stderr)
        return 1
    live = {p["pane_id"] for p in herdr("pane", "list").get("panes", [])}
    try:
        with open(STATE) as f:
            panels = {k: v for k, v in json.load(f).items() if k in live}
    except Exception:
        panels = {}
    close = [p for p, target in panels.items() if p == pane or target == pane]
    for p in close:
        subprocess.run([H, "pane", "close", p], capture_output=True)
        panels.pop(p)
    if not close:
        new = open_panel(pane)
        if new:
            panels[new] = pane
            fit(new)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(panels, f)


if __name__ == "__main__":
    sys.exit(main() or 0)
