"""把 Wallpaper Engine 的壁纸接进来当素材（2026-11-12）。

不是装插件，而是直接读本机创意工坊目录（431960），挑能用的视频壁纸，
转码成网页能吃的尺寸，放到 web/bg/，然后用 /bg/list.json 给入口页随机用。

规则：
  * 只处理 video 类型（scene.pkg / web 壁纸不碰）
  * 转码：≤720p、10 秒、静音、H.264，尽量小
  * 生成 post.jpg 当首帧
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

WE = Path(r"D:\_steam_backup\steam\steamapps\workshop\content\431960")
OUT = Path(r"PROJECT_ROOT\chat_bridge\web\bg")
FFMPEG = r"C:\Users\Lenovo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe"

# 工坊 id → 输出名（我自己挑的语义名）
PICKS = {
    "3822770156": "whale",
    "3830362729": "rain",
    "3787353838": "night",
}


def transcode(src: Path, dst: Path, seconds: int = 10) -> bool:
    if dst.exists():
        print("  已存在:", dst.name)
        return True
    cmd = [
        FFMPEG, "-y", "-i", str(src), "-t", str(seconds),
        "-vf", "scale='min(1280,iw)':-2,fps=24",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("  转码失败:", (result.stderr or "")[-300:])
        return False
    print("  转码完成:", dst.name, round(dst.stat().st_size / 1024 / 1024, 2), "MB")
    return True


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    entries = []
    for folder_id, name in PICKS.items():
        folder = WE / folder_id
        if not folder.exists():
            print("跳过（没有这个工坊项）:", folder_id)
            continue
        videos = list(folder.glob("*.mp4"))
        if not videos:
            print("跳过（不是视频壁纸）:", folder_id)
            continue
        src = videos[0]
        print(f"[{name}] {src.name} ({round(src.stat().st_size / 1024 / 1024, 1)} MB)")
        dst = OUT / f"{name}.mp4"
        if not transcode(src, dst):
            continue
        poster = OUT / f"{name}.jpg"
        if not poster.exists():
            subprocess.run([FFMPEG, "-y", "-i", str(dst), "-vframes", "1", "-q:v", "4", str(poster)],
                           capture_output=True)
        info = {}
        project = folder / "project.json"
        if project.exists():
            try:
                info = json.loads(project.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                info = {}
        entries.append({
            "name": name,
            "file": f"{name}.mp4",
            "poster": f"{name}.jpg" if poster.exists() else "",
            "title": str(info.get("title") or name),
            "from": "wallpaper-engine",
            "workshop": folder_id,
        })
    (OUT / "list.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n写清单: /bg/list.json")
    for item in entries:
        print("  ", item["name"], "|", item["title"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
