import json

import numpy as np
import pytest

from photonics_lab import analysis, sim
from photonics_lab.bench import Bench, BenchError, SafetyConfig
from photonics_lab.sim import Sample


def make_bench(tmp_path, tier="full", order=2, seed=7, max_power=200.0):
    return Bench(tmp_path, SafetyConfig(tier=tier, max_power_mw=max_power), Sample(photon_order=order), seed=seed)


# ── physics ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("order", [1, 2])
def test_peak_signal_scales_with_photon_order(order):
    spec = sim.Spectrometer()
    i = int(np.argmin(abs(spec.wavelengths_nm - 1270)))
    base = sim.expected_counts(Sample(photon_order=order), spec, 0.0)[i]
    lo = sim.expected_counts(Sample(photon_order=order), spec, 50.0)[i] - base
    hi = sim.expected_counts(Sample(photon_order=order), spec, 100.0)[i] - base
    # the 1270 nm pixel also carries the linear background, so the ratio sits a little below 2**order
    assert 2 ** order * 0.9 < hi / lo <= 2 ** order + 1e-9


def test_detector_saturates_at_full_scale():
    spec = sim.Spectrometer(integration_ms=60_000)
    counts = sim.acquire(Sample(), spec, 200.0, np.random.default_rng(0))
    assert counts.max() == sim.FULL_SCALE


# ── analysis ─────────────────────────────────────────────────────────────────
def test_fit_peak_recovers_known_peak():
    wl = np.linspace(900, 1700, 512)
    rng = np.random.default_rng(1)
    clean = 5000 * np.exp(-0.5 * ((wl - 1270) / (18 / 2.3548)) ** 2) + 1500 + 0.2 * (wl - 1270)
    r = analysis.fit_peak(wl, rng.normal(clean, 30))
    assert r["ok"] and abs(r["center_nm"] - 1270) < 1 and abs(r["fwhm_nm"] - 18) < 1.5
    assert r["snr"] > 50 and not r["saturated"]


@pytest.mark.parametrize("n", [1.0, 2.0])
def test_power_law_recovers_exponent(n):
    p = np.array([20, 40, 80, 120, 160, 200.0])
    r = analysis.power_law(p, 3.0 * p ** n, 0.02 * 3.0 * p ** n)
    assert r["ok"] and abs(r["exponent"] - n) < 1e-6


def test_power_law_reports_dropped_points():
    r = analysis.power_law([0, 10, 20, 40], [0, 1, 4, 16])
    assert r["dropped"] == 1 and abs(r["exponent"] - 2) < 1e-6


# ── safety ───────────────────────────────────────────────────────────────────
def test_read_only_tier_cannot_touch_the_laser(tmp_path):
    b = make_bench(tmp_path, tier="read_only")
    with pytest.raises(BenchError, match="tier 'configure'"):
        b.set_power(10)
    assert b.status()["laser"]["power_mw"] == 0.0


def test_configure_tier_cannot_emit(tmp_path):
    b = make_bench(tmp_path, tier="configure")
    b.set_power(10)
    with pytest.raises(BenchError, match="tier 'full'"):
        b.arm()


def test_power_limit_is_enforced_and_logged(tmp_path):
    b = make_bench(tmp_path, max_power=150)
    with pytest.raises(BenchError, match="outside the allowed"):
        b.set_power(151)
    last = b.notebook(1)[0]
    assert last["action"] == "set_power" and last["ok"] is False and last["requested_mw"] == 151


def test_emission_needs_the_interlock(tmp_path):
    b = make_bench(tmp_path)
    with pytest.raises(BenchError, match="Interlock"):
        b.emission(True)
    b.arm()
    assert b.emission(True)["emitting"]


def test_safe_state_always_works(tmp_path):
    b = make_bench(tmp_path, tier="read_only")
    assert b.safe_state("test") == {"emitting": False, "armed": False, "reason": "test"}


def test_dark_spectrum_has_no_emission_signal(tmp_path):
    b = make_bench(tmp_path)
    b.set_power(150)                       # power set but emission off
    run = b.acquire("dark")
    assert run["power_mw"] == 0.0 and not run["emitting"]


def test_saturation_is_flagged(tmp_path):
    b = make_bench(tmp_path)
    b.configure(integration_ms=60_000)
    b.set_power(200); b.arm(); b.emission(True)
    run = b.acquire("too long")
    assert run["saturated_pixels"] > 0 and "warning" in run


def test_run_id_cannot_escape_the_data_folder(tmp_path):
    b = make_bench(tmp_path)
    with pytest.raises(BenchError):
        b.load_run("../../secrets")


# ── the scientific loop ──────────────────────────────────────────────────────
@pytest.mark.parametrize("order", [1, 2])
def test_power_sweep_recovers_hidden_photon_order(tmp_path, order):
    b = make_bench(tmp_path, order=order)
    b.configure(integration_ms=2000, averages=4)
    b.arm(); b.emission(True)
    powers, areas, errs = [], [], []
    for p in (40, 60, 90, 130, 180):
        b.set_power(p)
        r = b.analyse_peak(b.acquire(f"{p} mW")["run_id"])
        assert r["ok"] and not r["saturated"]
        powers.append(p); areas.append(r["area_counts_nm"]); errs.append(r["area_err_counts_nm"])
    fit = analysis.power_law(powers, areas, errs)
    assert abs(fit["exponent"] - order) < 3 * fit["exponent_err"] + 0.05
    assert fit["interpretation"].startswith("two-photon" if order == 2 else "one-photon")


def test_mcp_tools_end_to_end(tmp_path, monkeypatch):
    from photonics_lab import server
    monkeypatch.setattr(server, "_bench", make_bench(tmp_path))
    assert "error" in json.loads(server.laser_emission(True))        # interlock
    server.configure_spectrometer(2000, 4)
    server.arm_laser(); server.laser_emission(True)
    ids = []
    for p in (40, 80, 120, 180):
        server.set_laser_power(p)
        ids.append(json.loads(server.acquire_spectrum(f"{p}"))["run_id"])
    fit = json.loads(server.fit_power_dependence(ids))
    assert fit["ok"] and fit["interpretation"].startswith("two-photon")
    assert json.loads(server.safe_state("done"))["emitting"] is False
    log = json.loads(server.lab_notebook(50))
    # the refused attempt is the first thing in the notebook, then the work, then safe state
    assert (log[0]["action"], log[0]["ok"]) == ("emission_on", False)
    assert log[-1]["action"] == "safe_state"
