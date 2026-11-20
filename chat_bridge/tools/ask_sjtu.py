"""给 lead/Claude 之外的"杂活"用的免费问答入口（交大额度）。

用法：
    python tools/ask_sjtu.py "问题"
    python tools/ask_sjtu.py "这个函数有什么 bug" --file path.py --lines 100-180
    python tools/ask_sjtu.py "写个正则匹配日期" --model qwen

定位：便宜、够用；重要的判断和改动还是 Claude 来做。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sjtu_llm  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="交大免费模型问答")
    parser.add_argument("question")
    parser.add_argument("--file", default="")
    parser.add_argument("--lines", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-tokens", type=int, default=1200)
    args = parser.parse_args()

    prompt = args.question
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print("文件不存在：", path)
            return 2
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = 1, len(lines)
        if args.lines and "-" in args.lines:
            a, b = args.lines.split("-", 1)
            start, end = int(a), int(b)
        snippet = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))
        prompt = f"下面是 {path.name} 第 {start}-{end} 行：\n```\n{snippet[:12000]}\n```\n\n{args.question}"

    if not sjtu_llm.available():
        print("没有 SJTU key")
        return 1
    print(sjtu_llm.chat(prompt, model=args.model, max_tokens=args.max_tokens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
