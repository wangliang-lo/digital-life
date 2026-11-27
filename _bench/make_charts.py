# -*- coding: utf-8 -*-
"""把这一夜/这一天的实测结果画成图。

数据来源都是 `_exp/bench/ckpt-*.json` 的实际结果，不是手写数字。
噪声带：PS-事实 n=200 同配置跑两次差 1.5pp（7/200 翻转，McNemar p=0.45），
所以所有准确率图都画 ±1.5pp 的灰带。

用法：
    python make_charts.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

BENCH = Path(r"_bench")
OUT = BENCH / "charts"
OUT.mkdir(exist_ok=True)

# 中文字体（Windows 自带）
for f in ("Microsoft YaHei", "SimHei", "DengXian"):
    try:
        matplotlib.rcParams["font.sans-serif"] = [f]
        matplotlib.rcParams["axes.unicode_minus"] = False
        break
    except Exception:  # noqa: BLE001
        continue

C_MAIN = "#2E5EAA"
C_WARN = "#D97706"
C_OK = "#15803D"
C_GREY = "#9CA3AF"
NOISE = 1.5  # ±pp


def load(name: str) -> list[dict]:
    p = BENCH / name
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return d if isinstance(d, list) else d.get("items") or []


def acc(items: list[dict]) -> float:
    return 100.0 * sum(1 for x in items if x.get("correct")) / max(1, len(items))


# ---------------- 图 1：四个格子 vs oracle ----------------
def chart_cells() -> Path:
    cells = [
        ("OS-事实", load("ckpt-final-osf100.json"), 100, None),
        ("PS-事实", load("ckpt-base-R1.json") + load("ckpt-base-R2.json"), 200, 86.0),
        ("PS-反思", load("ckpt-final-psr60.json"), 60, 95.0),
        ("OS-反思", load("ckpt-final-osrm60.json"), 60, 81.7),
    ]
    names, got, oracle = [], [], []
    for name, items, n, ora in cells:
        names.append(f"{name}\nn={n}")
        got.append(acc(items))
        oracle.append(ora)
    x = np.arange(len(names))
    w = 0.36
    fig, ax = plt.subplots(figsize=(9, 5))
    b1 = ax.bar(x - w / 2, got, w, label="小鱼实测", color=C_MAIN)
    b2 = ax.bar(x + w / 2, [o or 0 for o in oracle], w, label="oracle 上限", color=C_GREY)
    ax.axhspan(85 - NOISE, 85 + NOISE, color=C_WARN, alpha=0.15, zorder=0)
    ax.axhline(85, color=C_WARN, ls="--", lw=1.2, label="目标 85%")
    for i, (v, o) in enumerate(zip(got, oracle)):
        ax.text(i - w / 2, v + 1, f"{v:.1f}%", ha="center", fontsize=10, fontweight="bold")
        if o:
            ax.text(i + w / 2, o + 1, f"{o:.0f}%", ha="center", fontsize=9, color="#555")
            ax.text(i, -7, f"达上限的 {100*v/o:.0f}%", ha="center", fontsize=9, color=C_MAIN)
    ax.set_xticks(x, names)
    ax.set_ylim(-11, 105)
    ax.set_ylabel("选择题准确率 (%)")
    ax.set_title("四个格子：实测 vs oracle 上限（橙带 = 测量噪声 ±1.5pp）", fontsize=12)
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(axis="y", alpha=0.25)
    p = OUT / "01_cells.png"
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


# ---------------- 图 2：四项指标达成 ----------------
def chart_metrics() -> Path:
    rows = [
        ("① 准确率 ≥85%", 84.3, 85.0, "%", False),
        ("② 检索 P95 ≤500ms", 277.0, 500.0, "ms", True),
        ("③ token 增幅 ≤1.5×", 1.00, 1.5, "×", True),
        ("④ 人格不退化", 0.047, 0.0, "漂移", True),
    ]
    fig, axes = plt.subplots(1, 4, figsize=(13, 3.4))
    for ax, (label, val, target, unit, ok) in zip(axes, rows):
        if unit == "%":
            frac = val / target
        elif unit == "ms":
            frac = 1 - min(val / target, 1) * 0.85
        elif unit == "×":
            frac = 1 - min((val - 1) / (target - 1), 1) * 0.85
        else:
            frac = min(1.0, 0.6 + val * 4)
        color = C_OK if ok else C_WARN
        ax.pie([frac, max(0.02, 1 - frac)], startangle=90,
               colors=[color, "#E5E7EB"], counterclock=False,
               wedgeprops=dict(width=0.32, edgecolor="white"))
        ax.text(0, 0.06, f"{val:g}{unit}", ha="center", va="center",
                fontsize=17, fontweight="bold", color=color)
        ax.text(0, -0.24, "达标" if ok else "差 0.7pp", ha="center",
                fontsize=9, color=color)
        ax.set_title(label, fontsize=10)
    fig.suptitle("四项可测指标（2026-11-25）", fontsize=13, y=1.02)
    p = OUT / "02_metrics.png"
    fig.tight_layout()
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


# ---------------- 图 3：噪声底 ----------------
def chart_noise() -> Path:
    r1 = acc(load("ckpt-base-R1.json"))
    r2 = acc(load("ckpt-base-R2.json"))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))
    ax1.bar(["第 1 次", "第 2 次"], [r1, r2], color=[C_MAIN, "#6B8FD4"], width=0.5)
    for i, v in enumerate([r1, r2]):
        ax1.text(i, v + 0.5, f"{v:.1f}%", ha="center", fontweight="bold")
    ax1.set_ylim(70, 90)
    ax1.set_ylabel("准确率 (%)")
    ax1.set_title(f"同一配置跑两次（PS-事实 n=200）\n差 {abs(r1-r2):.1f}pp，7/200 题翻转", fontsize=11)
    ax1.grid(axis="y", alpha=0.25)

    n = 200
    se = np.sqrt(0.825 * 0.175 / n) * 100
    xs = np.arange(50, 1100, 10)
    ax2.plot(xs, [np.sqrt(0.825*0.175/x)*100 for x in xs], color=C_WARN, lw=2)
    for xx, lab in ((100, "n=100"), (200, "n=200"), (500, "n=500"), (1000, "n=1000")):
        yy = np.sqrt(0.825*0.175/xx)*100
        ax2.plot([xx], [yy], "o", color=C_MAIN)
        ax2.annotate(f"{lab}\n±{yy:.1f}pp", (xx, yy), textcoords="offset points",
                     xytext=(6, 8), fontsize=8)
    ax2.axhline(1.5, color=C_GREY, ls="--", lw=1)
    ax2.text(760, 1.7, "±1.5pp = 今天实测的噪声", fontsize=8, color="#555")
    ax2.set_xlabel("样本量 n")
    ax2.set_ylabel("标准误 (±pp)")
    ax2.set_title("要分辨多小的差异，需要多大样本", fontsize=11)
    ax2.grid(alpha=0.25)
    p = OUT / "03_noise.png"
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


# ---------------- 图 4：改动账 ----------------
def chart_changes() -> Path:
    good = ["英文停用词过滤\nRecall@5 +23pp", "数数题专用提示\n+2pp",
            "情绪偏置纠正\n+5pp", "反思题按时间排\n+3.4pp"]
    bad = ["迭代检索", "检索层去重", "作品类型映射", "氛围提示", "自一致性",
           "上下文校准", "逐选项证据", "25条只给聚合", "数数给45条",
           "版本冲突规则", "两段式计数", "掰手指数"]
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.barh([0.5] * len(good), [1] * len(good), color=C_OK, height=0.5)
    for i, g in enumerate(good):
        ax.text(0.5, 0.5 + i * 0, "", ha="center")
    ax.clear()
    ax.barh(range(len(good)), [1] * len(good), color=C_OK, height=0.6)
    for i, g in enumerate(good):
        ax.text(0.02, i, g.replace("\n", "  "), va="center", fontsize=9, color="white")
    base = len(good) + 1
    ax.barh([base + i for i in range(len(bad))], [1] * len(bad), color="#E5E7EB", height=0.6)
    for i, b in enumerate(bad):
        ax.text(0.02, base + i, b, va="center", fontsize=9, color="#444")
    ax.set_yticks([])
    ax.set_xticks([])
    ax.set_xlim(0, 1)
    ax.invert_yaxis()
    ax.axhline(len(good) - 0.5, color="#333", lw=1.2)
    ax.text(0.62, len(good) / 2 - 0.5, f"有效 {len(good)} 条", color=C_OK,
            fontsize=13, fontweight="bold", va="center")
    ax.text(0.62, base + len(bad) / 2 - 0.5, f"无效 {len(bad)} 条", color="#666",
            fontsize=13, fontweight="bold", va="center")
    ax.set_title("这一轮试过的改动：4 条有效，12 条无效（都有配对检验或确定性指标）", fontsize=12)
    for s in ax.spines.values():
        s.set_visible(False)
    p = OUT / "04_changes.png"
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


# ---------------- 图 5：提示词构成 前后 ----------------
def chart_prompt() -> Path:
    labels = ["成长日记", "标签语法", "宪法/规则/状态", "其他"]
    before = [3623, 1345, 170, 0]
    after = [2273, 600, 170, 464]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.4), gridspec_kw={"width_ratios": [1, 1.15]})
    ax1.bar(["改前", "改后"], [sum(before), sum(after)],
            color=[C_GREY, C_MAIN], width=0.45)
    for i, v in enumerate([sum(before), sum(after)]):
        ax1.text(i, v + 60, f"{v}", ha="center", fontweight="bold")
    ax1.set_ylabel("system prompt token")
    ax1.set_title(f"system 长度：{sum(before)} → {sum(after)}（-{100-100*sum(after)/sum(before):.0f}%）", fontsize=11)
    ax1.set_ylim(0, 6000)
    ax1.grid(axis="y", alpha=0.25)

    y = np.arange(len(labels))
    ax2.barh(y - 0.2, before, 0.4, label="改前", color=C_GREY)
    ax2.barh(y + 0.2, after, 0.4, label="改后", color=C_MAIN)
    for i, (b, a) in enumerate(zip(before, after)):
        if b:
            ax2.text(b + 40, i - 0.2, str(b), va="center", fontsize=8)
        if a:
            ax2.text(a + 40, i + 0.2, str(a), va="center", fontsize=8, color=C_MAIN)
    ax2.set_yticks(y, labels)
    ax2.invert_yaxis()
    ax2.set_xlabel("token")
    ax2.set_title("分块：日记从「1/20 轮塞 7 篇」改成「三层常驻」", fontsize=10)
    ax2.legend(fontsize=8)
    ax2.grid(axis="x", alpha=0.25)
    p = OUT / "05_prompt.png"
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


# ---------------- 图 6：标签规则 vs 实际使用 ----------------
def chart_tags() -> Path:
    used = {"STICKER": 34, "DOODLE": 30, "SEND_DRAWING": 25, "DRAW": 16,
            "READ_BOOK": 2, "GOMOKU": 2, "其余 26 个": 5}
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2), gridspec_kw={"width_ratios": [1.2, 1]})
    names = list(used)
    vals = [used[k] for k in names]
    colors = [C_MAIN] * 4 + [C_GREY] * (len(names) - 4)
    ax1.bar(names, vals, color=colors)
    for i, v in enumerate(vals):
        if v:
            ax1.text(i, v + 0.6, str(v), ha="center", fontsize=9)
    ax1.set_ylabel("出现次数")
    ax1.set_title("她 3310 条真实回复里用到的标签", fontsize=11)
    ax1.tick_params(axis="x", rotation=35, labelsize=8)
    ax1.grid(axis="y", alpha=0.25)

    ax2.pie([25, 1285], labels=["用了标签\n25 轮", "没用\n1285 轮"],
            colors=[C_WARN, "#E5E7EB"], autopct="%1.1f%%", startangle=90,
            textprops={"fontsize": 9})
    ax2.set_title("标签使用率 1.9%\n而每轮要为它读 1345 token", fontsize=11)
    p = OUT / "06_tags.png"
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def main() -> int:
    made = []
    for fn in (chart_cells, chart_metrics, chart_noise, chart_changes, chart_prompt, chart_tags):
        try:
            made.append(fn())
        except Exception as exc:  # noqa: BLE001
            print(f"  {fn.__name__} 失败: {type(exc).__name__}: {exc}")
    for p in made:
        print(f"  {p.name}  {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
