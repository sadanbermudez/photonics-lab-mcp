"""The PM100 driver against pyvisa-sim: real SCPI over a simulated wire."""
from pathlib import Path

import pytest

from photonics_lab.drivers.pm100 import PM100, PowerMeterError

RESOURCE = "USB0::0x1313::0x8078::P2000123::INSTR"
SIM = f"{Path(__file__).with_name('pm100_sim.yaml')}@sim"


@pytest.fixture
def meter():
    m = PM100(RESOURCE, SIM)
    yield m
    m.close()


def test_identify(meter):
    assert meter.identify() == {"maker": "Thorlabs", "model": "PM100USB", "serial": "P2000123", "firmware": "1.7.0"}


def test_wavelength_round_trip(meter):
    assert meter.set_wavelength(800) == 800.0


def test_wavelength_outside_sensor_range_is_refused_before_sending(meter):
    with pytest.raises(PowerMeterError, match="outside"):
        meter.set_wavelength(1270)


def test_power_reading_in_milliwatts(meter):
    assert meter.read_power_mw() == pytest.approx(123.4)
