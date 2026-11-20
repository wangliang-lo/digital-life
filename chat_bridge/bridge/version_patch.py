# -*- coding: utf-8 -*-
"""IM 3.6.0.18 的「版本过低」绕过。

现象
----
刚登录 3.6.0.18 就弹「当前IM版本过低，请升级至最新版本」——这是**服务端**在拒绝
旧版本，不是 ntchat 的问题。客户端会把版本号（0x63060012 = 3.6.0.18）发给服务器，
服务器一比就拒绝。

做法
----
IM把版本号放在 IMWin.dll 的内存里（6 个位置）。把它们改成 3.9.12.15
（0x63090C0F），服务器就认为你是 3.9，放你登录；进程里跑的仍然是 3.6.0.18 的代码，
所以 ntchat 的 hook 偏移量依然有效。

⚠️ 风险
----
* 这是绕过腾讯的版本策略，违反用户协议，**有封号风险**，后果自负；
* 只改内存，不改文件；IM重启后要重新打一次；
* 如果服务器已经要求 4.x 协议，伪装成 3.9 也会被拒——那就只能换方案了。

用法
----
    python run_bridge.py patch-version            # IM已打开、还没登录时执行
    python run_bridge.py patch-version --target 3.9.12.15

或者在配置里打开自动伪装，让中间件启动时自己打补丁：

    "chat": {"version_spoof_enabled": true, "version_spoof_target": "3.9.12.15"}
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .logs import get_logger

log = get_logger("version")

# 3.6.0.18 的 6 个版本号存放位置（相对 IMWin.dll 基址）
# 来源：ntchat-wx/change_version.py
OFFSETS_3_6_0_18: tuple[int, ...] = (
    0x22300E0,
    0x223D90C,
    0x223D9E8,
    0x2253E4C,
    0x2255AA4,
    0x22585D4,
)

SOURCE_VERSION = "3.6.0.18"
DEFAULT_TARGET = "3.9.12.15"
MODULE_NAME = "IMWin.dll"


def version_to_hex(version: str) -> int:
    """3.6.0.18 -> 0x63060012；3.9.12.15 -> 0x63090C0F。"""
    parts = [int(part) for part in str(version).strip().split(".")]
    if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
        raise ValueError(f"版本号格式应为 a.b.c.d（0-255）：{version!r}")
    major, minor, patch, build = parts
    return (0x6 << 28) | (major << 24) | (minor << 16) | (patch << 8) | build


@dataclass(slots=True)
class PatchResult:
    ok: bool
    detail: str
    patched: int = 0
    source_hex: str = ""
    target_hex: str = ""
    pid: int = 0


class VersionPatcher:
    def __init__(self, source: str = SOURCE_VERSION, target: str = DEFAULT_TARGET) -> None:
        self.source = source
        self.target = target
        self.source_hex = version_to_hex(source)
        self.target_hex = version_to_hex(target)

    def patch(self, timeout: float = 15.0, interval: float = 0.5) -> PatchResult:
        try:
            import pymem  # noqa: PLC0415
        except ImportError:
            return PatchResult(
                ok=False,
                detail="没有安装 pymem。执行：pip install pymem",
                source_hex=f"0x{self.source_hex:08X}",
                target_hex=f"0x{self.target_hex:08X}",
            )

        deadline = time.time() + max(1.0, timeout)
        last_error = "未知错误"
        patched = 0

        while time.time() < deadline:
            try:
                pm = pymem.Pymem("IM.exe")
            except Exception as exc:  # noqa: BLE001 - 进程还没起来
                last_error = f"找不到 IM.exe（{exc}）"
                time.sleep(interval)
                continue

            try:
                base = 0
                for module in pm.list_modules():
                    if str(module.filename).lower().endswith(MODULE_NAME.lower()):
                        base = module.lpBaseOfDll
                        break
                if not base:
                    last_error = "进程里没有 IMWin.dll，确认是 PC IM 3.6.0.18"
                    time.sleep(interval)
                    continue

                found_source = 0
                already = 0
                for offset in OFFSETS_3_6_0_18:
                    address = base + offset
                    value = pm.read_uint(address)
                    if value == self.target_hex:
                        already += 1
                    elif value == self.source_hex:
                        found_source += 1
                        pm.write_uint(address, self.target_hex)
                    else:
                        # 同一个 DLL 里版本号都该一样，对不上说明不是 3.6.0.18
                        raise RuntimeError(
                            f"偏移 0x{offset:X} 的值是 0x{value:08X}，既不是 "
                            f"0x{self.source_hex:08X} 也不是 0x{self.target_hex:08X}"
                        )

                if found_source or already:
                    patched = found_source
                    detail = (
                        f"已伪装成 {self.target}（改 {found_source} 处"
                        f"{'，其余已是目标值' if already else ''}），现在可以扫码登录了"
                        if found_source
                        else f"之前已经伪装过（{already} 处均为 {self.target}），直接登录即可"
                    )
                    log.info(detail)
                    return PatchResult(
                        ok=True,
                        detail=detail,
                        patched=found_source,
                        source_hex=f"0x{self.source_hex:08X}",
                        target_hex=f"0x{self.target_hex:08X}",
                        pid=int(pm.process_id),
                    )
            except RuntimeError as exc:
                return PatchResult(
                    ok=False,
                    detail=str(exc),
                    source_hex=f"0x{self.source_hex:08X}",
                    target_hex=f"0x{self.target_hex:08X}",
                )
            except Exception as exc:  # noqa: BLE001 - 读内存失败，重试
                last_error = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    pm.close_process()
                except Exception:  # noqa: BLE001
                    pass

            time.sleep(interval)

        return PatchResult(
            ok=False,
            detail=f"没打成补丁：{last_error}",
            patched=patched,
            source_hex=f"0x{self.source_hex:08X}",
            target_hex=f"0x{self.target_hex:08X}",
        )
