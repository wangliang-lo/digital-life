"""生活循环的看护进程（2026-11-11）。

PS 版 supervisor 在这台机器上没跑起来（起了 powershell 但没有子进程、日志也没写），
改成 Python：直接 Popen 生活循环，退出就 5 秒后重来。
由 start_all.ps1 用 python 隐藏启动，和别的后台服务一样。
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOCK = DATA / "life_supervisor.lock"
LOG = ROOT / "logs" / "life_loop.out.log"
ERR = ROOT / "logs" / "life_loop.err.log"
PYTHON = sys.executable or r"C:\Users\Lenovo\anaconda3\python.exe"



def _already_running() -> bool:
    """已经有 supervisor 在跑就不再起一份（计划任务每 5 分钟会调一次）。

    不靠锁文件——2026-11-12 实测锁写成了 0 字节，一下子起了 3 个。
    直接数进程最靠谱。
    """
    import os

    try:
        import psutil  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline", "name"]):
        if proc.info["pid"] == me:
            continue
        name = str(proc.info.get("name") or "").lower()
        if name not in ("python.exe", "pythonw.exe"):
            continue          # powershell / cmd 里带这串字不算
        cmd = " ".join(proc.info.get("cmdline") or [])
        if "life_supervisor.py" in cmd:
            return True
    return False


def _kill_stray_loops() -> list[int]:
    """把自己之外的 life_loop 清掉，避免一份大脑多个循环打架。"""
    import os

    killed = []
    try:
        import psutil  # noqa: PLC0415

        me = os.getpid()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            if proc.info["pid"] == me:
                continue
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "life_loop.py" in cmd and "life_supervisor" not in cmd:
                try:
                    proc.kill()
                    killed.append(proc.info["pid"])
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return killed

def note(line: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%H:%M:%S}] {line}\n")
    except OSError:
        pass


def main() -> int:
    if _already_running():
        return 0
    killed = _kill_stray_loops()
    if killed:
        note(f"清掉遗留 life_loop：{killed}")
    note("life_supervisor 启动")
    while True:
        with LOG.open("a", encoding="utf-8") as out, ERR.open("a", encoding="utf-8") as err:
            note("启动 life_loop")
            try:
                proc = subprocess.Popen(
                    [PYTHON, "-u", str(ROOT / "tools" / "life_loop.py"), "--watch", "20"],
                    cwd=str(ROOT), stdout=out, stderr=err,
                )
                code = proc.wait()
            except Exception as exc:  # noqa: BLE001
                code = -1
                err.write(f"{type(exc).__name__}: {exc}\n")
                err.flush()
        note(f"life_loop 退出 code={code}，5 秒后重启")
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
