# -*- coding: utf-8 -*-
"""Generate the report/deck figures that derive from the 2026-11-24 results.

    python tools/make_figures.py              # writes docs/figures/fig5..fig7

fig1..fig4 predate this script (they were drawn from the calibration and S1
matrices in the working repo); they are kept untouched so the images cited by
the earlier commits stay byte-identical. This script only covers the figures
added together with the beta sweep and the v4 deployment.

Every number below is copied from TABR_project/docs/REPORT_20261124.md
(sections in the comments) or from the archived sweep summary; keep them in
step with the report when the report changes.
"""
import io
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]          # TABR_project/
DOCS = ROOT / "docs"
OUT = DOCS / "figures"
SWEEP = ROOT.parent / "实验归档" / "2026-11-24_beta扫描" / "summary.csv"

TEAL = "#0E7C86"
ORANGE = "#C2710C"
GREY = "#7F7F7F"
PURPLE = "#7B5AA6"
BLUE = "#2E75B6"
GREEN = "#3C9A4E"


def read_sweep():
    """Return (compact rows, ceiling rows) from the archived sweep summary."""
    if not SWEEP.exists():
        raise SystemExit("missing archived sweep: %s" % SWEEP)
    rows = list(csv.DictReader(io.open(SWEEP, encoding="utf-8", newline="")))
    compact = [r for r in rows if float(r["lead_b"]) < 2.6]
    ceiling = [r for r in rows if float(r["lead_b"]) > 2.6]
    compact.sort(key=lambda r: float(r["beta"]))
    ceiling.sort(key=lambda r: float(r["beta"]))
    return compact, ceiling


