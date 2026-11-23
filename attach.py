#!/usr/bin/env python3
"""浮窗里跑 $ATTACH_CMD 的 pty 中转, 只为加一个统一的关窗键。
herdr 的 popup 吃掉所有按键 (herdr 前缀在里面不生效), 只在进程退出时关; claude attach 和
herdr agent attach 各有各的退出键 (← / ctrl+b q), 所以在这里统一: ctrl+q 再按 q / x / Esc 关浮窗 (普通编码和 kitty 键盘协议都认)
(和 herdr 的 prefix+q = 离开一致), ctrl+q ctrl+q 发一个原样的 ctrl+q。agent 本身不受影响, 只是断开这个查看端。
herdr agent attach 把 ctrl+b 当自己的前缀吃掉 (ctrl+b ctrl+b 才发原样的), 关窗已经有 ctrl+q 了, 所以这里把 ctrl+b 发两遍, 让 Claude Code 收到。"""
import fcntl, os, pty, re, select, signal, termios, tty

# 一次读到的字节拆成一个个键: kitty 键盘协议 (CSI 码点;修饰:事件 u) / 其它 CSI / SS3 / alt+键 / 单字节。
# codex 开的 kitty 标志带"报告松开"和"所有键都发转义", 一次会读到 ctrl 按下、q 按下、q 松开、ctrl 松开好几个序列, 不能整块比较
TOKEN = re.compile(rb"\x1b\[[0-9;:?<=>]*[@-~]|\x1bO.|\x1b.?|[\s\S]", re.S)
KITTY = re.compile(rb"\x1b\[(\d+)(?::[\d:]*)?(?:;(\d+)(?::(\d+))?)?(?:;[\d:]*)?u")
LOCKS = 64 | 128  # caps lock / num lock 位, 不算修饰


def kind(tok):
    """-> prefix (ctrl+q) / close (q x Esc) / ctrlb / release (松开) / mod (单按 ctrl shift 等修饰键) / other"""
    if tok == b"\x11":
        return "prefix"
    if tok == b"\x02":
        return "ctrlb"
    if tok in (b"q", b"x", b"\x1b"):
        return "close"
    m = KITTY.fullmatch(tok)
    if not m:
        return "other"
    cp, mods, ev = int(m[1]), int(m[2] or 1) - 1, int(m[3] or 1)
    if ev == 3:
        return "release"
    if 57441 <= cp <= 57452:  # kitty 给修饰键本身的码点
        return "mod"
    if cp == 113 and mods & ~LOCKS == 4:
        return "prefix"
    if cp == 98 and mods & ~LOCKS == 4:
        return "ctrlb"
    return "close" if cp in (113, 120, 27) and not mods & ~LOCKS else "other"


def sync_size(*_):
    fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))


def main():
    old = termios.tcgetattr(0)
    tty.setraw(0)
    armed = False
    dup_b = os.environ["ATTACH_CMD"].startswith("herdr ")
    try:
        while True:
            try:
                ready, _, _ = select.select([0, master], [], [])
            except InterruptedError:
                continue
            if master in ready:
                try:
                    out = os.read(master, 65536)
                except OSError:  # 子进程退出 → EIO
                    return
                if not out:
                    return
                os.write(1, out)
            if 0 in ready:
                out = b""
                for tok in TOKEN.findall(os.read(0, 4096)):
                    k = kind(tok)
                    if armed:
                        if k in ("release", "mod"):  # 等第二个键时, 松开和单按修饰键都不算
                            continue
                        armed = False
                        if k == "close":
                            return
                        out += tok  # ctrl+q ctrl+q = 发一个原样的 ctrl+q; 别的键照常发
                    elif k == "prefix":
                        armed = True
                    elif k == "ctrlb" and dup_b:
                        out += tok + tok
                    else:
                        out += tok
                if out:
                    os.write(master, out)
    finally:
        termios.tcsetattr(0, termios.TCSAFLUSH, old)


pid, master = pty.fork()
if pid == 0:
    os.execvp("sh", ["sh", "-c", os.environ["ATTACH_CMD"]])
sync_size()
signal.signal(signal.SIGWINCH, sync_size)
try:
    main()
finally:
    os.close(master)  # 关 pty 主端 → 子进程收 SIGHUP, 和关掉终端窗口一样 (attach 客户端断开, agent 照跑)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
