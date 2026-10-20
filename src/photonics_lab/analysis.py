"""Analysis an experimentalist would do by hand: fit the emission peak on a sloped baseline,
quote uncertainties, and fit signal against excitation power on log-log axes."""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import curve_fit

FWHM_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))


def _gauss_on_line(x, amp, center, sigma, c0, c1):
    return amp * np.exp(-0.5 * ((x - center) / sigma) ** 2) + c0 + c1 * (x - center)


def fit_peak(wl: np.ndarray, counts: np.ndarray, center_nm: float = 1270.0, window_nm: float = 90.0) -> dict:
    """Gaussian on a linear baseline, fitted inside center +/- window.

    Returns the peak centre, FWHM, height and integrated area (counts x nm) with 1-sigma
    uncertainties from the fit covariance, plus a signal-to-noise ratio: area divided by its
    uncertainty. `saturated` flags any pixel at full scale in the window, which would make
    every number here wrong."""
    sel = (wl >= center_nm - window_nm) & (wl <= center_nm + window_nm)
    x, y = wl[sel], counts[sel]
    if x.size < 8:
        return {"ok": False, "error": "window holds fewer than 8 pixels"}
    edge = np.r_[y[: x.size // 6], y[-x.size // 6:]]
    c0 = float(np.median(edge))
    amp0 = float(max(y.max() - c0, 1.0))
    p0 = [amp0, center_nm, 8.0, c0, 0.0]
    bounds = ([0.0, center_nm - window_nm, 1.0, -np.inf, -np.inf],
              [np.inf, center_nm + window_nm, window_nm, np.inf, np.inf])
    try:
        popt, pcov = curve_fit(_gauss_on_line, x, y, p0=p0, bounds=bounds, maxfev=20000)
    except (RuntimeError, ValueError) as e:
        return {"ok": False, "error": f"fit did not converge: {e}"}
    perr = np.sqrt(np.clip(np.diag(pcov), 0.0, None))
    amp, center, sigma = popt[0], popt[1], popt[2]
    area = amp * sigma * math.sqrt(2.0 * math.pi)
    # d(area)/d(amp) and d(area)/d(sigma), with their covariance
    j = np.array([sigma * math.sqrt(2 * math.pi), amp * math.sqrt(2 * math.pi)])
    cov_as = pcov[np.ix_([0, 2], [0, 2])]
    area_err = float(math.sqrt(max(j @ cov_as @ j, 0.0)))
    residual_rms = float(np.std(y - _gauss_on_line(x, *popt)))
    return {
        "ok": True,
        "center_nm": round(float(center), 2), "center_err_nm": round(float(perr[1]), 2),
        "fwhm_nm": round(float(sigma * FWHM_PER_SIGMA), 2), "fwhm_err_nm": round(float(perr[2] * FWHM_PER_SIGMA), 2),
        "height_counts": round(float(amp), 1), "height_err_counts": round(float(perr[0]), 1),
        "area_counts_nm": round(float(area), 1), "area_err_counts_nm": round(area_err, 1),
        "snr": round(float(area / area_err), 1) if area_err > 0 else None,
        "baseline_counts": round(float(popt[3]), 1),
        "residual_rms_counts": round(residual_rms, 1),
        "saturated": bool(np.any(y >= 65535 - 1)),
    }


def power_law(powers_mw, signals, signal_errs=None) -> dict:
    """Fit signal = a * P^n by weighted least squares on log-log axes.

    n near 1 means one-photon excitation, near 2 two-photon. Points with zero or negative
    signal are dropped (their log is undefined) and reported, because silently losing data
    changes the answer."""
    p = np.asarray(powers_mw, float)
    s = np.asarray(signals, float)
    e = np.asarray(signal_errs, float) if signal_errs is not None else np.full_like(s, np.nan)
    keep = (p > 0) & (s > 0)
    dropped = int((~keep).sum())
    p, s, e = p[keep], s[keep], e[keep]
    if p.size < 3:
        return {"ok": False, "error": f"need at least 3 positive points, have {p.size}", "dropped": dropped}
    x, y = np.log(p), np.log(s)
    # error on log(s) is e/s; fall back to equal weights when errors are unknown
    w = np.where(np.isfinite(e) & (e > 0), (s / e) ** 2, 1.0)
    W = np.sum(w)
    xm, ym = np.sum(w * x) / W, np.sum(w * y) / W
    sxx = np.sum(w * (x - xm) ** 2)
    slope = np.sum(w * (x - xm) * (y - ym)) / sxx
    intercept = ym - slope * xm
    resid = y - (intercept + slope * x)
    dof = max(p.size - 2, 1)
    # scale by reduced chi-square so the error reflects the actual scatter
    chi2_red = float(np.sum(w * resid ** 2) / dof)
    slope_err = math.sqrt(chi2_red / sxx) if sxx > 0 else float("nan")
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = 1.0 - np.sum(resid ** 2) / ss_tot if ss_tot > 0 else float("nan")
    return {
        "ok": True, "exponent": round(float(slope), 3), "exponent_err": round(float(slope_err), 3),
        "prefactor": float(math.exp(intercept)), "r2": round(float(r2), 4), "points": int(p.size),
        "dropped": dropped, "interpretation": interpret_exponent(slope, slope_err),
    }


def interpret_exponent(n: float, err: float) -> str:
    """Which photon order the exponent supports, and how strongly."""
    if not math.isfinite(err) or err <= 0:
        err = 0.1
    z1, z2 = abs(n - 1.0) / err, abs(n - 2.0) / err
    if z2 < 2.0 and z1 > 3.0:
        return f"two-photon: exponent {n:.2f} +/- {err:.2f} is consistent with 2 and excludes 1 (by {z1:.0f} sigma)"
    if z1 < 2.0 and z2 > 3.0:
        return f"one-photon: exponent {n:.2f} +/- {err:.2f} is consistent with 1 and excludes 2 (by {z2:.0f} sigma)"
    return (f"inconclusive: exponent {n:.2f} +/- {err:.2f}; add points over a wider power range, "
            "longer integration, or check for saturation and background")
