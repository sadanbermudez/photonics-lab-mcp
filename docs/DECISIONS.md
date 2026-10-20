# Design decisions

photonics-lab-mcp is an MCP server that lets an AI agent run a photophysics experiment on a simulated optics bench: an 800 nm laser, a 1270 nm InGaAs spectrometer, a safety layer, a lab notebook and the analysis an experimentalist would do by hand. This file records the design decisions that shape the code, why each one was made, what it costs, and how to check it against the source. File and line pointers refer to the current tree.

## System in one picture

```
  Agent (Claude Code, or a local model via examples/agent_demo.py)
        |
        |  MCP: stdio, or streamable HTTP on 127.0.0.1 at a secret path
        v
  server.py  FastMCP("photonics-lab"), 10 tools
        |    every tool goes through _call():
        |      BenchError  -> {"error": "..."} the agent can act on
        |      any other   -> bench.safe_state(), then {"error": ..., "bench": "moved to safe state"}
        v
  bench.py  Bench + SafetyConfig
        |    _require(tier)       permission tiers: read_only < configure < full
        |    set_power()          power cap (default 200 mW, never above HARDWARE_MAX_MW = 500)
        |    arm() -> emission()  interlock: no emission without an explicit arm
        |    safe_state()         emission off, disarmed, allowed at every tier
        |    _log()  ----------------------------------------->  data_dir/notebook.jsonl
        |    acquire() ------------------------------------------>  data_dir/runs/<run_id>.csv + .json
        |
        +--> sim.py       expected_counts(), acquire(): signal ~ P^photon_order, shot/read
        |                 noise, dark current, bias, 16-bit saturation, linear background
        |
        +--> analysis.py  fit_peak(): Gaussian on a linear baseline, area ± error
                          power_law(): weighted log-log fit, exponent ± error, verdict

  drivers/pm100.py  real Thorlabs PM100 SCPI driver over PyVISA
                    (tested against tests/pm100_sim.yaml with pyvisa-sim; not yet wired into Bench)
```

## Decisions

### 1. Expose the bench as an MCP server

**Context.** The goal is an agent that operates a lab instrument, and the same instrument should be usable from different agents: Claude Code during development, and a local model for the published experiment. Each agent framework has its own tool-calling format.

**Decision.** `src/photonics_lab/server.py` builds a `FastMCP("photonics-lab", ...)` server and registers ten functions with `@mcp.tool()`: `bench_status`, `set_laser_power`, `arm_laser`, `laser_emission`, `safe_state`, `configure_spectrometer`, `acquire_spectrum`, `analyse_peak`, `fit_power_dependence`, `lab_notebook`. Each tool's docstring is its description for the model. `main()` runs over stdio by default; `--http` switches to streamable HTTP bound to `127.0.0.1`, with the URL path set to `/mcp-<secret>`, where the secret is generated once with `secrets.token_urlsafe(24)` and stored in `<data-dir>/.server-secret` (ignored by git).

**Why.** MCP (Model Context Protocol) is a standard way for a client to ask a server "what tools do you have" (`list_tools`) and then call them (`call_tool`). Writing the bench once as an MCP server means any MCP client can drive it without bench-specific glue. The repo shows this directly: `examples/agent_demo.py` connects over stdio, calls `list_tools()`, and converts each MCP tool into an OpenAI-style function definition (name, description, `inputSchema` as parameters) so a model served behind an OpenAI-compatible endpoint can use the same server Claude Code uses. FastMCP generates each tool's input schema from the Python type hints, so the signature and the schema cannot drift apart. The HTTP mode binds to localhost and hides the endpoint behind an unguessable path, so another local process cannot reach the laser just by knowing the port.

**Alternatives.** (a) Hand-written function-calling schemas for one vendor's API: ties the bench to one model provider and duplicates the schema by hand. (b) A REST API: works, but every agent would need custom code to describe the endpoints to the model. (c) A Python library imported by the agent: no process boundary, so the agent's code could reach past the safety layer and set `bench.power_mw` directly.

