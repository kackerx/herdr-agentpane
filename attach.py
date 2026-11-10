#!/usr/bin/env python3
"""浮窗里跑 $ATTACH_CMD 的 pty 中转, 只为加一个统一的关窗键。
herdr 的 popup 吃掉所有按键 (herdr 前缀在里面不生效), 只在进程退出时关; claude attach 和
herdr agent attach 各有各的退出键 (← / ctrl+b q), 所以在这里统一: ctrl+q 再按 q / x / Esc 关浮窗
(和 herdr 的 prefix+q = 离开一致), ctrl+q ctrl+q 发一个原样的 ctrl+q。agent 本身不受影响, 只是断开这个查看端。"""
import fcntl, os, pty, select, signal, termios, tty

PREFIX = (b"\x11", b"\x1b[113;5u")  # ctrl+q: 普通编码 / kitty 键盘协议 (claude 会开 disambiguate)
CLOSE = (b"q", b"x", b"\x1b", b"\x1b[113u", b"\x1b[120u", b"\x1b[27u")


def sync_size(*_):
    fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))


def main():
    old = termios.tcgetattr(0)
    tty.setraw(0)
    armed = False
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
                data = os.read(0, 4096)
                if armed:
                    armed = False
                    if data in CLOSE:
                        return
                    if data in PREFIX:
                        os.write(master, b"\x11")
                        continue
                elif data in PREFIX:  # ponytail: 只认"整块就是这个键"; 粘贴里夹着 ctrl+q 会原样透传
                    armed = True
                    continue
                os.write(master, data)
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
