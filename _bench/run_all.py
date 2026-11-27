import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import BadZipFile

from .paths import BENCH, REPO, default_data, open_dataset


MODELS = ("deepseek-chat", "gpt-6.2-sol", "claude-opus-5-5")


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def stable_id(value):
    if type(value) not in (str, int) or value == "":
        raise ValueError("gid / memory_id 必须是非空字符串或整数 ID")
    return str(value)


def load_checkpoint(path):
    with path.open(encoding="utf-8") as stream:
        payload = json.load(stream)

    if isinstance(payload, dict):
        # 支持结果数组、带 results/rows 的对象和 gid -> 结果对象。
        payload = payload.get("results", payload.get("rows", payload))
        if isinstance(payload, dict):
            payload = list(payload.values())

    if not isinstance(payload, list):
        raise ValueError(f"{path}：无法识别 checkpoint 的结果结构")

    scored = {}
    seen = set()
    for row in payload:
        if not isinstance(row, dict) or "gid" not in row:
            raise ValueError(f"{path}：结果行缺少 gid")
        gid = stable_id(row["gid"])
        if gid in seen:
            raise ValueError(f"{path}：gid 重复：{gid}")
        seen.add(gid)

        correct = row.get("correct")
        if type(correct) is bool or (type(correct) is int and correct in (0, 1)):
            scored[gid] = row

    if not scored:
        raise ValueError(f"{path}：没有 correct 为布尔值或 0/1 的结果")
    return scored, len(seen)


def load_gold(path):
    gold = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if not {"gid", "memory_id"} <= set(reader.fieldnames or ()):
            raise ValueError("--gold TSV 必须含 gid 和 memory_id 两列")
        for row in reader:
            gid = stable_id(row["gid"])
            gold.setdefault(gid, set()).add(stable_id(row["memory_id"]))
    if not gold:
        raise ValueError("--gold 中没有标注")
    return gold


def retrieval_recall(scored, gold):
    if gold is None:
        return "N/A"

    hits = total = matched = 0
    for gid, row in scored.items():
        if gid not in gold:
            continue
        used = row.get("used_memories")
        if not isinstance(used, list):
            raise ValueError(f"gid={gid}：used_memories 不是记忆 ID 列表")
        try:
            retrieved = {stable_id(value) for value in used}
        except ValueError:
            raise ValueError(
                f"gid={gid}：used_memories 必须是与 --gold 同口径的 ID 列表，"
                "不能用记忆正文或对象替代 ID"
            ) from None
        matched += 1
        hits += len(retrieved & gold[gid])
        total += len(gold[gid])

    if not total:
        return "N/A（无标注题重叠）"
    return f"{hits / total:.2%} ({hits}/{total}；{matched} 题)"


def paired_result(scored, baseline):
    if baseline is None:
        return "N/A"

    common = scored.keys() & baseline.keys()
    if not common:
        return "N/A（无共同题）"

    wins = losses = 0
    for gid in common:
        now, old = scored[gid], baseline[gid]
        for field in ("truth", "category"):
            if field not in now or field not in old or now[field] != old[field]:
                raise ValueError(f"gid={gid}：{field} 缺失或不一致，不能配对")
        a, b = bool(now["correct"]), bool(old["correct"])
        wins += int(a and not b)
        losses += int(b and not a)

    delta = 100 * (wins - losses) / len(common)
    ties = len(common) - wins - losses
    return f"{len(common)} 题；胜/负/平 {wins}/{losses}/{ties}；{delta:+.2f} pp"


def run_dir(args, source):
    # 对目标 JSON 的内容做哈希；同一 JSON 从 ZIP 或目录读取时可复用。
    digest = hashlib.sha256()
    with open_dataset(source, args.file) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    config = (
        "run-all-v1", args.file, args.n, args.model, "no-compress",
        args.embed, args.topic, args.topk, args.raw_content,
    )
    digest.update(repr(config).encode("utf-8"))
    return BENCH / "runs" / digest.hexdigest()[:24]


def main():
    parser = argparse.ArgumentParser(description="运行 MemBench ingest + eval")
    parser.add_argument("--data", type=Path, help="本地 ZIP 或解压目录")
    parser.add_argument("--n", type=positive_int, default=300)
    parser.add_argument("--file", default="ThirdAgentDataLowLevel.json")
    parser.add_argument("--model", choices=MODELS, default="deepseek-chat")
    parser.add_argument("--embed", action="store_true")
    parser.add_argument("--topic", action="store_true")
    parser.add_argument("--topk", type=positive_int, default=25)
    parser.add_argument("--raw-content", action="store_true")
    parser.add_argument("--gold", type=Path, help="gid / memory_id 标注 TSV")
    parser.add_argument("--compare", type=Path, help="基线 ckpt-eval 文件")
    args = parser.parse_args()

    if args.model != "deepseek-chat":
        parser.error(
            f"{args.model} 尚未接通：当前补丁没有修改 pipeline 的模型路由。"
            "未发送任何模型请求。"
        )
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        parser.error("缺少 DEEPSEEK_API_KEY；请设置环境变量后重试。")

    pipeline = REPO / "_bench" / "run_membench_pipeline.py"
    if not pipeline.is_file():
        parser.error(f"缺少 {pipeline}；请先把原版 pipeline 加入仓库并应用补丁。")

    try:
        source = (args.data or default_data()).expanduser().resolve()
        gold = load_gold(args.gold) if args.gold else None
        baseline = load_checkpoint(args.compare)[0] if args.compare else None
        target = run_dir(args, source)
        target.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["MEMBENCH_DATA"] = str(source)
        env["MEMBENCH_RUN_DIR"] = str(target)
        command = [
            sys.executable, "-m", "_bench.run_membench_pipeline",
            "--stage", "all", "--file", args.file, "--n", str(args.n),
            "--topk", str(args.topk), "--no-compress",
        ]
        for enabled, flag in (
            (args.embed, "--embed"),
            (args.topic, "--topic"),
            (args.raw_content, "--raw-content"),
        ):
            if enabled:
                command.append(flag)

        result = subprocess.run(command, cwd=REPO, env=env, check=False)
        if result.returncode:
            print(
                f"pipeline 退出码：{result.returncode}；运行目录：{target}。"
                "请检查上方日志；修复后可用相同命令续跑。",
                file=sys.stderr,
            )
            return 1

        checkpoints = sorted(target.glob("ckpt-eval-*.json"))
        if len(checkpoints) != 1:
            raise ValueError(
                f"{target} 中应有一个 ckpt-eval-*.json，实际有 "
                f"{len(checkpoints)} 个。请检查 pipeline 的 checkpoint 路径"
                "和目录内是否有旧文件。"
            )

        scored, recorded = load_checkpoint(checkpoints[0])
        correct = sum(bool(row["correct"]) for row in scored.values())
        accuracy = f"{correct / len(scored):.2%} ({correct}/{len(scored)})"
        recall = retrieval_recall(scored, gold)
        paired = paired_result(scored, baseline)
    except (OSError, ValueError, RuntimeError, BadZipFile) as exc:
        print(f"MemBench：{exc}", file=sys.stderr)
        return 2

    print("模型\t已计分/已记录\t准确率\t检索召回（微平均）\t配对比较")
    print(f"{args.model}\t{len(scored)}/{recorded}\t{accuracy}\t{recall}\t{paired}")
    print(f"Checkpoint: {checkpoints[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