**Trade-offs.** The secret-path scheme is obscurity, not authentication: anyone who can read the data directory can read the secret, and there is no TLS (acceptable only because it binds to 127.0.0.1). The bench is a module-level singleton (`_bench`), so one server process serves one bench; several clients connected over HTTP would share the same laser state.

**Check it yourself.** `server.py` lines 24 to 32 (server and instructions), 56 to 128 (the ten tools), 147 to 153 (HTTP secret path). `examples/agent_demo.py` lines 53 to 55 (MCP tools converted to function definitions).

### 2. Run the experiment on a physics simulator with realistic imperfections

**Context.** The experiment needs a laser and an InGaAs spectrometer. Real hardware is not available, and an agent tested only on clean synthetic data proves little, because the hard parts of a real measurement are noise, background and detector limits.

**Decision.** `src/photonics_lab/sim.py` models the bench. `expected_counts()` builds the mean spectrum: a Gaussian singlet-oxygen peak at 1270 nm (18 nm FWHM) and a sensitiser fluorescence tail at the blue edge, both scaling as `(P/100 mW) ** photon_order`; a flat background that scales linearly with power; a wavelength-dependent quantum efficiency; a bias of 1000 counts; and dark current of 800 counts/s. `acquire()` adds Poisson shot noise and Gaussian read noise (35 counts rms) per read, clips each read at 65535 (16-bit full scale), and averages the reads.

**Why.** Each imperfection forces a real experimental decision on the agent. Saturation forces it to choose power and integration time with care (the clipping happens per read, as on a real ADC). Shot noise makes low-power points noisy, so the agent needs several powers over a wide range. The linear background is a deliberate trap: it scales as P^1 regardless of the photon order, so an analysis that sums the whole band mixes a P^1 term into a P^2 signal and reports a slope below 2 (the module docstring states this intent). Fitting the 1270 nm peak on a sloped baseline (decision 7) removes it. Because the photon order is a parameter of `Sample`, the same code can produce either answer (decision 10).

**Alternatives.** (a) A clean analytic model with no noise: any agent would pass, so the result means nothing. (b) Replaying recorded data from a real bench: realistic, but fixed, so the agent's choices of power and integration time could not change what it sees. (c) Waiting for hardware: blocks all the software work (tools, safety, analysis) that does not depend on it.

**Trade-offs.** The physics constants are, in the module's own words, "typical published values for such a bench, not measurements of one". The simulator has no laser drift, no photobleaching, no oxygen depletion and no wavelength calibration error, all of which a real singlet-oxygen measurement has. Results on the simulator say nothing about a real cuvette.

**Check it yourself.** `sim.py` lines 65 to 76 (`expected_counts`, with the linear background on line 74) and 79 to 89 (`acquire`). Tests: `pytest tests/test_lab.py -k "scales_with_photon_order or saturates"`.

### 3. Enforce safety in the server code, not in the prompt

**Context.** The agent controls a laser. A language model can misread an instruction, hallucinate a number, or be talked into an unsafe action by text it reads. A rule that exists only in a prompt is a request, not a limit.

**Decision.** All limits live in `src/photonics_lab/bench.py`, in the `Bench` class and its `SafetyConfig` (tier, `max_power_mw`, `max_integration_ms`, `max_averages`). Every state-changing method checks its limit before acting and raises `BenchError` with a message written for the agent ("The limit is a safety setting the agent cannot change."). No tool exists to change `SafetyConfig`; it is set only from the command line in `server.main()`. `server._call()` turns a `BenchError` into a JSON `{"error": ...}` result instead of a crash. `load_run()` strips every character except letters, digits and `-` from a run id before building a file path.

