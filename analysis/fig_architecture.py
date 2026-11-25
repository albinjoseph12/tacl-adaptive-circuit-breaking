#!/usr/bin/env python3
"""Architecture / control-loop diagram (Fig. 1) drawn with matplotlib."""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

ROOT = os.environ.get("ACB_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fig, ax = plt.subplots(figsize=(13, 6.2), dpi=200)
ax.set_xlim(0, 140); ax.set_ylim(0, 60); ax.axis("off")
INK, MUTED, ACC, BLUE, AQUA = "#0b0b0b", "#52514e", "#eb6834", "#2a78d6", "#1baf7a"

def box(x, y, w, h, title, lines=(), fc="#fcfcfb", ec="#9a9a97", tc=INK, lw=1.2, r=1.2):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw))
    ax.text(x + w / 2, y + h - 2.6, title, ha="center", va="center", fontsize=9.2, fontweight="bold", color=tc)
    for k, l in enumerate(lines):
        ax.text(x + 1.6, y + h - 6.2 - 3.1 * k, l, ha="left", va="center", fontsize=7.4, color=MUTED)

def arrow(x1, y1, x2, y2, color=INK, lw=1.4, style="-|>", ls="-", text=None, tpos=0.5, toff=(0, 1.6), fs=7.4):
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=12, color=color, lw=lw, linestyle=ls)
    ax.add_patch(a)
    if text:
        ax.text(x1 + (x2 - x1) * tpos + toff[0], y1 + (y2 - y1) * tpos + toff[1], text, fontsize=fs, color=color, ha="center", va="bottom")

# CPU lanes
ax.add_patch(FancyBboxPatch((1, 3), 76, 55, boxstyle="round,pad=0,rounding_size=1.5", fc="#f4f4f1", ec="#d9d9d4", lw=0.8))
ax.text(3, 55.5, "client tier  (vCPU 0)", fontsize=8, color=MUTED, style="italic")
ax.add_patch(FancyBboxPatch((79, 3), 60, 55, boxstyle="round,pad=0,rounding_size=1.5", fc="#f4f4f1", ec="#d9d9d4", lw=0.8))
ax.text(81, 55.5, "downstream tier  (vCPU 1)", fontsize=8, color=MUTED, style="italic")

box(3, 30, 20, 22, "Locust 2.46", ["500 concurrent users", "45/25/15/15 % OLTP mix", "think 0.4–1.2 s", "flash crowd: 0.1–0.3 s", "per-request outcome log"], fc="#ffffff")
box(27, 22, 48, 30, "Reactive gateway (Netty 4.1)", ["non-blocking event loops · async client · 3 s timeout"], fc="#ffffff")
ax.add_patch(FancyBboxPatch((29, 24), 44, 19, boxstyle="round,pad=0,rounding_size=1", fc="#fdf1ec", ec=ACC, lw=1.2))
ax.text(51, 40.6, "TACL controller  (100 ms tick)", ha="center", fontsize=8.4, fontweight="bold", color=ACC)
for k, l in enumerate(["pressure index   P = max( σt,  0.9·σp,  σq,  w/w_ref,  g/g_ref )",
                       "admission limit  L  by pressure-proportional AIMD   →  shed",
                       "adaptive bulkheads   C_r = max(2, ⌈L·s_r⌉)   →  shed_route",
                       "breaker   θ_f(P) = 30 + 40·(1−P) %  ·  P-gated recovery"]):
    ax.text(30.5, 36.6 - 3.3 * k, l, fontsize=7.4, color=INK, ha="left", va="center")

box(82, 22, 32, 30, "Order service (Tomcat 10.1)", ["48 worker threads (request port 8081)", "HikariCP: 16 conns, 2 s acquire timeout", "PostgreSQL JDBC 42.7 · 256 MB G1 heap", "GC-storm fault injector"], fc="#ffffff")
ax.add_patch(FancyBboxPatch((83.5, 23.5), 29, 8, boxstyle="round,pad=0,rounding_size=0.8", fc="#e9f1fb", ec=BLUE, lw=1))
ax.text(98, 29.3, "/telemetry  (management port 8091)", ha="center", fontsize=7.4, color=BLUE, fontweight="bold")
ax.text(98, 26.6, "GC pause (1 s window) · threads busy/max", ha="center", fontsize=6.6, color=BLUE)
ax.text(98, 24.8, "pool active / pending · acquire wait (EWMA)", ha="center", fontsize=6.6, color=BLUE)

box(118, 22, 19, 30, "PostgreSQL 16", ["orders: 2,000,000 rows", "306 MB, fully cached", "lock_timeout = 1 s", "row-lock fault:", "SELECT … FOR UPDATE", "on 5,000 hot ids, 45 s"], fc="#ffffff")

box(3, 6, 72, 12, "100 ms experiment logger", ["pg_stat_activity + pg_locks   ·   /telemetry snapshot   ·   /control/state   →   one joined CSV timeline"], fc="#ffffff")

arrow(23, 41, 27, 41, text="HTTP", toff=(0, 1.2))
arrow(75, 46, 82, 46, text="/orders/*", toff=(0, 1.2))
arrow(114, 40, 118, 40, text="JDBC", toff=(0, 1.2))
arrow(83.5, 27.5, 75, 27.5, color=BLUE, ls="--")
ax.text(79.2, 30.2, "poll\n100 ms", fontsize=6.4, color=BLUE, ha="center", va="bottom")
arrow(98, 18, 98, 22, color=MUTED, ls=":", lw=1)
arrow(127, 18, 127, 22, color=MUTED, ls=":", lw=1)
arrow(51, 18, 51, 22, color=MUTED, ls=":", lw=1)
ax.text(100, 12, "pg_stat_activity, pg_locks,\n/telemetry every 100 ms", fontsize=6.8, color=MUTED, va="center")

fig.savefig(f"{ROOT}/paper/fig_architecture.png", bbox_inches="tight", facecolor="white")
print("ok")
