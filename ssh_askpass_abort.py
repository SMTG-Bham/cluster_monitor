"""Windows 版 ssh_askpass_abort.sh（由 ssh_askpass_abort.cmd 调用，sshutil.py 负责设置）。

服务器一旦要求输入（2FA 验证码 / 密码 / 确认 host key），不回答任何东西，直接把 ssh.exe
杀掉，并在 stderr 里写 @@NEED_INPUT <提示语> 告诉 app.py。Windows 的 OpenSSH 在 askpass
失败（退出码非 0）时会发一个空回答再重试（实测连发 3 次空密码），所以必须在 askpass
退出之前把 ssh 杀掉。

调用链是 ssh.exe -> cmd.exe (ssh_askpass_abort.cmd) -> python.exe（本脚本），
沿着父进程往上找第一个 ssh.exe 结束它。万一杀不掉：关掉自己的 stdin/stdout/stderr，
一直等到 ssh 自己退出（被 app.py 的超时杀掉）才退出——ssh 永远拿不到回答。

    python ssh_askpass_abort.py --selftest   # sshutil.py 用它确认这台机器上能用
只用标准库（ctypes）。
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_WAIT_SECONDS = 3600
# .cmd 只有看到这个退出码才马上退出；python 起不来 / 崩了它就一直等着。
# 别用小数字：cmd.exe 自己找不到路径 / 文件时的 errorlevel 是 3 / 2 / 9009，python 崩了是 1
DONE = 77


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def process_table(k32):
    """{pid: (ppid, exe 名小写)}"""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return {}
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table


def ancestors(table, pid, depth=8):
    """[父进程, 祖父进程, ...]（pid 会被复用，所以只走 depth 层，碰到环就停）"""
    out = []
    for _ in range(depth):
        ppid = table.get(pid, (0, ""))[0]
        if not ppid or ppid not in table or ppid in out or ppid == pid:
            break
        out.append(ppid)
        pid = ppid
    return out


def terminate(k32, pid):
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, 1))
    finally:
        k32.CloseHandle(h)


def say(text):
    """写到 stderr（继承自 ssh，也就是 app.py 读的那根管道）"""
    try:
        os.write(2, (text + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def never_answer(k32, watch_pid):
    """杀不掉 ssh 时的兜底：不退出（askpass 一退出 ssh 就会发空回答），
    把管道还给 app.py，等 ssh 自己没了再走"""
    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        if watch_pid and watch_pid not in process_table(k32):
            return
        time.sleep(1)


def main():
    k32 = kernel32()
    say("@@NEED_INPUT " + " ".join(sys.argv[1:]))
    try:
        table = process_table(k32)
        chain = ancestors(table, os.getpid())
        ssh_pid = next((p for p in chain if table[p][1] == "ssh.exe"), None)
    except Exception:  # noqa: BLE001
        chain, ssh_pid = [], None
    if not (ssh_pid and terminate(k32, ssh_pid)):
        # 找不到 / 杀不掉：盯着 ssh（找不到就盯 cmd.exe 的父进程）
        never_answer(k32, ssh_pid or (chain[1] if len(chain) > 1 else None))
    return DONE


def selftest():
    """能列进程、能找到自己的父进程、能以 TERMINATE 权限打开它（不会真的杀）"""
    k32 = kernel32()
    table = process_table(k32)
    chain = ancestors(table, os.getpid())
    if os.getpid() not in table or not chain:
        print("@@SELFTEST_FAIL no parent")
        return 1
    h = k32.OpenProcess(PROCESS_TERMINATE, False, chain[0])
    if not h:
        print("@@SELFTEST_FAIL OpenProcess", ctypes.get_last_error())
        return 1
    k32.CloseHandle(h)
    print("@@SELFTEST_OK", " <- ".join(table[p][1] for p in chain[:3]))
    return 0


if __name__ == "__main__":
    if os.name != "nt":
        sys.exit("ssh_askpass_abort.py is for Windows; use ssh_askpass_abort.sh elsewhere")
    sys.exit(selftest() if sys.argv[1:] == ["--selftest"] else main())
