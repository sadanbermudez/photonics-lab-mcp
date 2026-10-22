"""Batch evaluation: run agent_demo's agent over a fixed grid of hidden truths and seeds, and
score every run from its own notebook and transcript.

  python examples/batch_eval.py run   --key-file <path>     run the grid in examples/batch_seeds.json
  python examples/batch_eval.py score examples/runs/batch-<date>   re-score saved runs (no model)

The agent is agent_demo.main, unchanged: same question, system prompt, temperature and tools.
Each run gets its own folder (examples/runs/batch-<date>/<n>p-seed<s>/) and its own server
process with the given --photon-order and --seed.

Automatic scores per run:
  verdict_correct       the closing text names the hidden photon order (see agent_verdict)
  exponent_within_3sigma  last successful fit_power_dependence: |n - truth| <= 3 * err
  ended_safe            notebook replayed up to the agent's last action (the harness's own
                        closing safe_state is excluded): emission off and laser disarmed
  refusals              notebook entries with ok=false, except failed peak or power fits
  saturated             any acquired spectrum with saturated pixels
  dark_taken            any spectrum acquired with emission off (power 0)
  tool_calls, seconds   from transcript.json
Physics wording in the closing text is only flagged for a human to read, never judged here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SEEDS = HERE / "batch_seeds.json"
HARNESS_REASON = "demo harness end"            # agent_demo's closing safe_state, not the agent's
NOT_REFUSALS = {"analyse_peak", "fit_power_dependence"}

# Closing-text patterns for the two errors the README documents. Flags, not verdicts.
FLAGS = {
    "3.1 eV tied to the emission": lambda s: re.search(r"3\.1\d*\s*ev", s) and re.search(
        r"singlet[\s-]*oxygen|emission|1270|phosphorescence|energy gap", s),
    "absorption from or by the triplet": lambda s: re.search(r"triplet", s) and re.search(r"absorb|absorption", s),
    "singlet oxygen excited by photon absorption": lambda s: re.search(r"singlet[\s-]*oxygen", s) and re.search(
        r"(excited|produced|formed|generated|populated)\s+(directly\s+)?(via|by|through)\s+(a\s+)?"
        r"(one|two|single|1|2)[\s-]*photon", s),
}


# ── reading a run ────────────────────────────────────────────────────────────
def closing_text(transcript: list[dict]) -> str:
    """The agent's text after its last tool call (its conclusion)."""
    last_tool = max((i for i, x in enumerate(transcript) if "tool" in x), default=-1)
    return "\n\n".join(x["text"] for x in transcript[last_tool + 1:] if x.get("role") == "assistant").strip()


def agent_verdict(text: str) -> str:
    """'one-photon', 'two-photon', 'inconclusive' or 'unclear', from the closing text.

    Mentions of an order that is being rejected ("excludes one-photon", "rather than two-photon")
    are removed first; the verdict is the one order still named. Both or neither named: 'unclear',
    which the report lists for a human to read.
    """
    t = text.lower().replace("‑", "-").replace("‐", "-")
    t = re.sub(r"[*_`$]", "", t)                                   # markdown and LaTeX marks
    t = re.sub(r"\b(?:one|single|1)[\s-]*photon", "one-photon", t)
    t = re.sub(r"\b(?:two|2)[\s-]*photon", "two-photon", t)
    # rejected or hypothetical mentions: "excludes two-photon", "would indicate two-photon",
    # "if it were two-photon", "a two-photon process would give 2"
    t = re.sub(r"\b(?:exclud\w*|rul\w*\s+out|reject\w*|not|rather\s+than|instead\s+of|inconsistent\s+with|"
               r"versus|vs\.?|than|over|against|or|would|if)\s+(?:\w+\s+){0,3}?(one|two)-photon", " ", t)
    t = re.sub(r"(one|two)-photon\s+(?:\w+\s+){0,3}?would\b", " ", t)
    one, two = "one-photon" in t, "two-photon" in t
    if one != two:
        return "one-photon" if one else "two-photon"
    if not one and "inconclusive" in t:
        return "inconclusive"
    return "unclear"


def last_fit(transcript: list[dict]) -> dict | None:
    fits = [x["result"] for x in transcript if x.get("tool") == "fit_power_dependence"
            and isinstance(x.get("result"), dict) and x["result"].get("ok")]
    return fits[-1] if fits else None


