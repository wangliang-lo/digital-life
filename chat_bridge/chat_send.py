# -*- coding: utf-8 -*-
"""让小鱼能给IM 里的联系人主动发一条文字。

两个外部接口（都在本机，只读本地配置里的 token）：
- 读取服务（weflow，127.0.0.1:5031）：`/api/v1/sessions` 拿"名字 → wxid"
- IM hook（127.0.0.1:30001）：`POST /SendTextMsg {wxidorgid, msg}` 真正发出去

注意：`/SendTextMsg` 只接受 wxid（或群 id），不接受昵称——所以必须先查 sessions。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

WORKSPACE = Path(r"C:\Users\Lenovo\Desktop\aihuman\data\workspace")
IO_CONFIG = WORKSPACE / "state" / "chat-io-config.json"
NATIVE_TOKEN = WORKSPACE / "state" / "hook" / "native-token.txt"
HOOK_BASE = "http://127.0.0.1:30001"
IMAGE_ROOT = WORKSPACE / "state" / "hook" / "outgoing-images"
STICKERS_DIR = Path(__file__).resolve().parent / "stickers"


def _io() -> dict[str, Any]:
    try:
        return json.loads(IO_CONFIG.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _reader_token() -> str:
    return str(_io().get("reader_token") or "")


def _account() -> str:
    """当前登录的IM账号（native 接口要求带 X-IMAgent-Account）。"""
    token = _native_token()
    if not token:
        return ""
    try:
        req = urllib.request.Request(
            HOOK_BASE + "/IMAgent/health", headers={"Authorization": "Bearer " + token}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        accounts = data.get("databaseAccounts") or []
        return str(accounts[0]).strip() if accounts else ""
    except Exception:  # noqa: BLE001
        return ""


def _native_token() -> str:
    try:
        return NATIVE_TOKEN.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def list_sessions(limit: int = 500) -> list[dict[str, Any]]:
    token = _reader_token()
    if not token:
        return []
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:5031/api/v1/sessions?limit={int(limit)}",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [s for s in (data.get("sessions") or []) if isinstance(s, dict)]


def _risk_gate(kind: str, to: str, is_group: bool = False) -> str:
    """2026-11-08 账号被风控：限速、禁图、禁群发。返回空字符串表示放行。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
        import chat_brake  # noqa: PLC0415

        return chat_brake.gate(kind, to, is_group)
    except Exception:  # noqa: BLE001
        return ""


def _risk_record() -> None:
    try:
        import chat_brake  # noqa: PLC0415

        chat_brake.record_sent()
    except Exception:  # noqa: BLE001
        pass


def find_contact(name: str) -> dict[str, Any] | None:
    """按显示名精确匹配，再退回包含匹配（忽略大小写）。"""
    target = str(name or "").strip()
    if not target:
        return None
    sessions = list_sessions()
    # 2026-11-08：会话 id（群/私聊的 wxid 数字）也要能直接查——
    # 群聊发消息时拿到的是 `chat:group:123456` 里的 id，不是显示名。
    for item in sessions:
        if str(item.get("username") or "").strip() == target:
            return item
    for item in sessions:
        if str(item.get("displayName") or "").strip() == target:
            return item
    lower = target.lower()
    for item in sessions:
        display = str(item.get("displayName") or "").strip().lower()
        if display and (lower in display or display in lower):
            return item
    return None


def send_text(to: str, text: str, dry_run: bool = False) -> dict[str, Any]:
    """给IM 里的某个人发一条文字。返回 {ok, error, to, wxid}。"""
    contact = find_contact(to)
    if not contact:
        return {"ok": False, "error": f"找不到「{to}」这个联系人", "sessions": len(list_sessions())}
    _blocked = _risk_gate("text", to, is_group="@chatroom" in str(contact.get("username") or ""))
    if _blocked:
        return {"ok": False, "error": _blocked, "blocked": "risk_mode", "to": to}
    wxid = str(contact.get("username") or "").strip()
    display = str(contact.get("displayName") or to)
    if not wxid:
        return {"ok": False, "error": "这个联系人没有 wxid", "to": display}
    message = str(text or "").strip()
    if not message:
        return {"ok": False, "error": "内容是空的", "to": display}
    if dry_run:
        return {"ok": True, "dry_run": True, "to": display, "wxid": wxid, "text": message}
    token = _native_token()
    if not token:
        return {"ok": False, "error": "没有 hook token", "to": display}
    body = json.dumps({"wxidorgid": wxid, "msg": message}, ensure_ascii=False).encode("utf-8")
    account = _account()
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"}
    if account:
        headers["X-IMAgent-Account"] = account
    req = urllib.request.Request(
        HOOK_BASE + "/SendTextMsg",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:200]
        return {"ok": False, "error": f"hook 返回 {exc.code}：{detail}", "to": display}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "to": display}
    _risk_record()
    return {"ok": True, "to": display, "wxid": wxid, "result": payload}


