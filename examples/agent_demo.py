"""A local LLM runs a photophysics experiment through the bench's MCP tools.

The model gets one question and the tools, nothing else: no procedure, no hint about the
answer. It must plan the measurement, run it, quantify it and conclude. Every tool call
and result is written to a transcript, so the run can be checked step by step.

Run (with the photonics-lab venv and a local OpenAI-compatible model server):
  python examples/agent_demo.py --base-url http://127.0.0.1:18434/v1 --model <id> --key-file <path>
Outputs go to examples/runs/<timestamp>/: transcript.md, transcript.json, the bench data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

QUESTION = ("The bench excites a photosensitiser with an 800 nm laser and records emission near "
            "1270 nm. Determine experimentally whether this emission is excited by one-photon or "
            "two-photon absorption, and state your conclusion with its uncertainty.")
SYSTEM = ("You are a careful experimental physicist operating a real optics bench through tools. "
          "Plan before acting, check each result, never guess a number you could measure, keep "
          "the detector out of saturation, and finish by putting the bench in its safe state. "
          "When done, give a short conclusion: the measured value, its uncertainty, and what it means.")


def chat(base_url: str, key: str, model: str, messages: list, tools: list) -> dict:
    body = {"model": model, "messages": messages, "tools": tools, "temperature": 0.3, "max_tokens": 1500,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)


async def main(a) -> int:
    out = Path(__file__).parent / "runs" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    key = Path(a.key_file).read_text(encoding="utf-8").strip() if a.key_file else ""
    server = StdioServerParameters(command=sys.executable, args=[
        "-m", "photonics_lab.server", "--data-dir", str(out / "bench"), "--seed", str(a.seed),
        "--photon-order", str(a.photon_order)], env=None)
    transcript, t0 = [], time.time()
    async with stdio_client(server) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            mcp_tools = (await s.list_tools()).tools
            tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                       "parameters": t.inputSchema}} for t in mcp_tools]
            messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": QUESTION}]
            for step in range(a.max_rounds):
                resp = chat(a.base_url, key, a.model, messages, tools)
                msg = resp["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                if msg.get("content"):
                    transcript.append({"step": step, "role": "assistant", "text": msg["content"]})
                if not calls:
                    break
                messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for c in calls:
                    name = c["function"]["name"]
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    res = await s.call_tool(name, args)
                    text = "\n".join(getattr(x, "text", "") for x in res.content)
                    transcript.append({"step": step, "tool": name, "args": args, "result": json.loads(text) if text.startswith(("{", "[")) else text})
                    messages.append({"role": "tool", "tool_call_id": c.get("id", name), "content": text[:6000]})
                    print(f"[{time.time() - t0:6.1f}s] {name}({json.dumps(args)[:90]})", flush=True)
            else:
                transcript.append({"role": "harness", "text": f"stopped after {a.max_rounds} rounds"})
            # whatever the model did, the bench ends safe
            await s.call_tool("safe_state", {"reason": "demo harness end"})
    meta = {"question": QUESTION, "model": a.model, "seed": a.seed, "photon_order_truth": a.photon_order,
            "seconds": round(time.time() - t0, 1), "tool_calls": sum(1 for x in transcript if "tool" in x)}
    (out / "transcript.json").write_text(json.dumps({"meta": meta, "transcript": transcript}, indent=1), encoding="utf-8")
    lines = [f"# Agent run {out.name}", "", f"Model: `{a.model}` (local). Question: {QUESTION}", "",
             f"Hidden truth: {a.photon_order}-photon. Duration {meta['seconds']} s, {meta['tool_calls']} tool calls.", ""]
    for x in transcript:
        if "tool" in x:
            lines.append(f"- **{x['tool']}** `{json.dumps(x['args'])}` -> `{json.dumps(x['result'])[:300]}`")
        else:
            lines += ["", f"> {x['text']}", ""]
    (out / "transcript.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    final = next((x["text"] for x in reversed(transcript) if x.get("role") == "assistant"), "(no conclusion)")
    print(f"\nconclusion: {final}\n\nsaved to {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:18434/v1")
    ap.add_argument("--model", default="Qwen3.6-35B-A3B-UD-Q4_K_M")
    ap.add_argument("--key-file", default="")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--photon-order", type=int, default=2)
    ap.add_argument("--max-rounds", type=int, default=40)
    sys.exit(asyncio.run(main(ap.parse_args())))
