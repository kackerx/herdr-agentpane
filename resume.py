#!/usr/bin/env python3
"""面板里的 `agmux recall` 选中会话后 exec 的「claude / pi」替身 (panel.py 把 shim 目录放进 agmux 的 PATH 最前)。

agmux 的 enter = 在当前终端 cd 到会话目录再 exec `claude --resume <id>`; 在面板里这样会把面板自己变成 claude。
这里改成: 把同一条命令发进【面板服务的那个 pane】, 然后面板变回普通 agent 面板。"""
import os, shlex, subprocess, sys, time

name = os.path.basename(sys.argv[0])  # shim 目录里 claude / pi 都是指向本文件的软链
cmd = f"cd {shlex.quote(os.getcwd())} && {name} " + " ".join(shlex.quote(a) for a in sys.argv[1:])
subprocess.run([os.environ.get("HERDR_BIN_PATH") or "herdr", "pane", "run", os.environ["AGENT_PANEL_TARGET"], cmd])

shim = os.path.dirname(os.path.abspath(sys.argv[0]))
env = dict(os.environ)
env["PATH"] = ":".join(p for p in env.get("PATH", "").split(":") if p != shim)  # 面板自己还要调真的 claude
env["AP_RECALL_HOLD"] = str(int(time.time()) + 90)  # 会话起来之前别又切回 recall
here = os.path.dirname(os.path.realpath(__file__))
os.execvpe("python3", ["python3", os.path.join(here, "panel.py")], env)
