"""The bench an agent operates: a laser and a spectrometer behind a safety layer.

Safety lives here, in code, not in the agent's instructions, because an agent can be
wrong or be talked into things:
- permission tiers: read_only < configure < full. Each action needs a minimum tier.
- a hard laser power limit, below the hardware maximum, checked on every change.
- an interlock: emission needs an explicit arm step, and any fault disarms it.
- safe_state(): laser off and disarmed, callable at any tier.
- every action, allowed or refused, goes to an append-only lab notebook (JSONL).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import analysis, sim

TIERS = {"read_only": 0, "configure": 1, "full": 2}
HARDWARE_MAX_MW = 500.0


class BenchError(Exception):
    """A refused or impossible action. The message is written for the agent to act on."""


@dataclass
class SafetyConfig:
    tier: str = "full"
    max_power_mw: float = 200.0
    max_integration_ms: float = 60_000.0
    max_averages: int = 100


@dataclass
class Bench:
    data_dir: Path
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    sample: sim.Sample = field(default_factory=sim.Sample)
    seed: int | None = None

    def __post_init__(self):
        if self.safety.tier not in TIERS:
            raise ValueError(f"tier must be one of {list(TIERS)}")
        if not 0 < self.safety.max_power_mw <= HARDWARE_MAX_MW:
            raise ValueError(f"max_power_mw must be in (0, {HARDWARE_MAX_MW}]")
        self.data_dir = Path(self.data_dir)
        (self.data_dir / "runs").mkdir(parents=True, exist_ok=True)
        self.notebook_path = self.data_dir / "notebook.jsonl"
        self.spectrometer = sim.Spectrometer()
        self.power_mw = 0.0
        self.armed = False
        self.emitting = False
        self.rng = np.random.default_rng(self.seed)

    # ── bookkeeping ──────────────────────────────────────────────────────────
    def _log(self, action: str, ok: bool, **details) -> None:
        entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, "ok": ok, **details}
        with open(self.notebook_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _require(self, action: str, tier: str) -> None:
        if TIERS[self.safety.tier] < TIERS[tier]:
            self._log(action, False, refused=f"needs tier '{tier}', bench is '{self.safety.tier}'")
            raise BenchError(f"{action} needs permission tier '{tier}'; this bench runs at "
                             f"'{self.safety.tier}'. Ask the operator to raise it.")

    def status(self) -> dict:
        s = self.spectrometer
        return {
            "laser": {"wavelength_nm": 800.0, "power_mw": self.power_mw, "armed": self.armed,
                      "emitting": self.emitting},
            "spectrometer": {"range_nm": [sim.WL_MIN_NM, sim.WL_MAX_NM], "pixels": sim.N_PIXELS,
                             "integration_ms": s.integration_ms, "averages": s.averages,
                             "full_scale_counts": sim.FULL_SCALE},
            "safety": {"tier": self.safety.tier, "max_power_mw": self.safety.max_power_mw,
                       "max_integration_ms": self.safety.max_integration_ms},
            "sample": "photosensitiser in air-saturated solution; looking for singlet oxygen near 1270 nm",
        }

    # ── laser ────────────────────────────────────────────────────────────────
    def set_power(self, power_mw: float) -> dict:
        self._require("set_power", "configure")
        p = float(power_mw)
        if not 0.0 <= p <= self.safety.max_power_mw:
            self._log("set_power", False, requested_mw=p, limit_mw=self.safety.max_power_mw)
            raise BenchError(f"{p} mW is outside the allowed 0 to {self.safety.max_power_mw} mW. "
                             "The limit is a safety setting the agent cannot change.")
        self.power_mw = p
        self._log("set_power", True, power_mw=p)
        return {"power_mw": p, "emitting": self.emitting}

    def arm(self) -> dict:
        self._require("arm", "full")
        self.armed = True
        self._log("arm", True)
        return {"armed": True, "note": "laser armed; emission starts only with emission(on=True)"}

    def emission(self, on: bool) -> dict:
        if on:
            self._require("emission_on", "full")
            if not self.armed:
                self._log("emission_on", False, refused="not armed")
                raise BenchError("Interlock: arm the laser first (arm), then switch emission on.")
            self.emitting = True
        else:
            self.emitting = False          # switching off is always allowed
        self._log("emission", True, on=bool(on), power_mw=self.power_mw)
        return {"emitting": self.emitting, "power_mw": self.power_mw}

    def safe_state(self, reason: str = "requested") -> dict:
        """Laser off and disarmed. Allowed at every tier, never refused."""
        self.emitting = False
        self.armed = False
        self._log("safe_state", True, reason=reason)
        return {"emitting": False, "armed": False, "reason": reason}

    # ── spectrometer ─────────────────────────────────────────────────────────
    def configure(self, integration_ms: float | None = None, averages: int | None = None) -> dict:
        self._require("configure", "configure")
        s = self.spectrometer
        if integration_ms is not None:
            t = float(integration_ms)
            if not 1.0 <= t <= self.safety.max_integration_ms:
                self._log("configure", False, requested_integration_ms=t)
                raise BenchError(f"integration_ms must be 1 to {self.safety.max_integration_ms}")
            s.integration_ms = t
        if averages is not None:
            a = int(averages)
            if not 1 <= a <= self.safety.max_averages:
                self._log("configure", False, requested_averages=a)
                raise BenchError(f"averages must be 1 to {self.safety.max_averages}")
            s.averages = a
        self._log("configure", True, integration_ms=s.integration_ms, averages=s.averages)
        return {"integration_ms": s.integration_ms, "averages": s.averages}

    def acquire(self, label: str = "") -> dict:
        """Take one spectrum. With emission off this is a dark/background spectrum, which is a
        legitimate measurement, so it needs only the configure tier."""
        self._require("acquire", "configure")
        effective_power = self.power_mw if self.emitting else 0.0
        counts = sim.acquire(self.sample, self.spectrometer, effective_power, self.rng)
        wl = self.spectrometer.wavelengths_nm
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        meta = {"run_id": run_id, "label": label, "power_mw": effective_power, "emitting": self.emitting,
                "integration_ms": self.spectrometer.integration_ms, "averages": self.spectrometer.averages}
        np.savetxt(self.data_dir / "runs" / f"{run_id}.csv", np.c_[wl, counts], delimiter=",",
                   header="wavelength_nm,counts", comments="", fmt="%.3f")
        (self.data_dir / "runs" / f"{run_id}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        saturated = int(np.sum(counts >= sim.FULL_SCALE - 1))
        peak_i = int(np.argmax(counts))
        summary = {**meta, "max_counts": round(float(counts.max()), 1),
                   "max_at_nm": round(float(wl[peak_i]), 1), "median_counts": round(float(np.median(counts)), 1),
                   "saturated_pixels": saturated}
        if saturated:
            summary["warning"] = "detector saturated: lower the power or the integration time"
        self._log("acquire", True, **{k: summary[k] for k in ("run_id", "label", "power_mw", "integration_ms",
                                                              "averages", "max_counts", "saturated_pixels")})
        return summary

    def load_run(self, run_id: str) -> tuple[np.ndarray, np.ndarray, dict]:
        safe = "".join(c for c in run_id if c.isalnum() or c == "-")
        csv = self.data_dir / "runs" / f"{safe}.csv"
        if not csv.exists():
            raise BenchError(f"no run '{run_id}'")
        data = np.loadtxt(csv, delimiter=",", skiprows=1)
        meta = json.loads((self.data_dir / "runs" / f"{safe}.json").read_text(encoding="utf-8"))
        return data[:, 0], data[:, 1], meta

    def analyse_peak(self, run_id: str, center_nm: float = 1270.0, window_nm: float = 90.0) -> dict:
        wl, counts, meta = self.load_run(run_id)
        result = analysis.fit_peak(wl, counts, center_nm, window_nm)
        self._log("analyse_peak", bool(result.get("ok")), run_id=run_id,
                  **{k: result.get(k) for k in ("center_nm", "fwhm_nm", "area_counts_nm", "snr", "saturated")})
        return {"run_id": run_id, "power_mw": meta["power_mw"], **result}

    def notebook(self, last: int = 20) -> list[dict]:
        if not self.notebook_path.exists():
            return []
        lines = self.notebook_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines[-max(1, min(int(last), 200)):]]