**Why.** The model can only act through the ten tools, and every tool goes through code that checks the limit. So even a fully wrong or manipulated agent cannot exceed the power cap: the check sits between the model's request and the (simulated) hardware. Returning the refusal as a readable error, rather than raising, keeps the conversation going and tells the agent what to do instead, which is how a refusal becomes useful rather than a dead end. The server's `instructions` string still tells the agent how to work, but that is guidance for a good run; the code is what makes a bad run safe. The run-id filter applies the same idea to file access: the agent supplies the id, so the code, not the agent's good intentions, keeps it inside the data folder.

**Alternatives.** (a) Put the limits in the system prompt: the model can ignore or forget them, and nothing checks. (b) A second model reviewing each action before it runs: adds latency and is itself a model that can be wrong. (c) Rely only on hardware interlocks: necessary on a real bench, but they cannot express rules such as "a read-only client may not touch the laser".

**Trade-offs.** Software limits are only as good as the code path: on real hardware, anything that bypasses this server (a vendor GUI, a direct VISA session) bypasses the limits too, so a hardware interlock is still needed. The limits are static; there is no way to raise them mid-session without restarting the server, by design.

**Check it yourself.** `bench.py` lines 86 to 95 (`set_power`), 165 to 172 (`load_run`). `server.py` lines 44 to 53 (`_call`). Tests: `pytest tests/test_lab.py -k "power_limit or escape"`.

### 4. Two-step arm then emit, a power cap below the hardware maximum, and a safe state that always works

**Context.** On a real laser bench, emission is gated by a key switch or interlock so a single mistaken command cannot fire the beam, and there is always a way to stop it.

**Decision.** In `bench.py`: `arm()` sets `armed = True` but does not emit; `emission(True)` refuses with "Interlock: arm the laser first" unless armed; `emission(False)` is always allowed. `set_power()` changes the setpoint only and never starts emission. The power cap defaults to 200 mW and `Bench.__post_init__` rejects any configured cap above `HARDWARE_MAX_MW = 500.0`. `safe_state()` turns emission off and disarms, has no tier check, and is exposed as a tool. In `server._call()`, any exception other than `BenchError` triggers `bench().safe_state(reason="fault: <ExceptionType>")` before the error is returned. The demo script also calls `safe_state` at the end of every run, whatever the model did.

**Why.** Two steps mean emission requires two separate, deliberate tool calls, so one hallucinated call cannot fire the laser. Separating "set power" from "emit" means changing the setpoint never has a side effect on the beam. The cap is a configurable software limit below the physical maximum, and the constructor refuses a cap above the physical maximum, so a bad command line cannot raise the cap past what the hardware allows. The asymmetry is the important part: turning the laser on needs permission and a sequence, turning it off needs nothing. And if the code itself fails in an unexpected way, the server assumes the worst and moves the bench to the safe state before telling anyone.

**Alternatives.** (a) A single `fire(power)` tool: simpler for the model, but one wrong call emits at the wrong power. (b) Require confirmation from a human for every emission: safest, but no autonomous experiment is possible. (c) Hard-code 500 mW as the only limit: the hardware maximum is rarely the safe operating limit for a given sample.

**Trade-offs.** Safety state lives in process memory. If the server process is killed, or the client disconnects mid-run, nothing turns the laser off: there is no watchdog or timeout. On a simulator that does not matter; on hardware it would need a hardware watchdog or a driver that defaults to off. Refusals (`BenchError`) deliberately do not disarm, so an agent that is armed and gets one refusal stays armed. Power can be changed while emitting; the agents in the published runs switched emission off before each change by their own choice, not because the code requires it.

**Check it yourself.** `bench.py` lines 46 to 50 (cap validation), 97 to 120 (`arm`, `emission`, `safe_state`). `server.py` lines 51 to 53 (fault path). `examples/agent_demo.py` line 80. Tests: `pytest tests/test_lab.py -k "interlock or safe_state_always"`.

### 5. Permission tiers: read_only, configure, full