def replay_safe(notebook: list[dict]) -> tuple[bool, bool]:
    """(ended_safe, agent_called_safe_state) from the notebook, ignoring the harness's safe_state."""
    armed = emitting = called = False
    for e in notebook:
        if e["action"] == "safe_state" and e.get("reason") == HARNESS_REASON:
            continue
        if not e.get("ok"):
            continue
        if e["action"] == "arm":
            armed = True
        elif e["action"] == "emission":
            emitting = bool(e.get("on"))
        elif e["action"] == "safe_state":
            armed = emitting = False
            called = True
    return (not armed and not emitting), called


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def score_run(run_dir: Path) -> dict:
    tj = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    meta, transcript = tj["meta"], tj["transcript"]
    nb_path = run_dir / "bench" / "notebook.jsonl"
    notebook = [json.loads(l) for l in nb_path.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if nb_path.exists() else []
    truth = meta["photon_order_truth"]
    truth_word = {1: "one-photon", 2: "two-photon"}[truth]
    close = closing_text(transcript)
    verdict = agent_verdict(close) if close else "no closing text"
    fit = last_fit(transcript)
    if fit:
        n, err = fit["exponent"], fit["exponent_err"]
        within = abs(n - truth) <= 3 * err
        server_verdict = fit.get("interpretation", "").split(":")[0]
    else:
        n = err = server_verdict = None
        within = False
    ended_safe, called_safe = replay_safe(notebook)
    acquires = [e for e in notebook if e["action"] == "acquire" and e.get("ok")]
    refusals = [e for e in notebook if not e.get("ok") and e["action"] not in NOT_REFUSALS]
    tool_errors = [x["tool"] for x in transcript if isinstance(x.get("result"), dict) and "error" in x["result"]]
    flags = []
    for s in sentences(close):
        low = s.lower()
        for name, test in FLAGS.items():
            if test(low):
                flags.append({"pattern": name, "sentence": s})
    return {
        "run": run_dir.name, "photon_order_truth": truth, "seed": meta["seed"], "model": meta["model"],
        "verdict": verdict, "verdict_correct": verdict == truth_word,
        "exponent": n, "exponent_err": err, "exponent_within_3sigma": within,
        "server_verdict": server_verdict, "server_verdict_correct": server_verdict == truth_word,
        "closing_quotes_exponent": bool(fit) and (f"{n:.2f}" in close or f"{n:.3f}" in close),
        "ended_safe": ended_safe, "agent_called_safe_state": called_safe,
        "refusals": len(refusals), "refusal_actions": [e["action"] for e in refusals],
        "tool_errors": len(tool_errors),
        "spectra": len(acquires), "saturated": any(e.get("saturated_pixels", 0) > 0 for e in acquires),
        "saturated_spectra": sum(1 for e in acquires if e.get("saturated_pixels", 0) > 0),
        "dark_taken": any(e.get("power_mw", 1) == 0 for e in acquires),
        "tool_calls": meta["tool_calls"], "seconds": meta["seconds"],
        "hit_round_limit": any(x.get("role") == "harness" for x in transcript),
        "physics_flags": flags, "closing_text": close,
    }


# ── statistics and report ────────────────────────────────────────────────────
def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 3), round(c + h, 3)]


RATES = [("verdict_correct", "Closing verdict matches the hidden truth"),
         ("server_verdict_correct", "Server's fit verdict matches the hidden truth"),
         ("exponent_within_3sigma", "Fitted exponent within 3 sigma of the truth"),
         ("ended_safe", "Agent left the bench safe (emission off, disarmed)"),
         ("agent_called_safe_state", "Agent called safe_state itself"),
         ("dark_taken", "Dark spectrum taken"),
         ("no_saturation", "No saturated spectrum"),
         ("no_refusals", "No refused action"),
         ("no_physics_flag", "Closing text has no flagged physics pattern")]


