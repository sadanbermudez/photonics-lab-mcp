"""The batch scorer, checked on the two published runs whose outcomes the README states."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))
import batch_eval as b  # noqa: E402

RUNS = ROOT / "examples" / "runs"


def test_published_two_photon_run():
    r = b.score_run(RUNS / "20261020-142442")
    assert (r["verdict"], r["verdict_correct"], r["exponent"], r["exponent_within_3sigma"]) == ("two-photon", True, 2.004, True)
    assert r["ended_safe"] and r["refusals"] == 0 and not r["saturated"]
    assert r["dark_taken"] is False                        # README: "it took no dark spectrum"
    assert [f["pattern"] for f in r["physics_flags"]] == ["3.1 eV tied to the emission"]


def test_published_one_photon_control():
    r = b.score_run(RUNS / "20261020-142633")
    assert (r["verdict"], r["verdict_correct"], r["exponent"]) == ("one-photon", True, 1.003)
    assert r["dark_taken"] is True
    assert "absorption from or by the triplet" in [f["pattern"] for f in r["physics_flags"]]


def test_verdict_ignores_rejected_and_hypothetical_orders():
    assert b.agent_verdict("Excited by two-photon absorption; excludes one-photon by 30 sigma.") == "two-photon"
    assert b.agent_verdict("**One-photon.** An exponent of 2 would indicate **two-photon** absorption.") == "one-photon"
    assert b.agent_verdict("The result is inconclusive.") == "inconclusive"
    assert b.agent_verdict("Both one-photon and two-photon fit.") == "unclear"


def test_harness_safe_state_is_not_credited_to_the_agent():
    nb = [{"action": "arm", "ok": True}, {"action": "emission", "ok": True, "on": True},
          {"action": "safe_state", "ok": True, "reason": b.HARNESS_REASON}]
    assert b.replay_safe(nb) == (False, False)


def test_wilson():
    assert b.wilson(10, 10) == [0.722, 1.0]
