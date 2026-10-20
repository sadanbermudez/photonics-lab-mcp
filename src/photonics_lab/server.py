"""MCP server exposing the photonics bench to an AI agent.

Run:  python -m photonics_lab.server                       (stdio, for Claude Code)
      python -m photonics_lab.server --http --port 8771    (local HTTP, secret path)
Options: --data-dir, --tier read_only|configure|full, --max-power-mw, --seed,
         --photon-order (the simulated sample's hidden truth; for tests and demos)
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import analysis
from .bench import Bench, BenchError, SafetyConfig
from .sim import Sample

DEFAULT_DATA = Path(os.environ.get("PHOTONICS_LAB_DATA", Path.home() / "photonics-lab-data"))

mcp = FastMCP("photonics-lab", instructions=(
    "Controls an optics bench: an 800 nm femtosecond laser exciting a photosensitiser, and an "
    "InGaAs spectrometer (900-1700 nm) looking for singlet-oxygen emission near 1270 nm. "
    "Work like an experimentalist: check bench_status first; take a dark spectrum with emission "
    "off; then arm_laser and laser_emission(on=True) to measure. Keep the detector below "
    "saturation (acquire_spectrum warns you). Quantify with analyse_peak, not by eye. To find "
    "how the signal scales with power, measure several powers over a wide range and call "
    "fit_power_dependence. Always finish with safe_state. Safety limits are enforced by the "
    "bench and cannot be changed from here; a refusal message says what to do instead."))

_bench: Bench | None = None


def bench() -> Bench:
    global _bench
    if _bench is None:
        _bench = Bench(DEFAULT_DATA)
    return _bench


def _call(fn, *args, **kwargs) -> str:
    """Run a bench action; a refusal becomes a readable error the agent can act on, and any
    unexpected failure puts the bench in its safe state before reporting."""
    try:
        return json.dumps(fn(*args, **kwargs), indent=1)
    except BenchError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:  # noqa: BLE001 - never leave a laser on after a crash
        bench().safe_state(reason=f"fault: {type(e).__name__}")
        return json.dumps({"error": f"{type(e).__name__}: {e}", "bench": "moved to safe state"})


@mcp.tool()
def bench_status() -> str:
    """Current laser, spectrometer and safety settings. Call this first."""
    return _call(bench().status)


@mcp.tool()
def set_laser_power(power_mw: float) -> str:
    """Set the laser output power in milliwatts (0 to the bench's safety limit)."""
    return _call(bench().set_power, power_mw)


@mcp.tool()
def arm_laser() -> str:
    """Arm the laser interlock. Required before emission can be switched on."""
    return _call(bench().arm)


@mcp.tool()
def laser_emission(on: bool) -> str:
    """Switch laser emission on or off. On requires arm_laser first; off always works."""
    return _call(bench().emission, on)


@mcp.tool()
def safe_state(reason: str = "requested") -> str:
    """Laser off and disarmed. Always allowed. Call it when finished or if anything looks wrong."""
    return _call(bench().safe_state, reason)


@mcp.tool()
def configure_spectrometer(integration_ms: float | None = None, averages: int | None = None) -> str:
    """Set integration time (ms) and the number of reads averaged per spectrum."""
    return _call(bench().configure, integration_ms, averages)


@mcp.tool()
def acquire_spectrum(label: str = "") -> str:
    """Record one spectrum and save it. Returns a run_id and a summary (max counts, where,
    saturation). With emission off this is a dark spectrum."""
    return _call(bench().acquire, label)


@mcp.tool()
def analyse_peak(run_id: str, center_nm: float = 1270.0, window_nm: float = 90.0) -> str:
    """Fit the emission peak of a saved run: centre, FWHM, integrated area with uncertainty,
    signal-to-noise, and a saturation flag."""
    return _call(bench().analyse_peak, run_id, center_nm, window_nm)


@mcp.tool()
def fit_power_dependence(run_ids: list[str], center_nm: float = 1270.0) -> str:
    """Fit peak area against laser power (log-log) for several runs. The exponent says how
    many photons excite the emission: about 1 for one-photon, about 2 for two-photon."""
    def run():
        rows = [bench().analyse_peak(r, center_nm) for r in run_ids]
        bad = [r["run_id"] for r in rows if not r.get("ok") or r.get("saturated") or r["power_mw"] <= 0]
        good = [r for r in rows if r["run_id"] not in bad]
        fit = analysis.power_law([r["power_mw"] for r in good], [r["area_counts_nm"] for r in good],
                                 [r["area_err_counts_nm"] for r in good])
        fit["excluded_runs"] = bad
        fit["table"] = [{k: r.get(k) for k in ("run_id", "power_mw", "area_counts_nm", "area_err_counts_nm", "snr")}
                        for r in good]
        bench()._log("fit_power_dependence", bool(fit.get("ok")), exponent=fit.get("exponent"),
                     exponent_err=fit.get("exponent_err"), runs=len(good), excluded=bad)
        return fit
    return _call(run)


@mcp.tool()
def lab_notebook(last: int = 20) -> str:
    """The most recent lab notebook entries: every action, including refused ones."""
    return _call(bench().notebook, last)


def main() -> None:
    global _bench
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA))
    ap.add_argument("--tier", default="full", choices=["read_only", "configure", "full"])
    ap.add_argument("--max-power-mw", type=float, default=200.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--photon-order", type=int, default=2, choices=[1, 2])
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--port", type=int, default=8771)
    a = ap.parse_args()
    _bench = Bench(Path(a.data_dir), SafetyConfig(tier=a.tier, max_power_mw=a.max_power_mw),
                   Sample(photon_order=a.photon_order), seed=a.seed)
    if not a.http:
        mcp.run()
        return
    secret_file = Path(a.data_dir) / ".server-secret"
    if not secret_file.exists():
        secret_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = a.port
    mcp.settings.streamable_http_path = f"/mcp-{secret_file.read_text(encoding='utf-8').strip()}"
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