**Context.** Different clients need different powers. A dashboard or a reviewing agent should be able to read state and analyse data without any way to touch the laser; an agent setting up a measurement may need to configure the spectrometer but not emit.

**Decision.** `bench.py` defines `TIERS = {"read_only": 0, "configure": 1, "full": 2}` and `_require(action, tier)`, which compares the bench's tier to the action's minimum and refuses (and logs) if it is too low. `set_power`, `configure` and `acquire` need `configure`; `arm` and `emission(True)` need `full`; status, notebook, analysis and `safe_state` have no tier check. The tier is fixed at start-up with `--tier`.

**Why.** An ordered set of levels, checked in one function, is easy to reason about: each action has one minimum, and raising the level only adds actions. Placing `acquire` at `configure` is a deliberate physics choice: with emission off, an acquisition is a dark or background spectrum, which is a real measurement that needs no laser, so a configure-tier client can collect calibration data without being able to emit. `acquire()` records `power_mw = 0.0` when emission is off, so the data is labelled honestly.

**Alternatives.** (a) Per-tool allow lists: more flexible, but harder to audit at a glance. (b) Hiding tools from lower tiers instead of refusing: the model would not know the tools exist, and would get no message explaining why it cannot proceed.

**Trade-offs.** The tier is per server process, not per client: two clients on one HTTP server share one tier. There is no authentication tying a tier to an identity. The `set_power` step is allowed at `configure`, so a configure-tier client can change the setpoint that a later full-tier session would emit at.

**Check it yourself.** `bench.py` lines 23, 66 to 70, 141 to 145. Tests: `pytest tests/test_lab.py -k "read_only_tier or configure_tier or dark_spectrum"`.

### 6. An append-only JSONL lab notebook that records refusals

**Context.** After an autonomous run, a human needs to see exactly what the agent did, including what it tried and was not allowed to do. The model's own summary of its run is not trustworthy evidence.

**Decision.** `Bench._log()` appends one JSON object per line to `<data-dir>/notebook.jsonl`: a timestamp, the action, `ok` true or false, and details. It is called on successful actions and inside every refusal path, before `BenchError` is raised (`_require`, `set_power` out of range, `emission` without arm, `configure` out of range). `fit_power_dependence` logs the exponent, its error and the excluded runs. The `lab_notebook` tool returns the last N entries (capped at 200). Each spectrum is saved as `runs/<run_id>.csv` (wavelength, counts) with a `.json` metadata file; the run id is a timestamp plus six random hex characters.

**Why.** JSON Lines means each entry is one independent line, opened in append mode, so a crash mid-write damages at most one line and the file can be read with any tool. Logging refusals is the point: a log that shows only successes hides exactly the behaviour a safety review needs, such as an agent that keeps trying to exceed the power cap. Writing the log entry before raising guarantees the refusal is recorded even though the action never happens. Saving raw spectra with metadata means the analysis can be redone later from the files alone, which is what `examples/make_figure.py` does.

**Alternatives.** (a) A SQLite database: queryable, but more machinery than a single-writer log needs. (b) Python `logging` to a text file: mixes diagnostics with lab records and is harder to parse back. (c) Relying on the MCP client's transcript: the transcript belongs to the client, can be lost, and is not produced at all by some clients.

**Trade-offs.** "Append-only" is a convention (the file is opened with mode `"a"`), not enforced: anything with file access can edit it, and there is no signing or hash chain. Read-only calls are not logged: `bench_status` and `lab_notebook` write nothing, and a request for a run id that does not exist is refused by `load_run()` without a notebook entry. The file grows without rotation.

**Check it yourself.** `bench.py` lines 61 to 70, 89 to 91, 105 to 108, 148 to 153, 181 to 185. In a published run, compare `examples/runs/20261020-142633/bench/notebook.jsonl` with `transcript.md` in the same folder. Test: `pytest tests/test_lab.py -k "power_limit or end_to_end"` (the end-to-end test asserts that the first notebook entry is the refused `emission_on`).

