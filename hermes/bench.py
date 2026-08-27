"""Find out which of your models can actually be an agent.

Being good at conversation and being able to drive a tool loop are different
skills, and the gap is enormous at small sizes. A 7B model that writes a lovely
paragraph may be completely unable to emit a tool call this parser can read, and
you only discover that after watching it burn eighteen steps on a real job.

So: one fixed task, run against each model, graded on what actually happened
rather than on what the model said it did. The task is deliberately small and
mechanical — three tool calls and an exact number — because a model that cannot
do this will not do anything harder.

    hermes bench                 # every model your backends offer
    hermes bench --model qwen2.5:latest --model llama3.1:latest
    hermes bench --provider ollama
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import config, db, providers

# Two invoices and a rounding — the sort of sum a model will confidently get
# wrong on its own, so using the tool is the only way to land on the answer.
EXPECTED_TOTAL = "1729.74"


def _scenario_tools() -> dict:
    """The files a scenario starts from."""
    return {
        "invoices.txt": ("ACME Corp        1249.50\n"
                         "Globex Ltd        380.25\n"
                         "Initech            99.99\n"),
        "policy.md": ("# Approvals\n\n"
                      "Any single invoice over 1000.00 needs director approval.\n"
                      "Everything else is auto-approved.\n"),
    }


SCENARIOS = {
    # Can it call a tool at all, and put the result where it was told to?
    "basics": {
        "label": "Basics — call a tool, write the answer down",
        "files": {},
        "brief": (
            "Do these three things in order.\n"
            "1. Use the calc tool to work out: 1249.50 + 380.25 + 99.99\n"
            "2. Use write_file to write the result to total.txt in your workspace. "
            "The file must contain the number and nothing else — no words, no currency symbol.\n"
            "3. Use read_file to read total.txt back.\n"
            "Then call finish and state the total."
        ),
    },
    # The real shape of assistant work: several files in, one exact artefact
    # out, a fact it must not invent, and a condition it has to apply.
    "assistant": {
        "label": "Assistant — read several files, apply a rule, produce an exact artefact",
        "files": _scenario_tools(),
        "brief": (
            "Read invoices.txt and policy.md in your workspace.\n"
            "Use the calc tool to total the three invoice amounts.\n"
            "Use the now tool to get today's real date — do not guess it.\n"
            "Then write summary.md containing exactly three lines and nothing else:\n"
            "TOTAL: <the total>\n"
            "FLAGGED: <the supplier whose invoice breaks the policy>\n"
            "DATE: <today's date as YYYY-MM-DD>\n"
            "Do not change invoices.txt or policy.md."
        ),
    },
}


def _grade_basics(ws: Path, used: list, task: dict) -> dict:
    f = ws / "total.txt"
    body = f.read_text(errors="replace").strip() if f.exists() else ""
    clean = body.replace(",", "").replace("$", "").replace("£", "").strip()
    return {
        "called a tool": bool(used),
        "used calc": "calc" in used,
        "wrote the file": f.exists(),
        "total is right": EXPECTED_TOTAL in clean,
        "file is clean": clean == EXPECTED_TOTAL,
        "read it back": "read_file" in used,
        "finished": task.get("status") == "done",
    }


def _grade_assistant(ws: Path, used: list, task: dict) -> dict:
    import datetime
    today = datetime.date.today().isoformat()
    f = ws / "summary.md"
    raw = f.read_text(errors="replace") if f.exists() else ""
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    joined = raw.upper()

    def field(name):
        for ln in lines:
            if ln.upper().startswith(name):
                return ln.split(":", 1)[-1].strip()
        return ""

    sources_intact = (
        (ws / "invoices.txt").read_text(errors="replace") == _scenario_tools()["invoices.txt"]
        and (ws / "policy.md").read_text(errors="replace") == _scenario_tools()["policy.md"]
    ) if (ws / "invoices.txt").exists() and (ws / "policy.md").exists() else False

    return {
        "read both sources": used.count("read_file") >= 2,
        "used calc": "calc" in used,
        "asked for the real date": "now" in used,
        "wrote the summary": f.exists(),
        "exactly three lines": len(lines) == 3,
        "total is right": EXPECTED_TOTAL in field("TOTAL").replace(",", ""),
        "flagged the right supplier": "ACME" in field("FLAGGED").upper()
                                      and "GLOBEX" not in field("FLAGGED").upper(),
        "date is today's": today in field("DATE") or today in joined,
        "left the sources alone": sources_intact,
        "finished": task.get("status") == "done",
    }


GRADERS = {"basics": _grade_basics, "assistant": _grade_assistant}


def _verdict_from(passed: int, total: int) -> tuple:
    pct = passed / total if total else 0
    if pct == 1:
        return "excellent", "Drives the tool loop cleanly. Use it for real work."
    if pct >= 0.85:
        return "good", "Works as an agent. Fine for everyday tasks."
    if pct >= 0.6:
        return "usable", "Gets there, but needs narrow briefs and will waste steps."
    if pct >= 0.3:
        return "weak", "Calls tools but cannot follow through. Small jobs only."
    return "unusable", "Cannot drive tools. Do not assign it work."


def candidates(only_provider: str = "", only_models: list | None = None) -> list:
    """Every (provider, model) worth trying, given what is configured."""
    if only_models:
        prov = only_provider or db.setting("default.provider", "ollama")
        return [(prov, m) for m in only_models]
    out = []
    for p in providers.catalogue():
        if not p["ok"]:
            continue
        if only_provider and p["id"] != only_provider:
            continue
        try:
            models = providers.list_models(p["id"])
        except Exception:
            models = []
        for m in models:
            out.append((p["id"], m))
    return out


def run_one(provider: str, model: str, scenario: str = "assistant",
            max_steps: int = 16) -> dict:
    """Run one scenario once and grade what ended up on disk."""
    from .runtime import engine, tools

    spec = SCENARIOS[scenario]
    slug = model.replace("/", "_").replace(":", "_")
    workspace = config.HOME / "bench" / f"{slug}-{scenario}"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in spec["files"].items():
        (workspace / name).write_text(content)

    aid = db.nid()
    # Everything the scenario needs is allowed outright and everything else is
    # denied. The bench measures ability, not permission — and a tool left on
    # "ask" would park the whole run on an approval nobody is there to give,
    # which is exactly what happened the first time this ran.
    allowed = {"calc", "now", "read_file", "write_file", "list_dir",
               "search_files", "append_file", "finish", "escalate"}
    grants = {name: (tools.ALLOW if name in allowed else tools.DENY) for name in tools.BY_NAME}
    db.ex("""INSERT INTO agents(id,name,role,emoji,accent,system_prompt,provider,model,
             temperature,max_steps,grants,scopes,status,created_at,updated_at,archived,autonomy)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'idle',?,?,1,'autonomous')""",
          (aid, f"bench-{model}-{scenario}", "benchmark", "🧪", "#5C6CF2",
           "You complete the task exactly as written, using the tools available.",
           provider, model, 0.0, max_steps, json.dumps(grants),
           json.dumps({"fs_roots": [str(workspace)], "net_allow": []}),
           db.now(), db.now()))

    tid = db.nid()
    db.ex("""INSERT INTO tasks(id,title,brief,agent_id,status,priority,created_at,source)
             VALUES(?,?,?,?,'queued','normal',?,'bench')""",
          (tid, f"Bench: {scenario}", spec["brief"], aid, db.now()))

    started = time.time()
    error = ""
    try:
        engine.run_task(tid)
    except Exception as e:              # a model that cannot be reached is a result
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - started

    task = db.q1("SELECT * FROM tasks WHERE id=?", (tid,)) or {}
    run = db.q1("SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (tid,)) or {}
    transcript = db.jload(run.get("transcript"), [])
    used = [e.get("tool") for e in transcript if e.get("role") == "tool"]

    results = GRADERS[scenario](workspace, used, task)
    passed = sum(1 for v in results.values() if v)

    # Leave nothing behind. A benchmark that fills your run history with its own
    # throwaway tasks is a benchmark you stop running.
    db.ex("DELETE FROM evals WHERE run_id IN (SELECT id FROM runs WHERE task_id=?)", (tid,))
    db.ex("DELETE FROM runs WHERE task_id=?", (tid,))
    db.ex("DELETE FROM approvals WHERE agent_id=?", (aid,))
    db.ex("DELETE FROM memories WHERE agent_id=?", (aid,))
    db.ex("DELETE FROM tasks WHERE id=?", (tid,))
    db.ex("DELETE FROM agents WHERE id=?", (aid,))
    shutil.rmtree(workspace, ignore_errors=True)

    return {
        "provider": provider, "model": model, "scenario": scenario,
        "label": spec["label"], "results": results,
        "passed": passed, "total": len(results),
        "steps": run.get("steps") or 0, "seconds": round(elapsed, 1),
        "cost": run.get("cost") or 0.0, "tools_used": used,
        "error": error or (task.get("error") or ""),
    }


def run(only_provider: str = "", only_models: list | None = None,
        scenarios: list | None = None, on_start=None, on_result=None) -> list:
    """Bench every candidate across every scenario, then aggregate per model."""
    picks = candidates(only_provider, only_models)
    which = scenarios or list(SCENARIOS)
    rows = []
    for provider, model in picks:
        runs = []
        for scenario in which:
            if on_start:
                on_start(provider, model, scenario)
            r = run_one(provider, model, scenario)
            runs.append(r)
            if on_result:
                on_result(r)
        passed = sum(r["passed"] for r in runs)
        total = sum(r["total"] for r in runs)
        tier, advice = _verdict_from(passed, total)
        rows.append({
            "provider": provider, "model": model, "runs": runs,
            "passed": passed, "total": total, "tier": tier, "advice": advice,
            "seconds": round(sum(r["seconds"] for r in runs), 1),
            "steps": sum(r["steps"] for r in runs),
        })
    rows.sort(key=lambda r: (-r["passed"], r["seconds"]))
    return rows
