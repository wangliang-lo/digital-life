import os
import re
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZipFile


REPO = Path(__file__).resolve().parents[1]


def _env_path(name, default):
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


HOME = _env_path("MEMBENCH_HOME", REPO / "_exp")
BENCH = _env_path("MEMBENCH_RUN_DIR", HOME / "bench")
CACHE = BENCH / "cache"

DATA_ZIP = HOME / "membench-ref" / "data" / "membench-data.zip"
DATA_DIR = DATA_ZIP.parent / "Membenchdata" / "data"
DATA_PREFIX = "Membenchdata/data/"


def default_data():
    if os.environ.get("MEMBENCH_DATA"):
        return _env_path("MEMBENCH_DATA", DATA_ZIP)
    return DATA_DIR if DATA_DIR.is_dir() else DATA_ZIP


def dataset_member(filename):
    if (
        not filename.endswith(".json")
        or "/" in filename
        or "\\" in filename
        or ":" in filename
        or "\x00" in filename
    ):
        raise ValueError("--file 必须是单个 .json 文件名，不能含路径")
    return DATA_PREFIX + filename


@contextmanager
def open_dataset(source, filename):
    """接受 ZIP、解压根目录、Membenchdata 目录或 data 目录。"""
    member = dataset_member(filename)
    source = Path(source).expanduser().resolve()

    if source.is_dir():
        for candidate in (
            source / member,
            source / "data" / filename,
            source / filename,
        ):
            if candidate.is_file():
                with candidate.open("rb") as stream:
                    yield stream
                return
        raise FileNotFoundError(f"数据目录 {source} 中找不到 {member}")

    if not source.is_file():
        raise FileNotFoundError(
            f"找不到数据：{source}。请用 --data 指定本地 ZIP 或解压目录。"
        )

    with ZipFile(source) as archive:
        try:
            stream = archive.open(member)
        except KeyError:
            raise FileNotFoundError(f"{source} 中没有 {member}") from None
        with stream:
            yield stream


def cache_n(path):
    """从 ingest-…-n{N}-{mode}.json 取出数值 N。"""
    name = Path(path).name
    # 文件名本身可能包含 -n123-；优先匹配数据文件名后的边界。
    match = re.search(r"\.json-n([0-9]+)-", name)
    if match is None:
        match = re.search(r"-n([0-9]+)-", name)
    if match is None:
        raise ValueError(f"无法从缓存文件名解析 n：{name}")
    return int(match.group(1))
