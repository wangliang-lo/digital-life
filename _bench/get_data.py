import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .paths import DATA_DIR, DATA_PREFIX, DATA_ZIP, dataset_member


def extract(source, dest):
    with ZipFile(source) as archive:
        members = {}
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith(DATA_PREFIX):
                continue
            name = info.filename[len(DATA_PREFIX):]
            if not name.endswith(".json"):
                continue
            if dataset_member(name) != info.filename:
                raise ValueError(f"无效的 ZIP 成员：{info.filename}")
            key = name.casefold()
            if key in members:
                raise ValueError(f"ZIP 中有重复文件名：{info.filename}")
            members[key] = (name, info)

        if not members:
            raise ValueError(f"ZIP 中没有 {DATA_PREFIX}*.json")

        dest.mkdir(parents=True, exist_ok=True)
        for name, info in members.values():
            temp_path = None
            try:
                with archive.open(info) as inp:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=dest, suffix=".part", delete=False
                    ) as out:
                        temp_path = Path(out.name)
                        shutil.copyfileobj(inp, out)
                os.replace(temp_path, dest / name)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

    return len(members)


def main():
    parser = argparse.ArgumentParser(description="检查并解压本地 MemBench 数据包")
    parser.add_argument("--zip", type=Path, default=DATA_ZIP)
    parser.add_argument("--dest", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    source = args.zip.expanduser().resolve()
    dest = args.dest.expanduser().resolve()

    if not source.is_file():
        print(
            f"找不到数据包：{source}\n"
            "仓库不附带数据包，也没有已验证的公开下载地址。\n"
            "请向数据提供方取得有权使用的 membench-data.zip，然后运行：\n"
            '  python -m _bench.get_data --zip "D:/data/membench-data.zip"\n'
            "也可把 ZIP 直接传给 run_all --data，无须解压。",
            file=sys.stderr,
        )
        return 2

    try:
        count = extract(source, dest)
    except (OSError, ValueError, RuntimeError, BadZipFile) as exc:
        print(f"解压失败：{exc}", file=sys.stderr)
        return 2

    print(f"已解压 {count} 个数据文件：{dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
