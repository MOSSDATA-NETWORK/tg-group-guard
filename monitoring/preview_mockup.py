"""按 monitoring/grafana-dashboard.json 的 gridPos 布局绘制高保真预览图(示例数据)。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.executable).parent.parent.parent))
from daimon_runtime import setup_plot

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Wedge

setup_plot()

# ---- Grafana 暗色主题 ----
BG = "#111217"
PANEL = "#181b1f"
BORDER = "#2c323a"
TEXT = "#d8d9da"
SUB = "#8e8e8e"
GRID = "#2a2e33"
BLUE, GREEN, RED, YELLOW, ORANGE, PURPLE = "#5794f2", "#73bf69", "#f2495c", "#fade2a", "#ff9830", "#b877d9"

GRID_W, UNIT_H = 24, 26  # 网格:24 列,总高 26 行单位
COL_IN, ROW_IN = 0.72, 0.56
FIG_W, FIG_H = GRID_W * COL_IN, UNIT_H * ROW_IN + 0.5

fig = plt.figure(figsize=(FIG_W, FIG_H), facecolor=BG)


def panel_rect(x, y, w, h):
    """gridPos -> figure 坐标(留小间距模拟面板间隙)。"""
    pad_x, pad_y = 0.06, 0.10
    left = (x + pad_x) * COL_IN / FIG_W
    width = (w - 2 * pad_x) * COL_IN / FIG_W
    top = 1.0 - ((y + pad_y) * ROW_IN + 0.5) / FIG_H
    bottom = 1.0 - ((y + h - pad_y) * ROW_IN + 0.5) / FIG_H
    return left, bottom, width, top - bottom


def draw_panel(x, y, w, h, title):
    l, b, ww, hh = panel_rect(x, y, w, h)
    ax = fig.add_axes([l, b, ww, hh])
    ax.set_facecolor(PANEL)
    for s in ax.spines.values():
        s.set_color(BORDER)
        s.set_linewidth(1.2)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, loc="left", color=TEXT, fontsize=11, pad=8,
                 fontweight="medium", x=0.02)
    return ax


def draw_row(y, title):
    l, b, ww, hh = panel_rect(0, y, 24, 1)
    ax = fig.add_axes([l, b, ww, hh])
    ax.set_facecolor("#1c1f24")
    for s in ax.spines.values():
        s.set_color(BORDER)
        s.set_linewidth(1.2)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.text(0.012, 0.5, title, va="center", ha="left",
            color=TEXT, fontsize=12, fontweight="bold", transform=ax.transAxes)
    ax.plot([0.004, 0.004], [0.2, 0.8], color=BLUE, lw=3,
            transform=ax.transAxes, solid_capstyle="round")


rng = np.random.default_rng(7)
t = np.arange(48)  # 48 个半小时点 = 24h


def smooth(base, noise, n=48):
    return np.maximum(0, base + rng.normal(0, noise, n).cumsum() * 0.3 + rng.normal(0, noise, n))


def style_ts(ax):
    ax.set_facecolor(PANEL)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.8)
    ax.tick_params(colors=SUB, labelsize=8, length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([0, 12, 24, 36, 47])
    ax.set_xticklabels(["-24h", "-18h", "-12h", "-6h", "现在"], fontsize=8)


# ---------- Row 1 ----------
draw_row(0, "核心指标")

# stat: 验证通过率(24h) - 带迷你走势
ax = draw_panel(0, 1, 5, 6, "验证通过率（24h）")
ax.text(0.5, 0.52, "92.4%", transform=ax.transAxes, ha="center", va="center",
        color=GREEN, fontsize=30, fontweight="bold")
v = smooth(0.92, 0.02)
ax.plot(np.linspace(0.08, 0.92, 48), 0.12 + 0.12 * (v - v.min()) / (np.ptp(v) + 1e-9),
        transform=ax.transAxes, color=GREEN, lw=1.2, alpha=0.7)
ax.text(0.5, 0.08, "verified 986 / expired 81", transform=ax.transAxes,
        ha="center", color=SUB, fontsize=8)

# stat: 处理消息(24h)
ax = draw_panel(5, 1, 5, 6, "处理消息（24h）")
ax.text(0.5, 0.52, "3,214", transform=ax.transAxes, ha="center", va="center",
        color=BLUE, fontsize=30, fontweight="bold")
m = smooth(60, 15)
ax.fill_between(np.linspace(0.08, 0.92, 48), 0.06,
                0.06 + 0.22 * (m - m.min()) / (np.ptp(m) + 1e-9),
                transform=ax.transAxes, color=BLUE, alpha=0.25)

# stat: 广告拦截(24h)
ax = draw_panel(10, 1, 5, 6, "广告拦截（24h）")
ax.text(0.5, 0.52, "137", transform=ax.transAxes, ha="center", va="center",
        color=RED, fontsize=30, fontweight="bold")

# stat: LLM 调用进行中
ax = draw_panel(15, 1, 4, 6, "LLM 调用进行中")
ax.text(0.5, 0.52, "2", transform=ax.transAxes, ha="center", va="center",
        color=PURPLE, fontsize=30, fontweight="bold")

# stat: Redis 状态
ax = draw_panel(19, 1, 5, 6, "评分 Redis 状态")
ax.text(0.5, 0.55, "正常", transform=ax.transAxes, ha="center", va="center",
        color=GREEN, fontsize=28, fontweight="bold")
ax.text(0.5, 0.22, "● 已连接,评分正常持久化", transform=ax.transAxes,
        ha="center", color=SUB, fontsize=8)

# ---------- Row 2 ----------
# 消息处理速率(堆叠)
ax = draw_panel(0, 7, 12, 9, "消息处理速率（按结果）")
received = smooth(3.2, 0.8)
kw = smooth(0.35, 0.15)
passed = smooth(1.4, 0.4)
flagged = smooth(0.22, 0.1)
ax.stackplot(t, received, kw, passed, flagged,
             labels=["received 收到", "keyword_deleted 关键词删除", "ad_passed 检测通过", "ad_flagged 判为广告"],
             colors=[BLUE, ORANGE, GREEN, RED], alpha=0.85, lw=0)
style_ts(ax)
ax.set_ylabel("msg/s", color=SUB, fontsize=8)
ax.legend(loc="upper left", fontsize=7.5, frameon=False, labelcolor=TEXT, ncol=2)

# 消息构成 donut
ax = draw_panel(12, 7, 6, 9, "消息处理构成（24h）")
vals = [2891, 186, 1037, 137]
labels = ["收到 2891", "关键词删除 186", "检测通过 1037", "判为广告 137"]
colors = [BLUE, ORANGE, GREEN, RED]
wedges, _ = ax.pie(vals, colors=colors, startangle=90,
                   wedgeprops=dict(width=0.34, edgecolor=PANEL, linewidth=2),
                   radius=1.0, center=(0, 0.12), frame=False)
ax.text(0, 0.12, "3,214", ha="center", va="center", color=TEXT,
        fontsize=15, fontweight="bold")
ax.text(0, -0.10, "总量", ha="center", va="center", color=SUB, fontsize=8)
ax.legend(wedges, labels, loc="lower center", bbox_to_anchor=(0.5, -0.16),
          fontsize=7.5, frameon=False, labelcolor=TEXT, ncol=2,
          handlelength=1.0, columnspacing=0.8)

# 验证结果速率 bars
ax = draw_panel(18, 7, 6, 9, "验证结果速率")
bt = np.arange(24)
ver = np.maximum(0, 2.2 + rng.normal(0, 0.9, 24))
exp = np.maximum(0, 0.3 + rng.normal(0, 0.18, 24))
ax.bar(bt - 0.2, ver, width=0.4, color=GREEN, label="verified 通过")
ax.bar(bt + 0.2, exp, width=0.4, color=RED, label="expired 超时")
ax.set_facecolor(PANEL)
ax.grid(color=GRID, linewidth=0.6, axis="y")
ax.tick_params(colors=SUB, labelsize=8, length=0)
for s in ax.spines.values():
    s.set_visible(False)
ax.set_xticks([0, 6, 12, 18, 23])
ax.set_xticklabels(["-24h", "-18h", "-12h", "-6h", "现在"], fontsize=8)
ax.legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=TEXT)

# ---------- Row 3 ----------
draw_row(16, "LLM 广告检测")

# LLM 延迟分位
ax = draw_panel(0, 17, 12, 9, "LLM 延迟分位")
p50 = smooth(1.8, 0.3)
p95 = p50 * 2.1 + np.abs(rng.normal(0, 0.4, 48))
p99 = p50 * 3.4 + np.abs(rng.normal(0, 0.8, 48))
ax.plot(t, p50, color=GREEN, lw=1.6, label="p50")
ax.plot(t, p95, color=YELLOW, lw=1.6, label="p95")
ax.plot(t, p99, color=RED, lw=1.6, label="p99")
ax.fill_between(t, p50, p99, color=RED, alpha=0.06)
style_ts(ax)
ax.set_ylabel("秒", color=SUB, fontsize=8)
ax.legend(loc="upper left", fontsize=8, frameon=False, labelcolor=TEXT)

# LLM 判定速率
ax = draw_panel(12, 17, 8, 9, "LLM 判定速率（按提供方）")
ok = smooth(0.8, 0.25)
fl = smooth(0.12, 0.06)
ax.plot(t, ok, color=BLUE, lw=1.6, label="deepseek · flagged=false")
ax.plot(t, fl, color=RED, lw=1.6, label="deepseek · flagged=true")
ax.fill_between(t, 0, ok, color=BLUE, alpha=0.12)
style_ts(ax)
ax.set_ylabel("次/s", color=SUB, fontsize=8)
ax.legend(loc="upper left", fontsize=7.5, frameon=False, labelcolor=TEXT)

# LLM 并发 gauge
ax = draw_panel(20, 17, 4, 9, "LLM 并发占用")
theta = np.linspace(np.pi, 0, 200)
for frac_lo, frac_hi, c in [(0, 0.5, GREEN), (0.5, 0.875, YELLOW), (0.875, 1.0, RED)]:
    a1, a2 = 180 - frac_hi * 180, 180 - frac_lo * 180
    ax.add_patch(Wedge((0, -0.25), 1.0, a1, a2, width=0.22, facecolor=c, alpha=0.28))
val = 3
maxv = 8
ang = 180 - (val / maxv) * 180
ax.add_patch(Wedge((0, -0.25), 1.0, ang, 180, width=0.22, facecolor=GREEN))
ax.text(0, -0.18, f"{val}", ha="center", va="center", color=TEXT,
        fontsize=26, fontweight="bold")
ax.text(0, -0.52, f"上限 {maxv}", ha="center", va="center", color=SUB, fontsize=9)
ax.set_xlim(-1.15, 1.15)
ax.set_ylim(-0.85, 1.1)

out = Path("monitoring/dashboard-preview.png")
fig.savefig(out, dpi=110, facecolor=BG, bbox_inches="tight", pad_inches=0.15)
plt.close(fig)
print("saved:", out.resolve())
