# -*- coding: utf-8 -*-
"""异步 RVC 音色转换：把 Breeze 合成好的语音排进队列，慢慢换成她的音色。

为什么异步：
- Breeze 常驻显存 ~4GB，RVC 再吃 2-4GB，8G 卡顶不住；
- 所以 worker 默认**只用 CPU**（约 13 秒/句），一次只做一个，绝不和 Breeze 抢显存。

协议（web_ui 和 worker 之间）：
- 任务：data/voice/jobs/<id>.json  {"id","input","pitch","model","index","created"}
- 状态：data/voice/jobs/<id>.status.json {"state":"queued|running|done|failed","error":""}
- 产物：data/voice/jobs/<id>.wav

用法：python tools/rvc_worker.py --watch 1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # chat_bridge
JOBS = ROOT / "data" / "voice" / "jobs"
RVC_ROOT = Path(r"PROJECT_ROOT\vendor\rvc")
RVC_PY = Path(r"C:\Users\Lenovo\anaconda3\envs\rvc312\python.exe")
MAX_AGE_HOURS = 6


def write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def status_path(job_id: str) -> Path:
    return JOBS / f"{job_id}.status.json"


def set_state(job_id: str, state: str, error: str = "") -> None:
    write_json(status_path(job_id), {"state": state, "error": error, "at": time.time()})


def cleanup() -> None:
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    for path in JOBS.glob("*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def convert(job: dict) -> tuple[bool, str]:
    job_id = str(job["id"])
    src = Path(str(job["input"]))
    dst = JOBS / f"{job_id}.wav"
    model = Path(str(job.get("model") or ""))
    if not src.exists():
        return False, "输入音频不在了"
    if not model.exists():
        return False, f"模型不存在：{model}"
    cmd = [
        str(RVC_PY), "infer/cli.py",
        "--model", str(model),
        "--input", str(src),
        "--output", str(dst),
        "--pitch", str(int(job.get("pitch") or 0)),
        "--f0-method", "rmvpe",
        "--overwrite",
    ]
    index = Path(str(job.get("index") or ""))
    if index.exists():
        cmd += ["--index", str(index)]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RVC_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    # 只用 CPU：不和常驻的 Breeze 抢显存
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["OMP_NUM_THREADS"] = "4"
    try:
        started = time.time()
        proc = subprocess.run(cmd, cwd=str(RVC_ROOT), env=env, capture_output=True, timeout=240)
        if proc.returncode != 0 or not dst.exists():
            tail = proc.stderr.decode("utf-8", "ignore")[-400:]
            return False, f"RVC 失败：{tail.strip() or proc.returncode}"
        sys.stderr.write(f"[rvc] {job_id} 完成 {time.time()-started:.0f}s\n")
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "RVC 超时（>240s）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def process_once() -> bool:
    if not JOBS.exists():
        return False
    for path in sorted(JOBS.glob("*.json")):
        name = path.name
        if ".status" in name:
            continue
        job_id = path.stem
        if status_path(job_id).exists():
            continue
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            set_state(job_id, "failed", "任务文件读不了")
            continue
        set_state(job_id, "running")
        ok, error = convert(job)
        set_state(job_id, "done" if ok else "failed", error)
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="异步 RVC 音色转换 worker")
    parser.add_argument("--watch", type=float, default=1.0, help="轮询间隔秒数，0 表示只跑一遍")
    args = parser.parse_args()
    JOBS.mkdir(parents=True, exist_ok=True)
    cleanup()
    if args.watch <= 0:
        process_once()
        return 0
    print(f"RVC worker 启动，盯 {JOBS}（CPU 模式）", flush=True)
    while True:
        try:
            if not process_once():
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[rvc] worker 异常：{type(exc).__name__}: {exc}\n")
            time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
