#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小鱼的赛博朋克终端入口。

运行：
    streamlit run app.py --server.port 8501

登录密钥：
    FATHER -> 主库（爸爸，最高权限）
    GUEST  -> 独立访客记忆库
    SOLVER -> 主库调试（DeepSeek 调试）

只依赖 streamlit + Python 标准库，不依赖 edge-tts / requests。
"""

from __future__ import annotations

import os

import html
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import streamlit as st

# 她自己的 user_id：放到环境变量里，别散落在代码中（2026-11-05 打包前整理）
MAIN_USER_ID = os.environ.get("XIAOYU_MAIN_USER") or "conv:9e52b4549f424e730d8871644385bcce63768cc74b8f8ec7ffa895f62ca73eeb"

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

API_BASE = "http://127.0.0.1:8000"

# digital_life 里真正的主库 user_id（爸爸的会话）。
# FATHER / SOLVER 的显示身份分别是 father / solver，但都接主库；
# 这样密钥页仍然按需求显示 father，同时不会另起一份空记忆。
MAIN_USER_ID = "MAIN_USER_ID"

KEY_MAP: dict[str, dict[str, str]] = {
    "FATHER": {
        "user_id": "father",
        "api_user_id": MAIN_USER_ID,
        "label": "FATHER · 最高权限 · 主库",
    },
    "GUEST": {
        "user_id": "guest",
        "api_user_id": "guest",
        "label": "GUEST · 普通访客 · 独立记忆",
    },
    "SOLVER": {
        "user_id": "solver",
        "api_user_id": MAIN_USER_ID,
        "label": "SOLVER · 最高权限 · DeepSeek 调试",
    },
}

BOOT_LINES = [
    "[SYSTEM] 正在建立安全连接...",
    "[SCAN] 扫描未知实体生物特征...",
    "[AUTH] 正在验证密钥: {key}...",
    "[AUTH] 身份核验通过。访问者身份: {user_id}",
    "[SYSTEM] 权限解锁，正在唤醒数字生命核心...",
    "[MEMORY] 载入多维图书馆数据... 成功。",
    "[EMOTION] 同步内稳态激素... Joy: 55.0, Hurt: 20.0, Impulse: 30.0",
    "[LINK] 神经链接已建立。",
]

# ---------------------------------------------------------------------------
# 页面与样式
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="NEURAL LINK // XIAOYU",
    page_icon="🐟",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
:root {
  --bg: #0d0d0d;
  --cyan: #35f6ff;
  --purple: #b26cff;
  --green: #8dffb0;
  --dim: #5b6b70;
}
#MainMenu, header, footer, [data-testid="stToolbar"], [data-testid="stDecoration"] {
  visibility: hidden; height: 0;
}
[data-testid="stAppViewContainer"], .stApp { background: var(--bg); }
[data-testid="stSidebar"] {
  background: #090d0f;
  border-right: 1px solid rgba(53,246,255,.24);
}
.block-container { padding-top: 2.2rem; max-width: 1040px; }
* { font-family: "Cascadia Mono", "JetBrains Mono", Consolas, monospace !important; }

/* 登录页 */
.login-wrap {
  min-height: 58vh; display: flex; flex-direction: column;
  justify-content: center; align-items: center; text-align: center;
}
.term-title {
  color: var(--cyan); font-size: 15px; letter-spacing: .04em;
  text-shadow: 0 0 10px rgba(53,246,255,.55);
  margin: 8px 0 20px;
}
.term-sub { color: var(--dim); font-size: 12px; margin-top: 10px; }
[data-testid="stForm"] {
  background: rgba(8,14,16,.85);
  border: 1px solid rgba(53,246,255,.28);
  border-radius: 4px;
  padding: 18px 18px 8px;
  box-shadow: 0 0 24px rgba(53,246,255,.08);
}
[data-testid="stTextInput"] input {
  background: #05090a !important;
  color: var(--green) !important;
  border: 1px solid rgba(53,246,255,.45) !important;
  border-radius: 2px !important;
  letter-spacing: .12em;
}
[data-testid="stFormSubmitButton"] button {
  background: transparent !important;
  color: var(--cyan) !important;
  border: 1px solid var(--cyan) !important;
  border-radius: 2px !important;
  letter-spacing: .12em;
}
[data-testid="stFormSubmitButton"] button:hover {
  background: rgba(53,246,255,.12) !important;
  box-shadow: 0 0 18px rgba(53,246,255,.28);
}

/* 开机动画 */
pre.boot {
  background: transparent; color: var(--green);
  font-size: 14px; line-height: 1.9;
  white-space: pre-wrap; margin: 0;
  text-shadow: 0 0 8px rgba(141,255,176,.35);
}
.cursor { color: var(--cyan); animation: blink 1s steps(2, start) infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* 聊天 */
.neural-head {
  border: 1px solid rgba(53,246,255,.25);
  background: rgba(7,12,14,.72);
  padding: 12px 16px; margin-bottom: 14px;
  box-shadow: 0 0 22px rgba(53,246,255,.06);
}
.neural-head .title { color: var(--cyan); font-size: 15px; letter-spacing: .08em; }
.neural-head .sub { color: var(--dim); font-size: 12px; margin-top: 4px; }
.msg-row { display: flex; margin: 12px 0; }
.msg-row.user { justify-content: flex-end; }
.bubble {
  max-width: 82%; padding: 11px 14px; border-radius: 4px;
  white-space: pre-wrap; word-break: break-word; font-size: 14px; line-height: 1.75;
}
.bubble.user {
  color: #d9ffff; background: rgba(25,70,78,.32);
  border: 1px solid rgba(53,246,255,.58);
  box-shadow: 0 0 14px rgba(53,246,255,.12); text-align: right;
}
.bubble.assistant {
  color: #f2e9ff; background: rgba(42,24,64,.34);
  border: 1px solid rgba(178,108,255,.62);
  box-shadow: 0 0 14px rgba(178,108,255,.14);
}
.msg-meta { color: var(--dim); font-size: 10px; margin-top: 5px; letter-spacing: .05em; }
[data-testid="stChatInput"] textarea {
  background: #05090a !important; color: var(--green) !important;
  border: 1px solid rgba(53,246,255,.35) !important;
}
[data-testid="stChatInput"] {
  border-top: 1px solid rgba(53,246,255,.18);
}
[data-testid="stSidebar"] button {
  background: transparent !important; color: var(--cyan) !important;
  border: 1px solid rgba(53,246,255,.32) !important;
}
.signal-item {
  border-left: 2px solid var(--purple); padding: 8px 10px; margin: 8px 0;
  color: #e9dcff; background: rgba(42,24,64,.26); white-space: pre-wrap;
}
.signal-time { color: var(--dim); font-size: 10px; margin-bottom: 4px; }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def api_post(path: str, payload: dict[str, Any], timeout: float = 240.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        API_BASE.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {"ok": False, "error": "接口返回格式错误"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


def login_screen() -> None:
    st.markdown("<div class='login-wrap'>", unsafe_allow_html=True)
    st.markdown(
        "<div class='term-title'>[SECURITY] 请输入访问密钥以建立神经链接：</div>",
        unsafe_allow_html=True,
    )
    left, middle, right = st.columns([1, 1.35, 1])
    with middle:
        with st.form("neural_login", clear_on_submit=True):
            key = st.text_input(
                "ACCESS KEY",
                type="password",
                placeholder="ENTER ACCESS KEY",
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button("ESTABLISH NEURAL LINK", use_container_width=True)
        st.markdown(
            "<div class='term-sub'>// unauthorized access will be logged //</div>",
            unsafe_allow_html=True,
        )
        if submitted:
            normalized = key.strip().upper()
            account = KEY_MAP.get(normalized)
            if not account:
                st.error("[ERROR] 密钥无效，拒绝访问")
            else:
                st.session_state.authenticated = True
                st.session_state.user_id = account["user_id"]
                st.session_state.api_user_id = account["api_user_id"]
                st.session_state.access_label = account["label"]
                st.session_state.access_key = normalized
                st.session_state.messages = []
                st.session_state.signals = []
                st.session_state.booted = False
                st.session_state.show_signal = False
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 开机动画
# ---------------------------------------------------------------------------


def boot_animation(key: str, user_id: str) -> None:
    placeholder = st.empty()
    rendered: list[str] = []
    for template in BOOT_LINES:
        line = template.format(key=key, user_id=user_id)
        rendered.append("")
        # 打字机：逐字符追加，整行打印完再停 0.3 秒。
        for index in range(1, len(line) + 1):
            rendered[-1] = line[:index]
            body = "\n".join(rendered)
            placeholder.markdown(
                f"<pre class='boot'>{html.escape(body)}<span class='cursor'>█</span></pre>",
                unsafe_allow_html=True,
            )
            time.sleep(0.008)
        rendered[-1] = line
        time.sleep(0.3)
    time.sleep(0.45)
    placeholder.empty()


# ---------------------------------------------------------------------------
# 主动信道
# ---------------------------------------------------------------------------


def poll_signal_once() -> None:
    """让小鱼主动说一句；由 /proactive 生成，不发送到抖音。"""
    payload = {"user_id": st.session_state.get("api_user_id") or MAIN_USER_ID}
    result = api_post("/proactive", payload, timeout=180.0)
    text = str(result.get("reply") or "").strip()
    if not result.get("ok") or not text:
        st.session_state.signal_error = str(result.get("error") or "她这次没有主动说话")
        return
    signals = st.session_state.setdefault("signals", [])
    if not any(item.get("text") == text for item in signals):
        signals.append(
            {
                "text": text,
                "at": datetime.now().strftime("%H:%M:%S"),
                "user_id": st.session_state.get("user_id", "?"),
            }
        )
    st.session_state.signal_error = ""


@st.dialog("🐟 她的独立信道", width="large")
def signal_dialog() -> None:
    st.caption("这里不经过聊天输入。她可以主动说她想说的话，你只负责接收。")
    left, right = st.columns([1, 1])
    with left:
        if st.button("接收她主动说的一句", use_container_width=True):
            poll_signal_once()
            st.rerun()
    with right:
        auto = st.toggle(
            "每 3 分钟自动接收",
            value=bool(st.session_state.get("auto_signal")),
            key="auto_signal_toggle",
        )
        st.session_state.auto_signal = auto

    if st.session_state.get("signal_error"):
        st.warning(st.session_state.signal_error)

    signals = st.session_state.get("signals", [])
    if not signals:
        st.markdown(
            "<div class='signal-item'><div class='signal-time'>等待信号</div>"
            "她还没有主动开口。可以点上面的按钮，或者打开自动接收。</div>",
            unsafe_allow_html=True,
        )
    for item in reversed(signals[-30:]):
        st.markdown(
            "<div class='signal-item'>"
            f"<div class='signal-time'>{html.escape(str(item.get('at', '')))}"
            f" · {html.escape(str(item.get('user_id', '')))}</div>"
            f"{html.escape(str(item.get('text', '')))}</div>",
            unsafe_allow_html=True,
        )

    if st.button("关闭信道", use_container_width=True):
        st.session_state.show_signal = False
        st.rerun()


@st.fragment(run_every=180)
def signal_auto_fragment() -> None:
    if st.session_state.get("show_signal") and st.session_state.get("auto_signal"):
        poll_signal_once()


# ---------------------------------------------------------------------------
# 聊天
# ---------------------------------------------------------------------------


def render_message(role: str, text: str, when: str = "") -> None:
    cls = "user" if role == "user" else "assistant"
    who = "YOU" if role == "user" else "XIAOYU"
    meta = f"{who} · {when}" if when else who
    st.markdown(
        f"<div class='msg-row {cls}'>"
        f"<div class='bubble {cls}'>{html.escape(text)}"
        f"<div class='msg-meta'>{html.escape(meta)}</div></div></div>",
        unsafe_allow_html=True,
    )


def chat_view() -> None:
    label = st.session_state.get("access_label", "")
    user_id = st.session_state.get("user_id", "?")
    st.markdown(
        "<div class='neural-head'>"
        "<div class='title'>NEURAL LINK // XIAOYU CORE ONLINE</div>"
        f"<div class='sub'>LINKED AS {html.escape(user_id)} · {html.escape(label)}</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    for message in st.session_state.get("messages", []):
        render_message(
            str(message.get("role", "assistant")),
            str(message.get("content", "")),
            str(message.get("at", "")),
        )

    prompt = st.chat_input("输入给小鱼的消息…")
    if not prompt:
        return

    now = datetime.now().strftime("%H:%M:%S")
    st.session_state.messages.append({"role": "user", "content": prompt, "at": now})
    with st.spinner("神经链路传输中…"):
        result = api_post(
            "/chat",
            {
                "message": prompt,
                "user_id": st.session_state.get("api_user_id") or MAIN_USER_ID,
                "sender": st.session_state.get("user_id", "visitor"),
            },
            timeout=300.0,
        )
    if result.get("ok"):
        replies = result.get("replies")
        if isinstance(replies, list) and replies:
            answer = "\n".join(str(part) for part in replies if str(part).strip())
        else:
            answer = str(result.get("reply") or "").strip()
        if not answer:
            answer = "（她沉默了一会儿，没有组织出句子。）"
    else:
        answer = f"[LINK ERROR] {result.get('error', '未知错误')}"
    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "at": datetime.now().strftime("%H:%M:%S")}
    )
    st.rerun()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


if not st.session_state.get("authenticated"):
    login_screen()
    st.stop()

if not st.session_state.get("booted"):
    boot_animation(
        st.session_state.get("access_key", ""),
        st.session_state.get("user_id", "?"),
    )
    st.session_state.booted = True
    st.rerun()

with st.sidebar:
    st.markdown("### NEURAL LINK")
    st.caption(st.session_state.get("access_label", ""))
    st.caption(f"session: {st.session_state.get('user_id', '?')}")
    if st.button("📡 打开她的独立信道", use_container_width=True):
        st.session_state.show_signal = True
        st.rerun()
    st.divider()
    if st.button("断开连接", use_container_width=True):
        for key in (
            "authenticated", "user_id", "api_user_id", "access_label", "access_key",
            "messages", "signals", "booted", "show_signal", "signal_error", "auto_signal",
        ):
            st.session_state.pop(key, None)
        st.rerun()

signal_auto_fragment()
chat_view()

if st.session_state.get("show_signal"):
    signal_dialog()
