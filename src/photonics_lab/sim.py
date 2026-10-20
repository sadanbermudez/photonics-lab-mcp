"""Physics simulator for a singlet-oxygen photosensitisation bench.

The bench: a femtosecond laser at 800 nm excites a photosensitiser in solution. The
sensitiser's triplet state transfers energy to dissolved oxygen, which then emits a weak
phosphorescence band at 1270 nm. A cooled InGaAs spectrometer records the near-infrared
spectrum.

At 800 nm the sensitiser is excited by TWO-photon absorption, so the emitted signal scales
with the square of laser power. That scaling, not the spectrum's shape, is what tells
one-photon from two-photon excitation, and it is the question the agent demo asks.

Everything that makes a real measurement hard is in the model, because an agent that only
works on clean data is not doing science:
- Poisson shot noise on signal, background and dark current, plus Gaussian read noise
- detector bias offset, dark current that grows with integration time, 16-bit saturation
- a sensitiser fluorescence tail at the blue edge of the detector
- a weak background that scales LINEARLY with power (filter and solvent luminescence), so a
  careless analysis that integrates the whole band instead of fitting the 1270 nm peak
  reports a slope below 2.

All numbers are typical published values for such a bench, not measurements of one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Detector: 512-pixel thermoelectrically cooled InGaAs array, 900-1700 nm
N_PIXELS = 512
WL_MIN_NM, WL_MAX_NM = 900.0, 1700.0
BIAS_COUNTS = 1000.0            # electronic offset added to every pixel
DARK_COUNTS_PER_S = 800.0       # per pixel at -10 C
READ_NOISE_COUNTS = 35.0        # rms per read
FULL_SCALE = 65535              # 16-bit ADC


@dataclass
class Sample:
    """What is in the cuvette. `photon_order` is the hidden truth the agent must infer."""
    photon_order: int = 2                  # 2 = two-photon excitation at 800 nm
    o2_center_nm: float = 1270.0           # singlet oxygen phosphorescence
    o2_fwhm_nm: float = 18.0
    # peak count rate (counts/s per pixel) at 100 mW: weak, as singlet oxygen always is
    o2_rate_at_100mw: float = 2200.0
    fluor_rate_at_100mw: float = 9000.0    # sensitiser fluorescence, tail into the blue edge
    fluor_decay_nm: float = 45.0
    linear_bg_rate_at_100mw: float = 160.0  # flat background that scales with power^1


@dataclass
class Spectrometer:
    integration_ms: float = 1000.0
    averages: int = 1
    wavelengths_nm: np.ndarray = field(default_factory=lambda: np.linspace(WL_MIN_NM, WL_MAX_NM, N_PIXELS))

    # quantum efficiency: InGaAs is flat-ish from 1000-1600 nm and rolls off at both edges
    def quantum_efficiency(self) -> np.ndarray:
        wl = self.wavelengths_nm
        blue = 1.0 / (1.0 + np.exp(-(wl - 960.0) / 18.0))
        red = 1.0 / (1.0 + np.exp((wl - 1640.0) / 15.0))
        return 0.82 * blue * red


def expected_counts(sample: Sample, spec: Spectrometer, power_mw: float) -> np.ndarray:
    """Noise-free mean counts per pixel for one read (bias and dark included)."""
    wl = spec.wavelengths_nm
    t_s = spec.integration_ms / 1000.0
    p = max(power_mw, 0.0) / 100.0
    excite = p ** sample.photon_order              # signal from the excited sensitiser
    sigma = sample.o2_fwhm_nm / (2.0 * np.sqrt(2.0 * np.log(2.0)))
    o2 = sample.o2_rate_at_100mw * excite * np.exp(-0.5 * ((wl - sample.o2_center_nm) / sigma) ** 2)
    fluor = sample.fluor_rate_at_100mw * excite * np.exp(-(wl - WL_MIN_NM) / sample.fluor_decay_nm)
    linear_bg = sample.linear_bg_rate_at_100mw * p * np.ones_like(wl)
    photons = (o2 + fluor + linear_bg) * spec.quantum_efficiency()
    return BIAS_COUNTS + (photons + DARK_COUNTS_PER_S) * t_s


def acquire(sample: Sample, spec: Spectrometer, power_mw: float, rng: np.random.Generator) -> np.ndarray:
    """One measured spectrum: the mean of `averages` reads, each with shot and read noise,
    clipped at the ADC's full scale as a real detector would be."""
    mean = expected_counts(sample, spec, power_mw)
    signal_electrons = np.clip(mean - BIAS_COUNTS, 0.0, None)
    reads = []
    for _ in range(max(1, int(spec.averages))):
        shot = rng.poisson(signal_electrons).astype(float)
        read = rng.normal(0.0, READ_NOISE_COUNTS, size=shot.shape)
        reads.append(np.clip(shot + read + BIAS_COUNTS, 0, FULL_SCALE))
    return np.mean(reads, axis=0)