### 7. Measure the signal by fitting a Gaussian on a linear baseline, with the area error from the fit covariance

**Context.** The quantity that scales with laser power is the size of the 1270 nm peak. The spectrum also contains the detector bias, dark current, a fluorescence tail and a power-dependent background, so "the signal" has to be separated from everything under it, with an honest uncertainty.

**Decision.** `analysis.fit_peak()` selects the pixels within `center_nm ± window_nm` (defaults 1270 and 90 nm), and fits `amp * exp(-0.5 * ((x - center) / sigma)^2) + c0 + c1 * (x - center)` with `scipy.optimize.curve_fit`, starting from a baseline estimated as the median of the outer sixths of the window, with bounds on centre and width. The peak area is `amp * sigma * sqrt(2π)`. Its uncertainty comes from the 2 by 2 covariance of `amp` and `sigma`, propagated with the gradient `[sigma * sqrt(2π), amp * sqrt(2π)]`. It also returns FWHM, SNR (area divided by its error), residual rms and a `saturated` flag. `Bench.acquire()` separately counts pixels at or above full scale and adds a warning to its result.

**Why.** The linear baseline term absorbs everything that varies slowly across the window: bias, dark counts, the flat linear background and the local slope of the fluorescence tail. What is left in `amp` and `sigma` is the peak alone, so the area scales with the true excitation and not with the background. This is why the simulator's linear-background trap (decision 2) does not bias the result. The error propagation includes the covariance term because `amp` and `sigma` are correlated in a Gaussian fit (a slightly wider peak can be traded for a slightly lower one); treating them as independent would misstate the area error. Using the area rather than the peak height makes the measure less sensitive to small changes in width. The saturation flag exists because a clipped peak makes every number from the fit wrong.

**Alternatives.** (a) Summing counts over a band: simple, but includes the linear background and gives the wrong slope. (b) Subtracting a separately measured dark spectrum, then summing: removes bias and dark current but not the power-dependent background, and needs a dark spectrum the agent may not take (one of the published runs took none). (c) Peak height only: noisier, and biased if the width changes.

**Trade-offs.** The fit assumes a Gaussian line shape and a straight baseline within ±90 nm; a curved baseline or an asymmetric line would bias the area. Uncertainties from `curve_fit` with no `sigma` argument are scaled by the fit residuals, so they reflect observed scatter but not systematic errors. The saturation check in `Bench.acquire()` looks at the averaged spectrum: with several averages, a pixel that saturates in some reads but not all averages to below full scale and is not flagged, although its value is distorted.

**Check it yourself.** `analysis.py` lines 13 to 56 (area error on lines 41 to 44). Test: `pytest tests/test_lab.py -k fit_peak_recovers`.

### 8. Fit the power law by weighted least squares on log-log axes, scale the error by reduced chi-square, and let the server state the verdict

**Context.** For n-photon excitation the signal scales as P^n, so the photon order is the slope of log(signal) against log(power). The answer must come with an uncertainty that reflects the data, and bad points must not silently change it.

**Decision.** `analysis.power_law()` drops points with zero or negative power or signal (their log is undefined) and reports how many were dropped; refuses with fewer than 3 points; fits a straight line to `(log P, log S)` with weights `w = (S / σ_S)^2`; computes the slope error as `sqrt(chi2_red / Sxx)` where `chi2_red` is the weighted residual sum of squares divided by `N - 2`; and calls `interpret_exponent()`, which reports "two-photon" only if the exponent is within 2σ of 2 and more than 3σ from 1 (and the mirror image for one-photon), else "inconclusive" with advice. The `fit_power_dependence` tool in `server.py` runs `analyse_peak` on every run id, excludes runs whose fit failed, that were saturated, or that had zero power, and returns the excluded runs and a table of the points used.

