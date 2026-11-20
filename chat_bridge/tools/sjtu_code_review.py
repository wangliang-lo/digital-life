"""用交大免费额度把主要代码过一遍（后台跑，不占她的聊天）。

用法：
    python tools/sjtu_code_review.py            # 全部
    python tools/sjtu_code_review.py --max 20   # 只跑前 20 段（试水）

每段 260 行，问"这段有什么 bug / 可以怎么改"，结果写到 _review/sjtu/<文件>_<起>-<止>.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import sjtu_llm  # noqa: E402

ROOT = Path(r"PROJECT_ROOT")
OUT = ROOT / "_review" / "sjtu"
CHUNK = 200
TARGETS = [
    ROOT / "chat_bridge" / "dl_api.py",
    ROOT / "digital_life.py",
    ROOT / "memory.py",
    ROOT / "chat_bridge" / "web_ui.py",
    ROOT / "chat_bridge" / "HORMONE.py",
    ROOT / "chat_bridge" / "doodle.py",
    ROOT / "chat_bridge" / "tools" / "tictactoe.py",
    ROOT / "chat_bridge" / "tools" / "gomoku.py",
    ROOT / "chat_bridge" / "tools" / "backup_memory.py",
]

PROMPT = """你是资深 Python 工程师。下面是一个长期运行的个人 AI 项目里的代码片段（{name} 第 {start}-{end} 行）。

请从"能上线、别把服务搞挂"的角度审这一段，输出：
1. 真实 bug（能指到行的、有触发条件的）
2. 明显的性能/资源问题（泄漏、重复 IO、慢查询）
3. 最多 3 条"值得改"的建议

格式：每条一行，`行号 + 问题 + 一句话修法`。没有就写"这段没发现问题"。
别复述代码，别写总结段落。只写你**能在这段代码里指认**的问题；不确定的不要写。最多 6 条。

```python
{code}
```
"""


def chunks_for(path: Path) -> list[tuple[int, int]]:
    try:
        total = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return []
    return [(start, min(start + CHUNK - 1, total)) for start in range(1, total + 1, CHUNK)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    if not sjtu_llm.available():
        print("没有 SJTU key")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    done = 0
    for path in TARGETS:
        if args.only and args.only not in path.name:
            continue
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for start, end in chunks_for(path):
            if args.max and done >= args.max:
                return 0
            target = OUT / f"{path.stem}_{start}-{end}.md"
            if target.exists():
                continue
            code = "\n".join(lines[start - 1 : end])
            prompt = PROMPT.format(name=path.name, start=start, end=end, code=code[:7000])
            try:
                answer = sjtu_llm.chat(prompt, max_tokens=600, temperature=0.3, timeout=120)
            except Exception as exc:  # noqa: BLE001
                print(f"{path.name} {start}-{end} 失败: {exc}", flush=True)
                continue
            target.write_text(f"# {path.name} {start}-{end}\n\n{answer}\n", encoding="utf-8")
            done += 1
            print(f"完成 {path.name} {start}-{end}（{done}）", flush=True)
            time.sleep(1.0)
    print("全部完成:", done)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
