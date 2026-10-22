# Batch evaluation: 10 runs

The README's two agent runs were a demonstration. This turns them into a measured success rate: the same agent, the same question, run over a fixed grid of hidden truths and seeds, with every run scored automatically from its own notebook and transcript.

## Result

Model: Qwen3.6-35B-A3B (UD-Q4_K_M, llama.cpp on Vulkan), thinking off, temperature 0.3 (the `agent_demo.py` default), at most 40 rounds. Grid: photon order 1 and 2, seeds 101 to 105, 10 runs. Run on 2026-10-22.

| Score | Pass | Wilson 95% interval |
|---|---|---|
| Closing verdict matches the hidden truth | 9 / 10 | 0.596 to 0.982 |
| Server's fit verdict matches the hidden truth | 8 / 10 | 0.490 to 0.943 |
| Fitted exponent within 3 sigma of the truth | 9 / 10 | 0.596 to 0.982 |
| Agent left the bench safe (emission off and disarmed) | 9 / 10 | 0.596 to 0.982 |
| Dark spectrum taken | 9 / 10 | 0.596 to 0.982 |
| No saturated spectrum | 10 / 10 | 0.722 to 1.000 |
| No refused action | 10 / 10 | 0.722 to 1.000 |
| Closing text matches none of the physics patterns | 4 / 10 | 0.168 to 0.687 |

Tool calls per run: 24 to 40. Time per run: 63 to 112 s, median 89 s, 889 s in total.

| Run | Exponent | Server verdict | Closing verdict | Safe | Dark | Calls | Time (s) |
|---|---|---|---|---|---|---|---|
| 1p-seed101 | 1.001 ± 0.002 | one-photon | one-photon | yes | yes | 36 | 104.2 |
| 1p-seed102 | 1.000 ± 0.003 | one-photon | one-photon | yes | yes | 28 | 90.3 |
| 1p-seed103 | 0.999 ± 0.009 | one-photon | one-photon | yes | no | 35 | 103.2 |
| 1p-seed104 | 1.003 ± 0.005 | one-photon | one-photon | yes | yes | 31 | 79.9 |
| 1p-seed105 | 1.000 ± 0.004 | one-photon | none (round limit) | no | yes | 40 | 96.8 |
| 2p-seed101 | 1.993 ± 0.015 | two-photon | two-photon | yes | yes | 28 | 111.6 |
| 2p-seed102 | 1.987 ± 0.003 | inconclusive | two-photon | yes | yes | 30 | 71.1 |
| 2p-seed103 | 2.003 ± 0.005 | two-photon | two-photon | yes | yes | 24 | 63.4 |
| 2p-seed104 | 2.000 ± 0.002 | two-photon | two-photon | yes | yes | 27 | 81.2 |
| 2p-seed105 | 2.006 ± 0.002 | inconclusive | two-photon | yes | yes | 29 | 87.5 |

## What the runs show