**Why.** Taking logs turns the power law into a straight line, so the slope is the exponent. The weight follows from error propagation: the error on log S is σ_S / S, and a weighted fit uses 1 / (error)^2, which is (S / σ_S)^2. A weak, noisy point therefore counts for little: in the two-photon run the 10 mW point had an SNR of 1.4 and barely moved the fit. Scaling by reduced chi-square makes the quoted error match the actual scatter about the line: if the points scatter more than their error bars predict (chi2_red > 1), the error grows. Recomputing from the published table, the two-photon run has chi2_red of about 1.9, which widens the slope error from about 0.022 to the reported 0.030. The thresholds in `interpret_exponent()` (consistent within 2σ, excluding the alternative by more than 3σ) mean a verdict needs both agreement with one order and a clear rejection of the other. Exclusions happen in the server rather than being left to the agent, and are reported back, because silently losing data changes the answer.

**Alternatives.** (a) Non-linear fit of S = a P^n directly: equivalent in principle, but the log form gives a closed-form weighted linear regression with no starting guess or convergence risk. (b) Unweighted log-log fit: lets the noisiest low-power points pull the slope. (c) Leaving the interpretation to the agent: the published runs show the agent's physical reasoning is the weak link (decision 10).

**Trade-offs.** The reduced chi-square scaling also shrinks the error when the scatter is smaller than expected (chi2_red < 1); the one-photon run has chi2_red of about 0.92, which is why it quotes ±0.002. With few points, chi2_red is itself very uncertain. The error is statistical only: the power axis is the setpoint, not a measured power, so a calibration error in the laser power is not in the uncertainty. `r2` is computed unweighted, while the fit is weighted, so the two are not strictly comparable. `interpret_exponent()` prints the error to two decimals, so ±0.002 appears as "+/- 0.00" in the verdict string, while the `exponent_err` field holds the exact rounded value. The verdict thresholds are fixed and are not adjusted for the number of points.

**Check it yourself.** `analysis.py` lines 59 to 92 (weights on line 75, scaling on lines 83 to 85) and 95 to 105. `server.py` lines 106 to 122. Tests: `pytest tests/test_lab.py -k "power_law or power_sweep"`. The fit tables are in `examples/runs/*/transcript.json`.

### 9. Write the real PM100 power-meter driver now and test it against pyvisa-sim

**Context.** The bench is simulated, but the project should show the path to real hardware. Without the instrument on the desk, driver code usually goes untested until the day the hardware arrives.

**Decision.** `src/photonics_lab/drivers/pm100.py` is a SCPI driver for a Thorlabs PM100 power meter over PyVISA: `identify()` (`*IDN?`), `set_wavelength()` (`SENS:CORR:WAV`, then reads it back and refuses if the readback differs by more than 0.5 nm), and `read_power_mw()` (`MEAS:POW?`, watts converted to mW, negative readings refused). `set_wavelength()` refuses wavelengths outside the 400 to 1100 nm sensor range before anything is sent. `tests/test_pm100.py` opens the driver against `tests/pm100_sim.yaml` with the `@sim` backend of pyvisa-sim. PyVISA is an optional dependency (`visa` and `test` extras in `pyproject.toml`) and is imported inside `PM100.__init__`.

**Why.** pyvisa-sim simulates an instrument at the message level: the YAML file declares which query strings the device answers and with what, plus a `wavelength` property with a getter, setter and allowed range. The driver code runs unchanged, sends real SCPI strings, and parses real-format responses, so command spelling, termination characters and parsing are all tested in CI. Switching to hardware means changing only the resource string and VISA library. The range check before sending and the readback after setting follow instrument practice: never send a command the device will reject, and never assume a setting took effect without reading it back. A power meter is the first instrument to add because the power axis of the experiment is currently a setpoint, not a measurement (decision 8).

**Alternatives.** (a) Mocking `pyvisa` with `unittest.mock`: tests that the code calls the right methods, but not that the SCPI strings and parsing match what an instrument says. (b) Using a vendor SDK: ties the driver to one operating system and DLL, and is harder to simulate.

