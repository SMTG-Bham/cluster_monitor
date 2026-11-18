"""
跑 ssh 的公共部分（app.py 和 install.py 共用，只用标准库）
==========================================================
不用 BatchMode=yes。它会直接拒绝 keyboard-interactive 认证，而 sulis 这种
"key + 2FA" 的集群，即使 2FA 还在有效期（不用输验证码），也要走一轮 keyboard-interactive。
这里允许它，但一旦服务器真的要输入东西，askpass 会直接杀掉 ssh
（不发任何回答，免得发空验证码被锁账号），并在 stderr 里留下 @@NEED_INPUT。

Linux / macOS：askpass 是 ssh_askpass_abort.sh（kill $PPID）。start_new_session 让 ssh
没有控制终端，不会在你跑 app.py 的终端里弹提示。

Windows：askpass 是 ssh_askpass_abort.cmd -> ssh_askpass_abort.py，沿父进程找到 ssh.exe 并
TerminateProcess。Windows 的 OpenSSH 在 askpass 起不来 / 失败时会发空回答再重试（实测连发 3 次），
所以只有下面几条都满足才用这种方式，否则退回 BatchMode=yes（只用 key / ssh-agent，要
keyboard-interactive 的集群直接失败，页面上显示"需要 2FA"）：
  - ssh 是 Win32-OpenSSH（`ssh -V` 里有 OpenSSH_for_Windows；Git for Windows / MSYS 的 ssh
    不一定能启动 .cmd）
  - askpass 的 .cmd / .py 都在，用来跑 .py 的 python.exe 存在
  - ssh_askpass_abort.py --selftest 能跑通（能列出进程、能找到自己的父进程）
结果缓存在进程里；CM_SSH_BATCHMODE=1 强制用 BatchMode=yes。
"""

import os
import re
import subprocess
import sys
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WINDOWS = os.name == "nt"
ASKPASS = os.path.join(APP_DIR, "ssh_askpass_abort.cmd" if WINDOWS else "ssh_askpass_abort.sh")
ASKPASS_PY = os.path.join(APP_DIR, "ssh_askpass_abort.py")

_win_mode = None  # Windows："askpass" / "batch"，第一次用的时候判断
_win_lock = threading.Lock()


def askpass_python():
    """跑 ssh_askpass_abort.py 的 python：当前的解释器，pythonw.exe 换成旁边的 python.exe
    （pythonw 拿不到 stderr，写不出 @@NEED_INPUT）"""
    exe = sys.executable or ""
    if os.path.basename(exe).lower() == "pythonw.exe":
        py = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(py):
            return py
    return exe


def _windows_mode():
    """判断 Windows 上能不能安全地用 askpass（见模块说明），不能就用 BatchMode=yes"""
    global _win_mode
    with _win_lock:
        if _win_mode is None:
            _win_mode = _probe_windows_askpass()
        return _win_mode


def _probe_windows_askpass():
    if os.environ.get("CM_SSH_BATCHMODE") == "1":
        return "batch"
    py = askpass_python()
    if not (py and os.path.isfile(py) and os.path.isfile(ASKPASS) and os.path.isfile(ASKPASS_PY)):
        return "batch"
    no_window = {"creationflags": subprocess.CREATE_NO_WINDOW}
    try:
        v = subprocess.run(["ssh", "-V"], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, errors="replace", timeout=10, **no_window)
        if "OpenSSH_for_Windows" not in (v.stderr + v.stdout):
            return "batch"
        t = subprocess.run([py, "-I", ASKPASS_PY, "--selftest"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace", timeout=20, **no_window)
        if t.returncode != 0 or "@@SELFTEST_OK" not in t.stdout:
            return "batch"
    except (OSError, subprocess.SubprocessError):
        return "batch"
    return "askpass"


def mode():
    """'askpass'（允许 keyboard-interactive，一要输入就杀 ssh）或 'batch'（BatchMode=yes）"""
    return _windows_mode() if WINDOWS else "askpass"


def run(host, remote_cmd, timeout, connect_timeout=10):
    """ssh host remote_cmd，返回 CompletedProcess；超时抛 TimeoutExpired，没有 ssh 抛 OSError"""
    if mode() == "batch":
        auth = ["-o", "BatchMode=yes"]
        env = None
    else:
        auth = ["-o", "BatchMode=no", "-o", "KbdInteractiveAuthentication=yes"]
        env = dict(os.environ, SSH_ASKPASS=ASKPASS, SSH_ASKPASS_REQUIRE="force")
        env.setdefault("DISPLAY", "none")  # 老版本 OpenSSH 只有设了 DISPLAY 才会用 SSH_ASKPASS
        if WINDOWS:
            env["CM_ASKPASS_PYTHON"] = askpass_python()  # .cmd 用它跑 .py
            # .cmd 兜底时（python 起不来）一直等到 ssh 被下面的超时杀掉，不让 ssh 拿到空回答
            env["CM_ASKPASS_HOLD"] = str(int(timeout) + 5)
    cmd = ["ssh", *auth, "-o", f"ConnectTimeout={connect_timeout}", host, remote_cmd]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
              encoding="utf-8", errors="replace",  # Windows 默认是本地代码页，集群输出是 UTF-8
              env=env)
    if not WINDOWS:
        return subprocess.run(cmd, timeout=timeout, start_new_session=True, **kw)
    # Windows：超时的时候连 ssh 的子进程（askpass、ProxyJump 的 ssh）一起杀掉。
    # 只杀 ssh.exe 的话它们还拿着 stdout/stderr 管道，communicate() 会一直等下去
    with subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW, **kw) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            p.kill()
            out, err = p.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def as_text(data):
    """TimeoutExpired.stderr 可能是 None / bytes / str"""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def need_input(stderr):
    """服务器要求交互输入（2FA 验证码 / 密码 / 确认 host key）时返回提示语，否则 None"""
    m = re.search(r"@@NEED_INPUT (.*)", stderr)
    if m:
        return m.group(1).strip() or "(prompt)"
    # BatchMode=yes（Windows 退回的方式）：服务器只剩 keyboard-interactive 可走
    if WINDOWS and re.search(r"Permission denied \(keyboard-interactive\)", stderr):
        return "keyboard-interactive (2FA / password)"
    return None
