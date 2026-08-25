"""In-process pub/sub so the browser can watch runs live over SSE."""
from __future__ import annotations

import json
import queue
import threading

from .. import db

_subs: list[queue.Queue] = []
_lock = threading.Lock()


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=1000)
    with _lock:
        _subs.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        if q in _subs:
            _subs.remove(q)


def publish(event: dict) -> None:
    with _lock:
        targets = list(_subs)
    for q in targets:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def emit(kind: str, payload: dict, run_id: str = "", task_id: str = "", agent_id: str = "") -> dict:
    ts = db.now()
    event = {"kind": kind, "payload": payload, "run_id": run_id,
             "task_id": task_id, "agent_id": agent_id, "ts": ts}
    try:
        db.ex("INSERT INTO events(run_id,task_id,agent_id,kind,payload,ts) VALUES(?,?,?,?,?,?)",
              (run_id, task_id, agent_id, kind, json.dumps(payload), ts))
    except Exception:
        pass
    publish(event)
    return event