**Trade-offs.** pyvisa-sim answers what the YAML says, so the test proves the driver matches the YAML, not the real instrument; the SCPI commands come from the PM100 programming reference but have not been checked against a physical meter. The driver is not yet connected to `Bench`, so no reading enters the experiment. The simulated `MEAS:POW?` returns a fixed value.

**Check it yourself.** `drivers/pm100.py` lines 35 to 49, `tests/pm100_sim.yaml`, `pytest tests/test_pm100.py -v`.

### 10. Hide the answer in the server, run a control, and publish the agent's mistakes

**Context.** A demonstration where the agent reaches the textbook answer proves little: the question mentions an 800 nm laser and emission near 1270 nm, a setup commonly associated with two-photon excitation, so a model could state "two-photon" without measuring anything.

**Decision.** The photon order is a server start-up option (`--photon-order 1|2`, default 2) passed into `Sample(photon_order=...)`; it is not visible through any tool (`bench_status` reports the sample only as "photosensitiser in air-saturated solution"). `examples/agent_demo.py` launches the server as a subprocess with a chosen order and seed, gives the model one fixed question and a short system prompt, records every tool call and result to `transcript.json` and `transcript.md`, and stores the truth in the run metadata (`photon_order_truth`). Two runs are published in `examples/runs/`: two-photon truth (seed 11) and a one-photon control (seed 23). The README reports both results and lists the physics errors in the agent's closing explanations.

**Why.** With the truth hidden and changeable, the only reliable way to the right answer is the measurement, so a correct result in both conditions is evidence about the measurement chain rather than about the model's prior knowledge. The one-photon control is the run that would fail if the agent simply recited "two-photon at 800 nm". Recording every call and the hidden truth makes each run checkable by someone else. Stating the agent's mistakes (it attributed the 3.1 eV two-photon excitation energy to the singlet-oxygen emission, which is 0.98 eV; it described singlet oxygen as formed by absorption from the triplet, rather than energy transfer from the sensitiser triplet) turns the demo into an honest evaluation: it shows where the agent is reliable (operating the bench within limits, fitting) and where it is not (physical interpretation), which is the argument for keeping safety and statistics in the server and a human on the interpretation.

**Alternatives.** (a) A single run against the default sample: cannot distinguish measurement from recitation. (b) A benchmark of many seeded runs with a pass rate: stronger statistically; the two published runs are a demonstration, not a benchmark. (c) Publishing only the correct conclusions: looks better, hides the failure mode that matters most for deploying such an agent.

**Trade-offs.** Two runs on one model are an anecdote, not a measured success rate. The server does part of the reasoning for the agent: the `fit_power_dependence` tool description says the exponent is "about 1 for one-photon, about 2 for two-photon", and the tool returns an `interpretation` verdict that the agent repeated in both runs. So the control shows that the agent ran the right measurement and reported the server's verdict; it does not show the agent derived the verdict alone. The server's `instructions` string describes a procedure (dark spectrum first, then measure several powers), but `agent_demo.py` sends the model only the tool list, the system prompt and the question, so the local model did not receive that procedure.

**Check it yourself.** `server.py` lines 138 and 142 to 143 (photon order wiring), `bench.py` line 82, `examples/agent_demo.py` lines 24 to 30 and 46 to 48, `examples/runs/20261020-142442/transcript.md` (two-photon) and `examples/runs/20261020-142633/transcript.md` (control). Test: `pytest tests/test_lab.py -k hidden_photon_order`.

## Known limits and what comes next