def fig5_beta(compact, ceiling):
    beta = [float(r["beta"]) for r in compact]
    r_brake = [float(r["lead_brake_r"]) for r in compact]
    d_info = [float(r["D_info_lower_ms"]) for r in compact]
    b0 = float(compact[0]["lead_b"])
    r_max = 10.0

    fig, axes = plt.subplots(1, 3, figsize=(12.4, 4.0), dpi=200)

    # (a) rate law in the braking window
    ax = axes[0]
    xs = [0.35 + i * 0.05 for i in range(60)]
    ax.plot(xs, [b0 * (1 + x) for x in xs], color=GREY, lw=1.2,
            label=r"$r = b(1+\beta)$, b = %.2f Hz" % b0)
    ax.plot(beta, r_brake, "o", color=TEAL, ms=6, label="v4 compact runs")
    cb = [float(r["beta"]) for r in ceiling]
    cr = [float(r["lead_brake_r"]) for r in ceiling]
    ax.plot(cb, cr, "s", mfc="none", mec=ORANGE, ms=8, mew=1.6,
            label="all-scheme working point")
    ax.axhline(r_max, color="#B22222", ls="--", lw=1.2)
    ax.annotate(r"$r_{max}$ = 10 Hz", (2.15, r_max), xytext=(2.15, 10.35),
                fontsize=8, color="#B22222")
    for x, y in zip(cb, cr):
        if y >= r_max - 1e-6:
            ax.annotate("clipped", (x, y), xytext=(x - 0.75, y - 0.9),
                        fontsize=8, color=ORANGE)
    ax.set_xlabel(r"$\beta$")
    ax.set_ylabel("braking-window rate r (Hz)")
    ax.set_title("(a) rate law holds exactly")
    ax.set_ylim(0, 12.2)
    ax.legend(fontsize=7.5, loc="lower right")

    # (b) delay vs beta
    ax = axes[1]
    ax.plot(beta, d_info, "o-", color=TEAL, ms=6)
    ax.axhline(100.5, color=GREY, ls=":", lw=1.2)
    ax.annotate("100 ms detection + 0.5 ms frame", (0.55, 101.6), fontsize=8, color="#555555")
    for i, (x, y) in enumerate(zip(beta, d_info)):
        off = (10, -4) if i == 0 else (0, -14)
        ax.annotate("%.1f" % y, (x, y), xytext=off, textcoords="offset points",
                    ha="left" if i == 0 else "center", fontsize=8)
    ax.set_xlabel(r"$\beta$")
    ax.set_ylabel("D_info from true onset (ms)")
    ax.set_title(r"(b) delay floors at $\beta \geq 1$")
    ax.set_ylim(90, 145)

    # (c) clipping fold line
    ax = axes[2]
    b = [0.6 + i * 0.02 for i in range(470)]
    for beta_v, color in ((1.0, GREY), (2.0, TEAL), (3.0, ORANGE)):
        ax.plot(b, [min(1 + beta_v, r_max / x) for x in b], color=color, lw=1.6,
                label=r"$\beta$ = %g" % beta_v)
        fold = r_max / (1 + beta_v)
        ax.plot([fold], [1 + beta_v], "o", color=color, ms=4)
    ax.plot([float(r["lead_b"]) for r in ceiling],
            [float(r["lead_brake_r"]) / float(r["lead_b"]) for r in ceiling],
            "s", mfc="none", mec="#B22222", ms=8, mew=1.6, label="measured (all-scheme)")
    ax.axvspan(2.9, 3.25, color="#EEF3F5", zorder=0)
    ax.annotate("working point\nb ≈ 3.07 Hz", (3.07, 1.15), fontsize=7.5,
                ha="center", color="#33475B")
    ax.set_xlabel("base rate b (Hz)")
    ax.set_ylabel("effective multiplier  min(1+β, r_max/b)")
    ax.set_title(r"(c) anything past $b = r_{max}/(1+\beta)$ is clipped")
    ax.set_ylim(0.9, 4.6)
    ax.set_xlim(2.0, 10.0)
    ax.legend(fontsize=7.5, loc="upper right")

    fig.tight_layout()
    path = OUT / "fig5_beta_sweep.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def fig6_v4():
    # REPORT_20261124.md §5.5: per-seed D_info at event onset, v4, 6 runs
    g0 = [434.2, 123.6, 176.5]
    g2 = [148.9, 100.6, 100.4]
    mean_g0, mean_g2 = 244.8, 116.6

    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.0), dpi=200,
                             gridspec_kw={"width_ratios": [1.55, 1]})

    ax = axes[0]
    for x, vals, color, label, mean in ((0, g0, GREY, "G0", mean_g0),
                                        (1, g2, TEAL, "G2", mean_g2)):
        ax.bar(x, mean, width=0.55, color=color, alpha=0.35)
        ax.plot([x] * len(vals), vals, "o", color=color, ms=7)
        ax.annotate("%.1f ms" % mean, (x, max(vals)), xytext=(0, 13),
                    textcoords="offset points", ha="center", fontsize=10,
                    fontweight="bold", color=color)
    ax.set_xticks([0, 1], ["G0 (CBR only)", "G2 (braking weight)"])
    ax.set_ylabel("D_info from true onset (ms)")
    ax.set_title("v4: per-seed runs, background fixed at 10 Hz")
    ax.set_ylim(0, 490)
    ax.text(0.03, 0.05, "CBR 0.423 · delivery 96% · −52%",
            transform=ax.transAxes, fontsize=8.5, va="bottom", color="#33475B")

    ax = axes[1]
    ax.bar([0, 1], [99.0, 96.0], width=0.5, color=[GREY, TEAL], alpha=0.8)
    for x, v in ((0, 99.0), (1, 96.0)):
        ax.annotate("%.0f%%" % v, (x, v), xytext=(0, 4), textcoords="offset points",
                    ha="center", fontsize=9)
    ax.set_xticks([0, 1], ["v3.2", "v4"])
    ax.set_ylim(90, 101)
    ax.set_ylabel("delivery ratio (%)")
    ax.set_title("cost of a real load")

    fig.tight_layout()
    path = OUT / "fig6_v4_deployment.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def fig7_delay_split():
    # REPORT_20261124.md §5.3: event-onset D_info and the detection-instant
    # variant; the difference is the wait after the brake flag is set.
    schemes = ["G0", "G1", "G2", "G3"]
    wait = [61.8, 172.5, 0.4, 0.4]
    detect = [100.0, 100.0, 100.0, 100.0]
    fig, ax = plt.subplots(figsize=(8.6, 4.2), dpi=200)
    xs = range(len(schemes))
    ax.bar(xs, detect, width=0.55, color="#D8E0E8")
    ax.bar(xs, wait, width=0.55, bottom=detect, color=TEAL)
    for i, (d, w) in enumerate(zip(detect, wait)):
        ax.annotate("%.1f ms" % (d + w), (i, d + w), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=9)
        if w > 10:
            ax.annotate("%.1f" % w, (i, d + w / 2), ha="center", va="center",
                        fontsize=8.5, color="white")
    ax.set_xticks(list(xs), schemes)
    ax.set_ylabel("D_info from true onset (ms)")
    ax.set_title("Where the delay sits: same detection, different wait")
    ax.set_ylim(0, 310)
    handles = [
        plt.Rectangle((0, 0), 1, 1, color="#D8E0E8", label="brake detection (~100 ms, all schemes)"),
        plt.Rectangle((0, 0), 1, 1, color=TEAL, label="wait for a send slot"),
    ]
    ax.legend(handles=handles, fontsize=8, loc="upper right")

    fig.tight_layout()
    path = OUT / "fig7_delay_split.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    compact, ceiling = read_sweep()
    for path in (fig5_beta(compact, ceiling), fig6_v4(), fig7_delay_split()):
        print("wrote", path, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