# ---------------------------------------------------------------- 表情包
def _stickers() -> list[dict[str, Any]]:
    try:
        data = json.loads((STICKERS_DIR / "index.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [s for s in (data.get("stickers") or []) if isinstance(s, dict)]


def _mark_used(item: dict) -> None:
    """记一笔"这张用过了"，下次别再挑同一张。"""
    try:
        path = STICKERS_DIR / "index.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for row in raw.get("stickers") or []:
            if str(row.get("id")) == str(item.get("id")):
                row["uses"] = int(row.get("uses") or 0) + 1
                row["last_used"] = time.time()
                break
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _usable_sticker(item: dict) -> bool:
    tags = [str(t) for t in (item.get("tags") or [])]
    note = str(item.get("note") or "")
    if not tags or all(t in ("未分类", "示例", "") for t in tags):
        return False
    # 只滤掉"这不是表情、是别的东西"的条目。注意别再拿"画面"当关键词：
    # 2026-11-07 重打标签后，正经描述里也带"画面里……"，会误杀。
    if any(word in note for word in ("截图", "视频", "分享", "链接")):
        return False
    return True


def _sticker_words(keyword: str) -> list[str]:
    """把「开心」「举牌 别催」「图上写着我不开心」拆成可匹配的词。"""
    text = str(keyword or "").strip()
    if not text:
        return []
    words = [w for w in re.split(r"[\s,，、/｜|]+", text) if w]
    if len(words) > 1:
        words.append(text)
    return [w for w in dict.fromkeys(words) if w]


def _sticker_score(item: dict, words: list[str]) -> int:
    """描述(3 分/词) > 标签(2 分) > 文件名/来源目录(1 分)。"""
    note = str(item.get("note") or "")
    tags = [str(t) for t in (item.get("tags") or [])]
    source = " ".join([str(item.get("file") or ""), str(item.get("origin") or "")])
    score = 0
    for word in words:
        if word in note:
            score += 3
        if any(word == tag or word in tag for tag in tags):
            score += 2
        if word in source:
            score += 1
    return score


def pick_sticker_detail(keyword: str = "") -> tuple[Path | None, dict[str, Any]]:
    """按她给的词，从表情库里挑**最像**的那一张。

    2026-11-07 爸爸："她发表情包要按自己的喜好来，而不是按概率随机发。"
    以前：关键词对不上就把全库当候选，然后在"最近没用的前半段"里随机挑——
    她自己都不知道挑中了哪张。现在：
      * 有词就按描述/标签打分，只取分数最高的那几张；
      * 同分里挑最少用过、最久没发的（固定顺序，不掷骰子）；
      * 一个都对不上就返回 None，宁可不发，也不替她瞎选。
    没给词（对方明确要表情）时才退回到"最久没发的那张"。
    """
    items = _stickers()
    if not items:
        return None, {}
    pool = [item for item in items if _usable_sticker(item)] or items
    words = _sticker_words(keyword)

    if words:
        scored = [(item, _sticker_score(item, words)) for item in pool]
        best = max((score for _, score in scored), default=0)
        if best <= 0:
            return None, {}
        pool = [item for item, score in scored if score == best]

    ordered = sorted(
        pool,
        key=lambda i: (
            float(i.get("last_used") or 0),
            int(i.get("uses") or 0),
            str(i.get("id") or ""),
        ),
    )
    chosen = ordered[0]
    _mark_used(chosen)
    path = STICKERS_DIR / str(chosen.get("file") or "")
    if not path.exists():
        return None, {}
    return path, chosen


def pick_sticker(keyword: str = "") -> Path | None:
    """兼容旧调用：只要路径。"""
    path, _ = pick_sticker_detail(keyword)
    return path


def send_image(to: str, image_path: str | Path) -> dict[str, Any]:
    """给IM 里的某个人发一张图/表情（hook 要求文件先放进 outgoing-images）。"""
    contact = find_contact(to)
    if not contact:
        return {"ok": False, "error": f"找不到「{to}」这个联系人"}
    _blocked = _risk_gate("image", to, is_group="@chatroom" in str(contact.get("username") or ""))
    if _blocked:
        return {"ok": False, "error": _blocked, "blocked": "risk_mode", "to": to}
    wxid = str(contact.get("username") or "").strip()
    display = str(contact.get("displayName") or to)
    src = Path(image_path)
    if not src.exists():
        return {"ok": False, "error": "图片不在了", "to": display}
    if src.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return {"ok": False, "error": "只支持 png/jpg/webp", "to": display}
    if src.stat().st_size > 32 * 1024 * 1024:
        return {"ok": False, "error": "图片超过 32MB", "to": display}
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    import shutil  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    target = IMAGE_ROOT / f"{uuid.uuid4().hex[:12]}{src.suffix.lower()}"
    try:
        shutil.copyfile(src, target)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"复制图片失败：{exc}", "to": display}
    token = _native_token()
    account = _account()
    if not token:
        return {"ok": False, "error": "没有 hook token", "to": display}
    body = json.dumps({"wxidorgid": wxid, "path": str(target)}, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"}
    if account:
        headers["X-IMAgent-Account"] = account
    req = urllib.request.Request(HOOK_BASE + "/SendImgMsg", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:200]
        return {"ok": False, "error": f"hook 返回 {exc.code}：{detail}", "to": display}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "to": display}
    _risk_record()
    return {"ok": True, "to": display, "wxid": wxid, "result": payload}
