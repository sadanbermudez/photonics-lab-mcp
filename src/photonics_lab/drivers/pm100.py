"""SCPI driver for a laser power meter of the Thorlabs PM100 family, over PyVISA.

This is the real-hardware path: the same commands work on a physical meter over USB
(resource like "USB0::0x1313::0x8078::P0012345::INSTR"). The tests drive it against
pyvisa-sim, which answers on the wire like an instrument, so every command and every
parse below is exercised without hardware.

SCPI used (PM100 programming reference): *IDN?, SENS:CORR:WAV <nm> and ?, MEAS:POW? (watts).
"""
from __future__ import annotations


class PowerMeterError(Exception):
    pass


class PM100:
    WAVELENGTH_RANGE_NM = (400.0, 1100.0)   # silicon sensor head, covers the 800 nm laser

    def __init__(self, resource: str, visa_library: str = ""):
        import pyvisa
        self.rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"
        self.inst.timeout = 3000

    def close(self) -> None:
        self.inst.close()

    def identify(self) -> dict:
        maker, model, serial, firmware = (s.strip() for s in self.inst.query("*IDN?").split(",", 3))
        return {"maker": maker, "model": model, "serial": serial, "firmware": firmware}

    def set_wavelength(self, nm: float) -> float:
        lo, hi = self.WAVELENGTH_RANGE_NM
        if not lo <= nm <= hi:
            raise PowerMeterError(f"{nm} nm is outside the sensor's {lo:.0f}-{hi:.0f} nm range")
        self.inst.write(f"SENS:CORR:WAV {nm:g}")
        readback = float(self.inst.query("SENS:CORR:WAV?"))
        if abs(readback - nm) > 0.5:
            raise PowerMeterError(f"meter reports {readback} nm after setting {nm} nm")
        return readback

    def read_power_mw(self) -> float:
        watts = float(self.inst.query("MEAS:POW?"))
        if watts < 0:
            raise PowerMeterError(f"negative power reading {watts} W: check the zero offset")
        return watts * 1000.0