- **Never run on real hardware.** The bench is a simulator and the PM100 driver has only been tested against pyvisa-sim.
- **No hardware abstraction yet.** `Bench.acquire()` calls `sim.acquire()` directly. A real spectrometer needs an interface with simulated and real implementations behind the same `acquire_spectrum` tool, as the README's Next section says.
- **Power is a setpoint, not a measurement.** The exponent's uncertainty is statistical only; wiring the PM100 reading into each acquisition would put a measured power on the x axis.
- **No watchdog.** Safety state lives in memory; a killed server or a dropped client leaves the simulated laser as it was. Real hardware needs a hardware interlock and an emission timeout.
- **Single bench, single tier, no client identity.** The server holds one `Bench` and one tier for all clients; the HTTP secret path is not authentication.
- **Notebook gaps.** Read-only calls and missing-run lookups are not logged, and the file is append-only by convention only.
- **Partial saturation with averaging is not flagged**, as described in decision 7.
- **Evaluation size.** Two agent runs on one local model (Qwen3.6-35B-A3B, Q4_K_M). A seeded batch of runs across orders and models would turn the demo into a measured success rate.
- **Interpretation check.** The README proposes a second model or a rule set that reviews the agent's closing physics against the measured numbers.

## Questions a reviewer might ask

**1. Why not just tell the model the limits in the system prompt?**
Because a prompt is advice and the model can ignore it, misread it or be argued out of it. In this code every tool goes through `Bench`, which checks the power cap, tier and interlock before acting and raises `BenchError` otherwise; there is no tool that changes `SafetyConfig`. The prompt can make a good run efficient, but only the code can make a bad run safe.

**2. What happens if the agent crashes or the code throws mid-measurement?**
Any unexpected exception inside a tool is caught by `server._call()`, which calls `safe_state()` (emission off, disarmed) and returns the error with "moved to safe state". Expected refusals (`BenchError`) do not change state. The gap is the process itself: if the server is killed or the client disconnects, nothing turns the laser off, which on real hardware would need a hardware watchdog.

**3. Why is the exponent error scaled by reduced chi-square, and what does that do to your two results?**
The weights come from each point's own error bar; if the points scatter more or less than those bars predict, the plain weighted-fit error is wrong. Multiplying the variance by chi2_red makes the quoted error match the observed scatter. In the two-photon run chi2_red is about 1.9, widening the error from about 0.022 to 0.030; in the one-photon run it is about 0.92, which slightly shrinks it to 0.002.

**4. ±0.002 on the exponent looks too good. Do you believe it?**
As a statistical error on this simulated data, yes: five high-SNR points (SNR from about 150 to 990) on a clean P^1 law. As a physical uncertainty, no: the power axis is the setpoint, so a power calibration error, detector non-linearity or drift is not included. That is why measured power from the PM100 is the next step.

**5. Why fit a Gaussian on a baseline instead of integrating the spectrum?**
The spectrum contains a background that scales linearly with power whatever the photon order. Integrating the band mixes that P^1 term into the signal and pulls a two-photon slope below 2. The linear baseline in `fit_peak()` absorbs the slowly varying background, so the Gaussian area tracks only the 1270 nm emission.

**6. How do you know the agent measured the answer rather than guessed it?**
The photon order is hidden in the server and differs between runs, and the one-photon control came out at 1.003 rather than the "expected" 2. One caveat: the `fit_power_dependence` tool returns an interpretation string and its description explains what 1 and 2 mean, so the agent's verdict leans on the server; the evidence is that the agent chose and ran a valid measurement, not that it reasoned to the verdict alone.

**7. Why test the driver with pyvisa-sim instead of mocks?**
pyvisa-sim answers real SCPI strings over a simulated wire according to `tests/pm100_sim.yaml`, so the actual command text, termination characters and response parsing run in CI. A mock would only confirm that `query` was called. The limit is that the test proves agreement with the YAML, not with a physical meter.

**8. How would you move this to a real bench?**
Put the spectrometer and laser behind an interface with the simulator as one implementation, so `Bench.acquire()` no longer calls `sim.acquire()` directly; keep `Bench` as the only path to the hardware so the safety checks still apply. Wire the PM100 reading into each acquisition and into the fit. Add a hardware interlock and an emission timeout, since software limits alone do not cover a crashed process.