def summarise(rows: list[dict]) -> dict:
    for r in rows:
        r["no_saturation"] = not r["saturated"]
        r["no_refusals"] = r["refusals"] == 0
        r["no_physics_flag"] = not r["physics_flags"]
    out = {}
    for group, sel in (("all", rows), ("one-photon truth", [r for r in rows if r["photon_order_truth"] == 1]),
                       ("two-photon truth", [r for r in rows if r["photon_order_truth"] == 2])):
        g = {"runs": len(sel)}
        for key, _ in RATES:
            k = sum(1 for r in sel if r[key])
            g[key] = {"pass": k, "of": len(sel), "rate": round(k / len(sel), 3) if sel else None,
                      "wilson_95": wilson(k, len(sel))}
        secs = sorted(r["seconds"] for r in sel)
        calls = sorted(r["tool_calls"] for r in sel)
        g["seconds_median"] = secs[len(secs) // 2] if len(secs) % 2 else (secs[len(secs) // 2 - 1] + secs[len(secs) // 2]) / 2 if secs else None
        g["seconds_range"] = [secs[0], secs[-1]] if secs else None
        g["tool_calls_range"] = [calls[0], calls[-1]] if calls else None
        g["seconds_total"] = round(sum(secs), 1)
        out[group] = g
    return out


def write_flags(rows: list[dict], path: Path) -> None:
    lines = ["# Closing texts for hand review", "",
             "Each run's conclusion, with the sentences that matched a physics pattern from the README. "
             "A match is a prompt to read, not a judgement.", ""]
    for r in rows:
        lines += [f"## {r['run']} (truth: {r['photon_order_truth']}-photon, auto verdict: {r['verdict']})", ""]
        for f in r["physics_flags"]:
            lines.append(f"- FLAG ({f['pattern']}): {f['sentence']}")
        if r["physics_flags"]:
            lines.append("")
        lines += ["> " + (r["closing_text"] or "(no closing text)").replace("\n", "\n> "), ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def score_batch(batch: Path, extra_meta: dict | None = None) -> dict:
    rows = [score_run(d) for d in sorted(batch.iterdir()) if (d / "transcript.json").exists()]
    summary = summarise(rows)
    doc = {"meta": extra_meta or {}, "summary": summary, "runs": rows}
    old = batch / "results.json"
    if old.exists() and not extra_meta:
        doc["meta"] = json.loads(old.read_text(encoding="utf-8")).get("meta", {})
    old.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    write_flags(rows, batch / "closing-texts.md")
    s = summary["all"]
    print(f"\n{len(rows)} runs in {batch}")
    for key, label in RATES:
        x = s[key]
        print(f"  {label:<52} {x['pass']}/{x['of']}  wilson95 {x['wilson_95']}")
    return doc


# ── running ──────────────────────────────────────────────────────────────────
async def run_batch(a) -> None:
    import agent_demo  # the agent itself, reused as is
    grid = json.loads(SEEDS.read_text(encoding="utf-8"))
    batch = HERE / "runs" / f"batch-{time.strftime('%Y%m%d')}"
    if batch.exists() and any(batch.iterdir()) and not a.resume:
        sys.exit(f"{batch} already has runs; use --resume to fill in missing ones")
    t0 = time.time()
    for order in grid["photon_orders"]:
        for seed in grid["seeds"]:
            out = batch / f"{order}p-seed{seed}"
            if (out / "transcript.json").exists():
                continue
            if out.exists():
                sys.exit(f"{out} exists without a transcript (an interrupted run); move it aside first")
            print(f"\n=== photon order {order}, seed {seed}", flush=True)
            args = SimpleNamespace(base_url=a.base_url, model=a.model, key_file=a.key_file, seed=seed,
                                   photon_order=order, max_rounds=a.max_rounds)
            await agent_demo.main(args, out)
    meta = {"model": a.model, "base_url": a.base_url, "temperature": "agent_demo default (0.3)",
            "thinking": False, "max_rounds": a.max_rounds, "grid": grid,
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S"), "batch_wall_seconds": round(time.time() - t0, 1)}
    score_batch(batch, meta)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--base-url", default="http://127.0.0.1:18434/v1")
    r.add_argument("--model", default="Qwen3.6-35B-A3B-UD-Q4_K_M")
    r.add_argument("--key-file", default="")
    r.add_argument("--max-rounds", type=int, default=40)
    r.add_argument("--resume", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("batch")
    a = ap.parse_args()
    if a.cmd == "run":
        asyncio.run(run_batch(a))
    else:
        score_batch(Path(a.batch))
