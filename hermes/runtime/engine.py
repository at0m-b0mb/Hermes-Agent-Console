"""The OpenClaw agent loop.

Tool calls travel as a plain text protocol rather than provider-native function
calling. That is deliberate: it works identically on a 7B local Ollama model and
on Claude, so an agent can be moved between backends without rewriting anything.
"""
from __future__ import annotations

import json
import re
import threading
import time

from .. import config, db, providers, security
from . import bus, tools, workforce

_cancelled: set[str] = set()
_approval_waiters: dict[str, threading.Event] = {}
_lock = threading.Lock()

TOOL_RE = re.compile(r"<tool>(.*?)</tool>", re.S)
FENCE_RE = re.compile(r"```(?:json|tool)?\s*(.*?)```", re.S)

FORMAT_REMINDER = """Your last reply was not a valid tool call, so nothing happened.

Reply with NOTHING except a tool block, in exactly this shape:

<tool>
{"name": "write_file", "args": {"path": "notes.txt", "content": "hello"}}
</tool>

The whole call is ONE JSON object with a "name" and an "args" object. Try again now."""

SYSTEM_TEMPLATE = """You are {name}, an autonomous agent{role_clause} operating inside Hermes.

{persona}

# How you work
You solve the task by calling tools, one at a time. After each tool call you will
be shown its result, then you decide the next step. Keep going until the task is
genuinely done, then call `finish`.

# Tools available to you
{tool_docs}

# Calling a tool
Emit EXACTLY one tool call per reply, in this format and nothing else after it:

<tool>
{{"name": "tool_name", "args": {{"arg": "value"}}}}
</tool>

You may write one short line of reasoning before the tool block. Never invent a
tool that is not listed. Never emit two tool blocks in one reply.

Worked example — writing a file then running it:

<tool>
{{"name": "write_file", "args": {{"path": "hello.py", "content": "print('hi')"}}}}
</tool>

then, after you are shown the result:

<tool>
{{"name": "run_shell", "args": {{"command": "python3 hello.py"}}}}
</tool>

Your reply must contain the tool block. A reply that only *describes* an action
does nothing at all — the work is not done until the tool actually runs.

# Finishing
When the work is complete, call `finish` with a full, self-contained answer:

<tool>
{{"name": "finish", "args": {{"summary": "your complete result here"}}}}
</tool>

# Your working scope
Filesystem: {fs_roots}
Network: {net_scope}
You have at most {max_steps} steps. Be efficient — do not re-read what you already read."""


def cancel(run_id: str) -> None:
    with _lock:
        _cancelled.add(run_id)


def is_cancelled(run_id: str) -> bool:
    with _lock:
        return run_id in _cancelled


def decide_approval(approval_id: str, approved: bool) -> bool:
    row = db.q1("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not row or row["state"] != "pending":
        return False
    db.ex("UPDATE approvals SET state=?, decided_at=? WHERE id=?",
          ("approved" if approved else "denied", db.now(), approval_id))
    with _lock:
        ev = _approval_waiters.get(approval_id)
    if ev:
        ev.set()
    agent = db.q1("SELECT name FROM agents WHERE id=?", (row["agent_id"],))
    security.audit(agent["name"] if agent else "operator",
                   "tool.approved" if approved else "tool.denied",
                   {"tool": row["tool"], "approval_id": approval_id,
                    "run_id": row["run_id"], "by": "human"})
    bus.emit("approval_decided", {"id": approval_id, "approved": approved}, run_id=row["run_id"])
    return True


def _await_approval(run_id: str, agent: dict, tool: str, args: dict) -> bool:
    aid = db.nid()
    db.ex("""INSERT INTO approvals(id,run_id,agent_id,tool,args,state,created_at)
             VALUES(?,?,?,?,?,'pending',?)""",
          (aid, run_id, agent["id"], tool, json.dumps(args), db.now()))
    ev = threading.Event()
    with _lock:
        _approval_waiters[aid] = ev
    bus.emit("approval_requested",
             {"id": aid, "tool": tool, "args": args, "agent": agent["name"],
              "danger": tools.BY_NAME[tool]["danger"]},
             run_id=run_id, agent_id=agent["id"])
    deadline = time.time() + 900
    while time.time() < deadline:
        if ev.wait(timeout=1.0):
            break
        if is_cancelled(run_id):
            break
    with _lock:
        _approval_waiters.pop(aid, None)
    row = db.q1("SELECT state FROM approvals WHERE id=?", (aid,))
    if row and row["state"] == "approved":
        return True
    if row and row["state"] == "pending":
        db.ex("UPDATE approvals SET state='expired', decided_at=? WHERE id=?", (db.now(), aid))
    return False


def _repair_json(fragment: str) -> str:
    """Fix the JSON mistakes small models make constantly.

    By far the most common is a multi-line string value — a file's contents or a
    command's output pasted in with real newlines, which JSON forbids. Left
    unrepaired the call fails to parse, the agent is told to retry, and it burns
    steps re-doing work it already did correctly.
    """
    out, in_str, esc = [], False, False
    for ch in fragment:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == '"':
                in_str = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
    repaired = "".join(out)
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)  # trailing commas
    return repaired


