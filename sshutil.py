"""
跑 ssh 的公共部分（app.py 和 install.py 共用，只用标准库）
==========================================================
Linux / macOS：不用 BatchMode=yes。它会直接拒绝 keyboard-interactive 认证，而 sulis 这种
"key + 2FA" 的集群，即使 2FA 还在有效期（不用输验证码），也要走一轮 keyboard-interactive。
这里允许它，但一旦服务器真的要输入东西，ssh_askpass_abort.sh 会直接杀掉 ssh
（不发任何回答，免得发空验证码被锁账号），并在 stderr 里留下 @@NEED_INPUT。
start_new_session 让 ssh 没有控制终端，不会在你跑 app.py 的终端里弹提示。

Windows：自带的 OpenSSH 跑不了 .sh 的 askpass，askpass 起不来时 ssh 会发空回答，
所以用 BatchMode=yes：只用 key / ssh-agent，要 keyboard-interactive（2FA / 密码）的集群
直接失败，页面上显示"需要 2FA"。这种集群在 Windows 上请用 WSL 跑 dashboard。
"""

import os
import re
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WINDOWS = os.name == "nt"
ASKPASS = os.path.join(APP_DIR, "ssh_askpass_abort.sh")


def run(host, remote_cmd, timeout, connect_timeout=10):
    """ssh host remote_cmd，返回 CompletedProcess；超时抛 TimeoutExpired，没有 ssh 抛 OSError"""
    if WINDOWS:
        auth = ["-o", "BatchMode=yes"]
        env = None
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW}  # pythonw 跑的时候不弹黑窗口
    else:
        auth = ["-o", "BatchMode=no", "-o", "KbdInteractiveAuthentication=yes"]
        env = dict(os.environ, SSH_ASKPASS=ASKPASS, SSH_ASKPASS_REQUIRE="force")
        env.setdefault("DISPLAY", "none")  # 老版本 OpenSSH 只有设了 DISPLAY 才会用 SSH_ASKPASS
        extra = {"start_new_session": True}
    return subprocess.run(
        ["ssh", *auth, "-o", f"ConnectTimeout={connect_timeout}", host, remote_cmd],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace",  # Windows 默认是本地代码页，集群输出是 UTF-8
        timeout=timeout, env=env, **extra)


def need_input(stderr):
    """服务器要求交互输入（2FA 验证码 / 密码 / 确认 host key）时返回提示语，否则 None"""
    m = re.search(r"@@NEED_INPUT (.*)", stderr)
    if m:
        return m.group(1).strip() or "(prompt)"
    # Windows 的 BatchMode=yes：服务器只剩 keyboard-interactive 可走
    if WINDOWS and re.search(r"Permission denied \(keyboard-interactive\)", stderr):
        return "keyboard-interactive (2FA / password)"
    return None
