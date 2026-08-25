"""The workforce dispatcher — what turns an agent into an employee.

Without this, an agent is a button you press. With it, each agent owns a queue,
picks up its own work, retries what fails, keeps standing duties on a cadence,
and escalates to you rather than dying quietly.

Three autonomy levels decide how much it does without asking:

  supervised  every ask-first tool waits for you. Good for a new agent.
  trusted     routine ask-first tools proceed; anything `critical`
              (shell commands) still waits for you.
  autonomous  everything the agent has been granted proceeds unattended.
              It can only ever do what you granted — autonomy widens *when*
              it asks, never *what* it may touch.
"""
from __future__ import annotations

import threading
import time

from .. import db, security
from . import bus, tools

SUPERVISED, TRUSTED, AUTONOMOUS = "supervised", "trusted", "autonomous"

LEVELS = {
    SUPERVISED: {"label": "Supervised", "auto": (),
                 "blurb": "Asks you before every write, fetch or command."},
    TRUSTED: {"label": "Trusted", "auto": ("low", "medium", "high"),
              "blurb": "Works unattended, but still asks before shell commands."},
    AUTONOMOUS: {"label": "Autonomous", "auto": ("low", "medium", "high", "critical"),
                 "blurb": "Works fully unattended within its granted tools and scope."},
}

_stop = threading.Event()
_thread: threading.Thread | None = None
TICK = 4.0


def auto_approve(agent: dict, tool: str) -> bool:
    """Does this agent's autonomy level cover this tool without asking?

    Irreversible outward-facing actions are exempt from autonomy entirely. This
    is what makes reading untrusted email safe: an injection can hijack the
    agent completely and still not get one message sent without a human.
    """
    if security.requires_human(tool):
        return False
    level = agent.get("autonomy") or SUPERVISED
    danger = tools.BY_NAME.get(tool, {}).get("danger", "critical")
    return danger in LEVELS.get(level, LEVELS[SUPERVISED])["auto"]


def on_shift(agent: dict) -> bool:
    """Working hours. 'always', or 'HH:MM-HH:MM' local time."""
    shift = (agent.get("shift") or "always").strip()
    if shift in ("", "always"):
        return True
    try:
        start, end = shift.split("-")
        now = time.localtime()
        cur = now.tm_hour * 60 + now.tm_min

        def mins(s):
            h, m = s.split(":")
            return int(h) * 60 + int(m)
        a, b = mins(start), mins(end)
        return a <= cur < b if a <= b else (cur >= a or cur < b)
    except Exception:
        return True


def _busy_agents() -> set[str]:
    return {r["agent_id"] for r in
            db.q("SELECT DISTINCT agent_id FROM runs WHERE status='running'")}


def _promote_duties() -> None:
    """Standing responsibilities become concrete tasks when they come due."""
    now = db.now()
    for duty in db.q("SELECT * FROM duties WHERE active=1"):
        due = duty["next_run_at"] or 0
        if due > now:
            continue
        agent = db.q1("SELECT * FROM agents WHERE id=? AND archived=0", (duty["agent_id"],))
        if not agent:
            db.ex("UPDATE duties SET active=0 WHERE id=?", (duty["id"],))
            continue
        tid = db.nid()
        db.ex("""INSERT INTO tasks(id,title,brief,agent_id,status,priority,created_at,
                 duty_id,source) VALUES(?,?,?,?,'queued','normal',?,?,'duty')""",
              (tid, duty["title"], duty["brief"], duty["agent_id"], now, duty["id"]))
        db.ex("UPDATE duties SET next_run_at=?, last_task_id=?, runs=runs+1 WHERE id=?",
              (now + max(1, duty["cadence_minutes"]) * 60, tid, duty["id"]))
        bus.emit("duty_due", {"duty": duty["title"], "task_id": tid},
                 task_id=tid, agent_id=duty["agent_id"])


def _requeue_failures() -> None:
    """An employee who hits an error tries again before giving up."""
    rows = db.q("""SELECT * FROM tasks WHERE status IN ('failed','incomplete')
                   AND attempts < max_attempts""")
    for t in rows:
        if db.q1("SELECT id FROM escalations WHERE task_id=? AND state='open'", (t["id"],)):
            continue  # blocked on a human answer, not a retry
        db.ex("""UPDATE tasks SET status='queued', attempts=attempts+1,
                 brief=?, error='' WHERE id=?""",
              (f"{t['brief']}\n\n[Retry {t['attempts'] + 1}] Your previous attempt ended with: "
               f"{(t['error'] or 'no result')[:400]}. Take a different approach this time.",
               t["id"]))
        bus.emit("task_retry", {"task": t["title"], "attempt": t["attempts"] + 1},
                 task_id=t["id"], agent_id=t["agent_id"])


def _dispatch() -> None:
    """Hand queued work to any idle agent that is on shift."""
    from . import engine
    busy = _busy_agents()
    order = {"high": 0, "normal": 1, "low": 2}
    queued = db.q("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at")
    queued.sort(key=lambda t: order.get(t.get("priority"), 1))
    for task in queued:
        aid = task["agent_id"]
        if not aid or aid in busy:
            continue
        agent = db.q1("SELECT * FROM agents WHERE id=? AND archived=0", (aid,))
        if not agent or not on_shift(agent):
            continue
        busy.add(aid)
        bus.emit("task_picked_up", {"task": task["title"], "agent": agent["name"],
                                    "source": task.get("source", "operator")},
                 task_id=task["id"], agent_id=aid)
        engine.start(task["id"])


def _loop() -> None:
    while not _stop.is_set():
        try:
            _promote_duties()
            _requeue_failures()
            _dispatch()
        except Exception as e:
            bus.emit("workforce_error", {"error": f"{type(e).__name__}: {e}"})
        _stop.wait(TICK)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="workforce", daemon=True)
    _thread.start()
    bus.emit("workforce_started", {"tick": TICK})


def stop() -> None:
    _stop.set()


def running() -> bool:
    return bool(_thread and _thread.is_alive() and not _stop.is_set())


def answer_escalation(escalation_id: str, answer: str) -> dict:
    """Answering unblocks the task and puts it straight back in the queue."""
    esc = db.q1("SELECT * FROM escalations WHERE id=?", (escalation_id,))
    if not esc:
        raise ValueError("No such escalation")
    db.ex("UPDATE escalations SET answer=?, state='answered', resolved_at=? WHERE id=?",
          (answer, db.now(), escalation_id))
    task = db.q1("SELECT * FROM tasks WHERE id=?", (esc["task_id"],))
    if task:
        db.ex("""UPDATE tasks SET status='queued', attempts=0,
                 brief=? WHERE id=?""",
              (f"{task['brief']}\n\n[Operator answered your question]\n"
               f"You asked: {esc['question']}\nAnswer: {answer}\n\nContinue the task.",
               task["id"]))
    bus.emit("escalation_answered", {"id": escalation_id, "answer": answer[:500]},
             task_id=esc["task_id"], agent_id=esc["agent_id"])
    return {"ok": True, "task_id": esc["task_id"]}