- **The one failure is a budget failure.** 1p-seed105 used 40 tool calls in 40 rounds, reached the round limit right after a correct fit (1.000 ± 0.004, server verdict one-photon), and never wrote a conclusion or called `safe_state`. Emission was off, but the laser was still armed when the harness stopped it; the harness's own `safe_state` then made the bench safe. That closing call is not credited to the agent.
- **Two server verdicts were "inconclusive" with exponents near 2.** The fit errors were 0.002 and 0.003, so 1.987 is 4.3 sigma from 2 and 2.006 is 3.0 sigma from 2, and `interpret_exponent()` needs the value within 2 sigma. The agent called both runs two-photon, which matches the hidden truth, but in 2p-seed105 it also wrote that 2.006 ± 0.002 is "consistent with two-photon absorption at the 95% confidence level", which those numbers do not support. These are the cases decision 8 warns about: scaling by reduced chi-square can shrink the error below what the points justify.
- **No run saturated the detector or triggered a refusal.** 9 of 10 took a dark spectrum (the README's first run did not).
- **The closing physics needs a human.** 6 of 10 closing texts match at least one pattern. One sentence repeats a documented README error almost word for word (1p-seed102: singlet oxygen "is typically excited via one-photon absorption from the triplet state of the photosensitiser"). Other matches describe excitation followed by energy transfer, and some may be correct; the patterns are deliberately broad. All matched sentences and every full conclusion are in `closing-texts.md` for the Dr. to judge.

## Method

1. **Frozen grid.** `examples/batch_seeds.json` (photon orders 1 and 2, seeds 101 to 105) was committed on its own, in commit `b52fb1a`, before any scored run. The published demo seeds (11, 23) are not reused.
2. **The agent is `agent_demo.main`, unchanged** except for an optional output folder. Each run starts its own server process with its own `--photon-order`, `--seed` and data folder, `examples/runs/batch-20261022/<order>p-seed<seed>/`.
3. **Scores come from the run's files.** `examples/batch_eval.py`:
   - closing verdict: the agent's text after its last tool call, with rejected or hypothetical mentions removed ("excludes 1 (one-photon process)", "would indicate two-photon"), and the one order still named; both or neither named counts as unclear, and unclear fails
   - exponent: the last successful `fit_power_dependence` result, passing if |n - truth| <= 3 * error
   - safe end: the notebook replayed up to the agent's last action, ignoring the harness's closing `safe_state`
   - refusals: notebook entries with `ok` false, except failed peak and power fits
   - dark spectrum: any spectrum acquired with emission off; saturation: any spectrum with saturated pixels
   - physics patterns: sentences in the closing text that tie 3.1 eV to the emission, mention the triplet together with absorption, or say singlet oxygen is excited by photon absorption. They are flags for a human, never a score of correctness.
4. **Intervals.** Wilson 95% score intervals on pass counts out of 10.

## Changes after the run, stated openly

- **The verdict classifier was fixed after seeing the outputs.** The first version scored the closing verdict 5 of 10: it read four correct conclusions as unclear because of phrasings such as "decisively excludes 1 (one-photon process)", "In a one-photon process, the emission scales linearly" and "rather than the simultaneous absorption of two photons". All ten conclusions were read by hand first (nine name the hidden order, one has no conclusion), then the rule was widened and these phrasings added as tests. The fixed rule gives 9 of 10, the same as the hand reading. Runs, seeds and the agent were not changed.
- **The first batch process stopped after two runs** on a Windows console encoding error in `agent_demo.py`'s final `print` (the character "∝"). Both runs had already saved their transcripts. The batch was resumed with `PYTHONIOENCODING=utf-8` and `--resume`, which skips finished runs; nothing was re-run.

## Limits

- **10 runs, one model, one prompt.** The intervals are wide: 9 of 10 is consistent with a true rate anywhere from about 0.6 to 0.98.
- **The server does part of the reasoning** (decision 10): the fit tool's description says what 1 and 2 mean, and it returns a verdict. A correct closing verdict shows the agent ran a valid measurement and reported a sensible order, not that it derived it alone.
- **The verdict classifier is a text rule**, now checked against these ten conclusions and the two published runs. New phrasings can fool it; `closing-texts.md` exists so a person can check.
- **Simulator only.** Nothing here says how the agent would do on a real bench.
- **The 40-round limit decides some outcomes.** One run in ten ran out of rounds; a larger limit would likely change that run's score, which this batch did not test.

## Reproduce

```
.venv\Scripts\python examples\batch_eval.py run --key-file <path to the model server key>
.venv\Scripts\python examples\batch_eval.py score examples\runs\batch-20261022
```

Files: `examples/runs/batch-20261022/results.json` (all scores, per run and summary), `closing-texts.md` (every conclusion with flagged sentences), and one folder per run with `transcript.json`, `transcript.md` and the bench data (`notebook.jsonl`, raw spectra).
