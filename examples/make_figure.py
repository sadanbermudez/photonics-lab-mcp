"""Figure from two agent runs: spectra at each power, and peak area against power on log-log
axes with the fitted exponent. Reads only the bench's saved files.

  python examples/make_figure.py <two-photon run dir> <one-photon run dir> --out docs/agent-experiment.png
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from photonics_lab import analysis  # noqa: E402

INK, SOFT, GRID = "#141821", "#5a6273", "#dde1e8"
ACCENT, SECOND = "#4b3fd6", "#c2185b"


def load(run_dir: Path):
    runs = []
    for meta_file in sorted((run_dir / "bench" / "runs").glob("*.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if meta["power_mw"] <= 0:
            continue
        data = np.loadtxt(meta_file.with_suffix(".csv"), delimiter=",", skiprows=1)
        fit = analysis.fit_peak(data[:, 0], data[:, 1])
        runs.append((meta, data, fit))
    runs.sort(key=lambda r: r[0]["power_mw"])
    return runs


def main(a):
    two, one = load(Path(a.two)), load(Path(a.one))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2), dpi=160)
    for ax in (ax1, ax2):
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(colors=SOFT, labelsize=9)
        for s in ax.spines.values():
            s.set_color(GRID)
    shades = np.linspace(0.25, 1.0, len(two))
    for (meta, data, _), alpha in zip(two, shades):
        ax1.plot(data[:, 0], data[:, 1], color=ACCENT, alpha=alpha, lw=1.0, label=f"{meta['power_mw']:.0f} mW")
    ax1.set_xlim(1150, 1400)
    ax1.set_xlabel("Wavelength (nm)", color=INK)
    ax1.set_ylabel("Counts", color=INK)
    ax1.set_title("Spectra the agent recorded (two-photon sample)", color=INK, fontsize=10, loc="left")
    ax1.legend(frameon=False, fontsize=8, labelcolor=SOFT)
    for runs, color, label in ((two, ACCENT, "two-photon sample"), (one, SECOND, "one-photon sample (control)")):
        p = np.array([r[0]["power_mw"] for r in runs])
        area = np.array([r[2]["area_counts_nm"] for r in runs])
        err = np.array([r[2]["area_err_counts_nm"] for r in runs])
        fit = analysis.power_law(p, area, err)
        ax2.errorbar(p, area, yerr=err, fmt="o", ms=4, color=color, capsize=2)
        xs = np.geomspace(p.min() * 0.9, p.max() * 1.1, 50)
        ax2.plot(xs, fit["prefactor"] * xs ** fit["exponent"], color=color, lw=1.2,
                 label=f"{label}: n = {fit['exponent']:.3f} ± {fit['exponent_err']:.3f}")
    ax2.set_xscale("log")
    ax2.set_yscale("log")
    ax2.set_xlabel("Laser power at 800 nm (mW)", color=INK)
    ax2.set_ylabel("1270 nm peak area (counts · nm)", color=INK)
    ax2.set_title("Signal ∝ Pⁿ: the slope gives the photon order", color=INK, fontsize=10, loc="left")
    ax2.legend(frameon=False, fontsize=8, labelcolor=SOFT, loc="upper left")
    fig.tight_layout()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, bbox_inches="tight")
    print("wrote", a.out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("two")
    ap.add_argument("one")
    ap.add_argument("--out", default="docs/agent-experiment.png")
    main(ap.parse_args())