def _loads(fragment: str) -> dict | None:
    for candidate in (fragment, _repair_json(fragment)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _json_objects(text: str) -> list[dict]:
    """Every balanced {...} in the text. Regex cannot do this — JSON nests."""
    out, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                obj = _loads(text[start:i + 1])
                if obj is not None:
                    out.append(obj)
                start = None
            depth = max(0, depth)
    return out


def _normalise(obj: dict) -> dict | None:
    """Accept the shapes different models actually emit."""
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or obj.get("function")
    if isinstance(name, dict):
        name = name.get("name")
    if not isinstance(name, str) or name not in tools.BY_NAME:
        return None
    args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or obj.get("input") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return {"name": name, "args": args if isinstance(args, dict) else {}}


def _parse_tool_call(text: str) -> dict | None:
    """Small local models are inconsistent about format. Meet them where they are.

    Tried in order: the documented <tool> block, a fenced block, any balanced
    JSON object naming a real tool, then a bare `tool_name` line followed by a
    JSON object of arguments — the shape qwen and llama fall into most often.
    """
    for rx in (TOOL_RE, FENCE_RE):
        for m in rx.finditer(text):
            for obj in _json_objects(m.group(1)):
                call = _normalise(obj)
                if call:
                    return call
    for obj in _json_objects(text):
        call = _normalise(obj)
        if call:
            return call
    # `write_file` on its own line, arguments in the object that follows.
    for m in re.finditer(r"(?:^|\n)\s*[`*\"'#\s]*([a-z_]{3,20})[`*\"':\-\s]*\n", text):
        name = m.group(1)
        if name in tools.BY_NAME:
            tail = _json_objects(text[m.end():])
            if tail:
                return {"name": name, "args": tail[0]}
            if name in ("finish", "recall"):
                return {"name": name, "args": {}}
    return None


def _clean(text: str) -> str:
    """Strip tool scaffolding so a result reads as an answer, not as machinery."""
    text = TOOL_RE.sub("", text)
    text = re.sub(r"</?tool>", "", text)
    return text.strip()


def _build_system(agent: dict) -> str:
    scopes = db.jload(agent.get("scopes"), {})
    net = scopes.get("net_allow") or []
    return SYSTEM_TEMPLATE.format(
        name=agent["name"],
        role_clause=f" whose speciality is {agent['role']}" if agent.get("role") else "",
        persona=agent.get("system_prompt") or "Work carefully and verify your results.",
        tool_docs=tools.render_tool_docs(agent) or "(none granted — you can only answer directly)",
        fs_roots=", ".join(scopes.get("fs_roots") or [str(config.WORKSPACE)]),
        net_scope=", ".join(net) if net else "any host (no allowlist set)",
        max_steps=agent.get("max_steps") or 18,
    )


VERIFY_PROMPT = """You are a quality gate. Decide whether the work below is genuinely finished.

## Task assigned
{title}
{brief}

## What the agent actually did
{actions}

## The agent's claimed result
{result}

Be strict. The work is NOT complete if it: only describes what it would do, leaves
any part of the brief unaddressed, claims a file was written or a command ran with
no matching action above, or answers a different question than the one asked.

Reply with ONLY this JSON, no prose:
{{"complete": true|false, "missing": "if incomplete, exactly what is still required"}}"""


def _verify_completion(agent: dict, task: dict, result: str, transcript: list[dict]) -> dict:
    """Second opinion on whether the agent really did the work it claims.

    Uses the judge model when one is configured, otherwise the agent's own model.
    A verifier that errors out fails open — a broken quality gate must never
    block delivery of work that may well be fine.
    """
    if db.setting("verify.enabled", "1") != "1":
        return {"complete": True, "missing": ""}
    provider = db.setting("judge.provider") or agent["provider"]
    model = db.setting("judge.model") or agent["model"]
    actions = [f"{e.get('tool')}({json.dumps(e.get('args', {}))[:160]}) -> "
               f"{'ok' if e.get('ok') else 'ERROR'}: {str(e.get('content'))[:220]}"
               for e in transcript if e.get("role") == "tool"]
    prompt = VERIFY_PROMPT.format(
        title=task["title"], brief=task.get("brief", "")[:2000],
        actions="\n".join(actions[-25:]) or "(the agent used no tools at all)",
        result=(result or "(empty)")[:3000])
    try:
        reply = providers.chat(provider, model, [{"role": "user", "content": prompt}],
                               system="You output only valid JSON.", temperature=0.0,
                               max_tokens=500)
        m = re.search(r"\{.*\}", reply["text"], re.S)
        if not m:
            return {"complete": True, "missing": ""}
        data = json.loads(m.group(0))
        return {"complete": bool(data.get("complete", True)),
                "missing": str(data.get("missing", ""))[:1000]}
    except Exception:
        return {"complete": True, "missing": ""}


def run_task(task_id: str) -> str:
    """Execute a task end to end. Returns the run id. Blocks — call in a thread."""
    task = db.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise ValueError(f"No task {task_id}")
    agent = db.q1("SELECT * FROM agents WHERE id=?", (task["agent_id"],))
    if not agent:
        raise ValueError("Task has no assigned agent.")

    run_id = db.nid()
    db.ex("""INSERT INTO runs(id,task_id,agent_id,status,provider,model,started_at)
             VALUES(?,?,?,'running',?,?,?)""",
          (run_id, task_id, agent["id"], agent["provider"], agent["model"], db.now()))
    db.ex("UPDATE tasks SET status='running', started_at=? WHERE id=?", (db.now(), task_id))
    db.ex("UPDATE agents SET status='working' WHERE id=?", (agent["id"],))
    security.audit(agent["name"], "run.started",
                   {"run_id": run_id, "task_id": task_id, "task": task["title"],
                    "provider": agent["provider"], "model": agent["model"],
                    "autonomy": agent.get("autonomy")})
    bus.emit("run_started", {"task": task["title"], "agent": agent["name"],
                             "provider": agent["provider"], "model": agent["model"]},
             run_id=run_id, task_id=task_id, agent_id=agent["id"])

    system = _build_system(agent)
    brief = task["brief"] or ""
    messages = [{"role": "user", "content":
                 f"TASK: {task['title']}\n\n{brief}\n\nBegin. Use one tool per reply."}]
    transcript: list[dict] = []
    max_steps = int(agent.get("max_steps") or 18)
    totals = {"tin": 0, "tout": 0, "cost": 0.0, "ms": 0}
    result, error, status = "", "", "done"
    verifications = 0
    malformed = 0
    MAX_VERIFICATIONS = 2

    try:
        for step in range(1, max_steps + 1):
            if is_cancelled(run_id):
                status, error = "cancelled", "Cancelled by operator."
                break

            try:
                security.check_budget(totals["cost"])
            except security.BudgetExceeded as e:
                status, error = "halted", str(e)
                bus.emit("budget_halt", {"reason": str(e)}, run_id=run_id, agent_id=agent["id"])
                break

            bus.emit("step", {"n": step, "of": max_steps}, run_id=run_id, agent_id=agent["id"])
            try:
                reply = providers.chat(agent["provider"], agent["model"], messages,
                                       system=system, temperature=agent.get("temperature", 0.7))
            except providers.ProviderError as e:
                status, error = "failed", str(e)
                break

            totals["tin"] += reply["tokens_in"]
            totals["tout"] += reply["tokens_out"]
            totals["cost"] += reply["cost"]
            totals["ms"] += reply["latency_ms"]
            text = security.redact(reply["text"].strip())
            transcript.append({"role": "assistant", "content": text, "step": step})
            db.ex("""UPDATE runs SET steps=?,tokens_in=?,tokens_out=?,cost=?,latency_ms=?,transcript=?
                     WHERE id=?""",
                  (step, totals["tin"], totals["tout"], totals["cost"], totals["ms"],
                   json.dumps(transcript), run_id))

            call = _parse_tool_call(text)
            thought = (TOOL_RE.sub("", FENCE_RE.sub("", text)).strip() if call else text)
            if thought:
                bus.emit("thought", {"text": thought[:2000]}, run_id=run_id, agent_id=agent["id"])

            if not call:
                # A reply with no parseable tool call is NOT a finished task.
                # Silently accepting it here is how an agent "completes" work it
                # never did, so every such reply must earn its way out.
                malformed += 1
                looks_like_attempt = any(t in text for t in tools.BY_NAME) or "{" in text
                if looks_like_attempt and malformed <= 3:
                    bus.emit("malformed_call", {"attempt": malformed},
                             run_id=run_id, agent_id=agent["id"])
                    messages.append({"role": "assistant", "content": text or "(empty)"})
                    messages.append({"role": "user", "content": FORMAT_REMINDER})
                    continue
                if not text:
                    messages.append({"role": "assistant", "content": "(empty)"})
                    messages.append({"role": "user", "content": FORMAT_REMINDER})
                    continue
                # Plain prose: allow it as an answer only if the gate agrees.
                text = _clean(text)
                check = _verify_completion(agent, task, text, transcript)
                if check["complete"] or verifications >= MAX_VERIFICATIONS:
                    result = text
                    status = "done" if check["complete"] else "incomplete"
                    if not check["complete"]:
                        error = f"Quality gate never passed: {check['missing'][:300]}"
                    break
                verifications += 1
                bus.emit("verify_rejected", {"missing": check["missing"]},
                         run_id=run_id, agent_id=agent["id"])
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content":
                                 f"That is not finished work. Still outstanding:\n"
                                 f"{check['missing']}\n\n{FORMAT_REMINDER}"})
                continue

            name, args = call["name"], call.get("args") or {}
            if name == "finish":
                claimed = _clean(args.get("summary", "") or thought)
                if verifications < MAX_VERIFICATIONS and step < max_steps:
                    bus.emit("verifying", {}, run_id=run_id, agent_id=agent["id"])
                    check = _verify_completion(agent, task, claimed, transcript)
                    if not check["complete"]:
                        verifications += 1
                        bus.emit("verify_rejected", {"missing": check["missing"]},
                                 run_id=run_id, agent_id=agent["id"])
                        nudge = ("QUALITY GATE REJECTED your result. Still outstanding:\n"
                                 f"{check['missing']}\n\nDo the remaining work now — actually "
                                 "perform it with tools, do not merely describe it. "
                                 "Then call finish again.")
                        messages.append({"role": "assistant", "content": text})
                        messages.append({"role": "user", "content": nudge})
                        transcript.append({"role": "tool", "tool": "quality_gate",
                                           "content": nudge, "ok": False})
                        continue
                    bus.emit("verify_passed", {}, run_id=run_id, agent_id=agent["id"])
                result = claimed
                bus.emit("finished", {"summary": result[:4000]},
                         run_id=run_id, task_id=task_id, agent_id=agent["id"])
                break

            bus.emit("tool_call", {"tool": name, "args": args},
                     run_id=run_id, agent_id=agent["id"])
            mode = tools.grant_of(agent, name)
            # An irreversible outward-facing action is exempt from autonomy AND
            # from the grant table. Granting it "allow" only means the tool
            # exists for this agent — it can never stand in for a human saying
            # yes to this particular send. Only DENY still wins, because DENY
            # removes the tool outright.
            gate = tools.ASK if (security.requires_human(name) and mode != tools.DENY) else mode
            human_approved = False
            if gate == tools.ASK and workforce.auto_approve(agent, name):
                security.audit(agent["name"], "tool.auto_approved",
                               {"tool": name, "autonomy": agent.get("autonomy"),
                                "run_id": run_id})
                bus.emit("auto_approved", {"tool": name, "autonomy": agent.get("autonomy")},
                         run_id=run_id, agent_id=agent["id"])
            elif gate == tools.ASK:
                if not _await_approval(run_id, agent, name, args):
                    observation = (f"DENIED: the operator refused permission to run '{name}'. "
                                   "Do not retry it. Find another way or call finish explaining "
                                   "what you could not do.")
                    bus.emit("tool_denied", {"tool": name}, run_id=run_id, agent_id=agent["id"])
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": observation})
                    transcript.append({"role": "tool", "tool": name, "content": observation})
                    continue
                human_approved = True

            try:
                observation = tools.execute(agent, name, args,
                                            {"task_id": task_id, "run_id": run_id,
                                             "human_approved": human_approved})
                ok = True
            except security.SecurityViolation as e:
                observation, ok = f"SECURITY BLOCK: {e}", False
                bus.emit("security_block", {"tool": name, "reason": str(e)},
                         run_id=run_id, agent_id=agent["id"])
            except (tools.ToolError, tools.Denied) as e:
                observation, ok = f"ERROR: {e}", False
            except Exception as e:  # a tool blowing up must not kill the run
                observation, ok = f"ERROR: {type(e).__name__}: {e}", False

            bus.emit("tool_result", {"tool": name, "ok": ok, "output": observation[:3000]},
                     run_id=run_id, agent_id=agent["id"])
            transcript.append({"role": "tool", "tool": name, "args": args,
                               "content": observation, "ok": ok})
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"RESULT of {name}:\n{observation}"})
        else:
            status = "done" if result else "incomplete"
            if not result:
                error = f"Hit the {max_steps}-step limit without calling finish."
    except Exception as e:
        status, error = "failed", f"{type(e).__name__}: {e}"

    finished = db.now()
    db.ex("""UPDATE runs SET status=?,steps=?,tokens_in=?,tokens_out=?,cost=?,latency_ms=?,
             transcript=?,finished_at=? WHERE id=?""",
          (status, len(transcript), totals["tin"], totals["tout"], totals["cost"],
           totals["ms"], json.dumps(transcript), finished, run_id))
    db.ex("UPDATE tasks SET status=?,result=?,error=?,finished_at=? WHERE id=?",
          (status, result, error, finished, task_id))
    db.ex("UPDATE agents SET status='idle' WHERE id=?", (agent["id"],))
    with _lock:
        _cancelled.discard(run_id)
    security.audit(agent["name"], "run.finished",
                   {"run_id": run_id, "task_id": task_id, "status": status,
                    "steps": len(transcript), "cost": round(totals["cost"], 6),
                    "verifications": verifications, "error": error[:300]})
    bus.emit("run_finished", {"status": status, "error": error, "result": result[:4000],
                              "cost": totals["cost"], "steps": len(transcript),
                              "tokens_in": totals["tin"], "tokens_out": totals["tout"]},
             run_id=run_id, task_id=task_id, agent_id=agent["id"])

    if status == "done" and db.setting("judge.provider"):
        threading.Thread(target=_auto_eval, args=(run_id,), daemon=True).start()
    return run_id


def _auto_eval(run_id: str) -> None:
    from . import evaluator
    try:
        evaluator.judge_run(run_id)
    except Exception as e:
        bus.emit("eval_failed", {"error": str(e)}, run_id=run_id)


def start(task_id: str) -> None:
    threading.Thread(target=_safe_run, args=(task_id,), daemon=True).start()


def _safe_run(task_id: str) -> None:
    try:
        run_task(task_id)
    except Exception as e:
        db.ex("UPDATE tasks SET status='failed', error=? WHERE id=?", (str(e), task_id))
        bus.emit("run_failed", {"error": str(e)}, task_id=task_id)
