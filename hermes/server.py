"""Local HTTP server: static UI, JSON API, and a live SSE event stream.

Binds to 127.0.0.1 only. The Host header is checked on every request so a
malicious page cannot reach this server via DNS rebinding.
"""
from __future__ import annotations

import json
import mimetypes
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, config, db, providers, security, templates
from .runtime import bus, engine, evaluator, tools, workforce

ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")

# Set by serve() when the operator deliberately exposes Hermes beyond loopback.
BIND_HOST = "127.0.0.1"
_auth_fails: dict[str, list] = {}
_auth_lock = threading.Lock()
MAX_FAILS, LOCKOUT_SECONDS = 8, 300


def _throttled(ip: str) -> float:
    """Seconds remaining in lockout, or 0. Stops token brute-forcing."""
    with _auth_lock:
        fails = [t for t in _auth_fails.get(ip, []) if db.now() - t < LOCKOUT_SECONDS]
        _auth_fails[ip] = fails
        if len(fails) >= MAX_FAILS:
            return LOCKOUT_SECONDS - (db.now() - fails[0])
    return 0


def _record_fail(ip: str) -> None:
    with _auth_lock:
        _auth_fails.setdefault(ip, []).append(db.now())
    security.audit("system", "auth.failed", {"ip": ip})


class ApiError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ helpers

def _agent_row(a: dict) -> dict:
    a = dict(a)
    a["grants"] = db.jload(a.get("grants"), {})
    a["scopes"] = db.jload(a.get("scopes"), {})
    return a


def _require(data: dict, *fields: str) -> None:
    missing = [f for f in fields if not str(data.get(f, "")).strip()]
    if missing:
        raise ApiError(f"Missing required field(s): {', '.join(missing)}")


STARTER_AGENTS = [
    {"name": "Atlas", "role": "Research & synthesis", "emoji": "🗺️", "accent": "#5B8DEF",
     "system_prompt": "You research thoroughly before answering. You cite the file or URL "
                      "behind every claim. You never guess when you can check.",
     "grants": {"read_file": "allow", "list_dir": "allow", "search_files": "allow",
                "http_fetch": "allow", "remember": "allow", "recall": "allow",
                "write_file": "ask", "run_shell": "deny", "delegate": "ask", "finish": "allow"}},
    {"name": "Forge", "role": "Code & automation", "emoji": "⚒️", "accent": "#F5B93B",
     "system_prompt": "You write working code, then you run it to prove it works. You read "
                      "surrounding code first and match its style. You never claim something "
                      "passes without executing it.",
     "grants": {"read_file": "allow", "list_dir": "allow", "search_files": "allow",
                "write_file": "ask", "run_shell": "ask", "http_fetch": "ask",
                "remember": "allow", "recall": "allow", "delegate": "ask", "finish": "allow"}},
    {"name": "Ledger", "role": "Files, notes & organisation", "emoji": "📒", "accent": "#4FD1A5",
     "system_prompt": "You keep things tidy and organised. You summarise clearly and always "
                      "confirm the exact path of any file you touch.",
     "grants": {"read_file": "allow", "list_dir": "allow", "search_files": "allow",
                "write_file": "ask", "run_shell": "deny", "http_fetch": "deny",
                "remember": "allow", "recall": "allow", "delegate": "ask", "finish": "allow"}},
]


def seed_if_empty() -> None:
    if db.q1("SELECT COUNT(*) c FROM agents")["c"]:
        return
    provider = db.setting("default.provider", "ollama")
    model = db.setting("default.model", "")
    if provider == "ollama" and not model:
        found = providers.list_models("ollama")
        model = found[0] if found else "qwen2.5:latest"
    for spec in STARTER_AGENTS:
        db.ex("""INSERT INTO agents(id,name,role,emoji,accent,system_prompt,provider,model,
                 temperature,max_steps,grants,scopes,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,0.7,18,?,?,'idle',?,?)""",
              (db.nid(), spec["name"], spec["role"], spec["emoji"], spec["accent"],
               spec["system_prompt"], provider, model, json.dumps(spec["grants"]),
               json.dumps({"fs_roots": [str(config.WORKSPACE)], "net_allow": []}),
               db.now(), db.now()))


# --------------------------------------------------------------------- API

def api(method: str, path: str, query: dict, body: dict) -> dict:
    parts = [p for p in path.strip("/").split("/") if p]
    parts = parts[1:]  # drop 'api'
    head = parts[0] if parts else ""
    rid = parts[1] if len(parts) > 1 else ""
    sub = parts[2] if len(parts) > 2 else ""

    # -- bootstrap ---------------------------------------------------------
    if head == "bootstrap":
        return {
            "version": __version__,
            "brand": {"name": db.setting("brand.name"), "engine": db.setting("brand.engine")},
            "providers": providers.catalogue(),
            "tools": [{k: v for k, v in s.items() if k != "fn"} for s in tools.SPECS],
            "workspace": str(config.WORKSPACE),
            "home": str(config.HOME),
            "settings": {k: db.setting(k) for k in config.DEFAULTS},
            "defaults": {"grants": tools.default_grants()},
            "autonomy_levels": [{"id": k, **v} for k, v in workforce.LEVELS.items()],
            "templates": templates.catalogue(),
            "workforce": {"running": workforce.running()},
            "security": {"audit": security.verify_audit(),
                         "protected_paths": len(security.SENSITIVE_PATHS),
                         "blocked_commands": len(security.DESTRUCTIVE_COMMANDS)},
        }

    if head == "stats":
        return evaluator.fleet_stats()

    if head == "leaderboard":
        return {"agents": evaluator.leaderboard()}

    # -- providers ---------------------------------------------------------
    if head == "providers":
        if not rid:
            return {"providers": providers.catalogue()}
        if sub == "models":
            return {"models": providers.list_models(rid)}
        raise ApiError("Unknown provider route", 404)

    if head == "keys" and method == "POST":
        _require(body, "provider")
        raw = body.get("key", "").strip()
        db.set_key(body["provider"], raw)
        # The key itself never enters the record — only the fact that it moved.
        security.audit("operator", "key.set" if raw else "key.cleared",
                       {"provider": body["provider"]})
        return {"ok": True, "provider": body["provider"], "status": providers.status(body["provider"])}

    if head == "settings":
        if method == "POST":
            for k, v in body.items():
                db.set_setting(k, str(v))
            return {"ok": True}
        return {k: db.setting(k) for k in config.DEFAULTS}

    # -- agents ------------------------------------------------------------
    if head == "agents":
        if not rid:
            if method == "GET":
                rows = db.q("SELECT * FROM agents WHERE archived=0 ORDER BY created_at")
                return {"agents": [_agent_row(a) for a in rows]}
            if method == "POST":
                _require(body, "name", "provider", "model")
                aid = db.nid()
                grants = body.get("grants") or tools.default_grants()
                scopes = body.get("scopes") or {"fs_roots": [str(config.WORKSPACE)], "net_allow": []}
                db.ex("""INSERT INTO agents(id,name,role,emoji,accent,system_prompt,provider,model,
                         temperature,max_steps,grants,scopes,status,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'idle',?,?)""",
                      (aid, body["name"].strip(), body.get("role", ""), body.get("emoji", "🤖"),
                       body.get("accent", "#F5B93B"), body.get("system_prompt", ""),
                       body["provider"], body["model"], float(body.get("temperature", 0.7)),
                       int(body.get("max_steps", 18)), json.dumps(grants), json.dumps(scopes),
                       db.now(), db.now()))
                db.ex("UPDATE agents SET autonomy=?, shift=? WHERE id=?",
                      (body.get("autonomy", "supervised"), body.get("shift", "always"), aid))
                security.audit("operator", "agent.created",
                               {"id": aid, "name": body["name"], "grants": grants,
                                "autonomy": body.get("autonomy", "supervised")})
                return {"agent": _agent_row(db.q1("SELECT * FROM agents WHERE id=?", (aid,)))}

        agent = db.q1("SELECT * FROM agents WHERE id=?", (rid,))
        if not agent:
            raise ApiError("No such agent", 404)

        if sub == "inventory":
            return tools.inventory(agent)
        if sub == "scorecard":
            return evaluator.scorecard(rid)
        if sub == "runs":
            return {"runs": db.q("SELECT * FROM runs WHERE agent_id=? ORDER BY started_at DESC LIMIT 50",
                                 (rid,))}
        if sub == "memories":
            if method == "DELETE":
                db.ex("DELETE FROM memories WHERE agent_id=?", (rid,))
                return {"ok": True}
            return {"memories": db.q("SELECT * FROM memories WHERE agent_id=? ORDER BY created_at DESC",
                                     (rid,))}

        if method == "PATCH":
            allowed = {"name", "role", "emoji", "accent", "system_prompt", "provider",
                       "model", "temperature", "max_steps", "autonomy", "shift"}
            sets, args = [], []
            for k, v in body.items():
                if k in allowed:
                    sets.append(f"{k}=?")
                    args.append(v)
                elif k in ("grants", "scopes"):
                    sets.append(f"{k}=?")
                    args.append(json.dumps(v))
            if sets:
                sets.append("updated_at=?")
                args.extend([db.now(), rid])
                db.ex(f"UPDATE agents SET {','.join(sets)} WHERE id=?", tuple(args))
            return {"agent": _agent_row(db.q1("SELECT * FROM agents WHERE id=?", (rid,)))}

        if method == "DELETE":
            db.ex("UPDATE agents SET archived=1 WHERE id=?", (rid,))
            return {"ok": True}

        # An unrecognised subresource used to fall through to the GET below, so
        # POSTing to something like /api/agents/<id>/autonomy answered 200 OK
        # and changed nothing. A caller cannot tell that apart from success.
        if sub:
            raise ApiError(f"No such agent subresource '{sub}'. Editable fields go to "
                           f"PATCH /api/agents/{rid}.", 404)
        if method != "GET":
            raise ApiError(f"{method} is not supported on an agent. Use PATCH to edit "
                           "it, or DELETE to archive it.", 405)
        return {"agent": _agent_row(agent)}

    # -- tasks -------------------------------------------------------------
    if head == "tasks":
        if not rid:
            if method == "POST":
                _require(body, "title", "agent_id")
                if not db.q1("SELECT id FROM agents WHERE id=? AND archived=0", (body["agent_id"],)):
                    raise ApiError("Assigned agent does not exist")
                tid = db.nid()
                db.ex("""INSERT INTO tasks(id,title,brief,agent_id,status,priority,created_at)
                         VALUES(?,?,?,?,'queued',?,?)""",
                      (tid, body["title"].strip(), body.get("brief", ""), body["agent_id"],
                       body.get("priority", "normal"), db.now()))
                if body.get("run"):
                    engine.start(tid)
                return {"task": db.q1("SELECT * FROM tasks WHERE id=?", (tid,))}
            limit = int(query.get("limit", ["100"])[0])
            return {"tasks": db.q("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))}

        task = db.q1("SELECT * FROM tasks WHERE id=?", (rid,))
        if not task:
            raise ApiError("No such task", 404)
        if sub == "run" and method == "POST":
            if task["status"] == "running":
                raise ApiError("That task is already running")
            engine.start(rid)
            return {"ok": True}
        if sub == "cancel" and method == "POST":
            run = db.q1("SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (rid,))
            if run:
                engine.cancel(run["id"])
            return {"ok": True}
        if sub == "runs":
            return {"runs": db.q("SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC", (rid,))}
        if method == "DELETE":
            db.ex("DELETE FROM tasks WHERE id=?", (rid,))
            return {"ok": True}
        return {"task": task}

    # -- runs --------------------------------------------------------------
    if head == "runs":
        if not rid:
            return {"runs": db.q("SELECT * FROM runs ORDER BY started_at DESC LIMIT 100")}
        run = db.q1("SELECT * FROM runs WHERE id=?", (rid,))
        if not run:
            raise ApiError("No such run", 404)
        if sub == "rate" and method == "POST":
            return evaluator.rate_run(rid, float(body.get("score", 0)), body.get("notes", ""))
        if sub == "judge" and method == "POST":
            result = evaluator.judge_run(rid)
            if not result:
                raise ApiError("No judge model configured. Set one in Settings.")
            return result
        run = dict(run)
        run["transcript"] = db.jload(run["transcript"], [])
        run["evals"] = db.q("SELECT * FROM evals WHERE run_id=?", (rid,))
        run["task"] = db.q1("SELECT * FROM tasks WHERE id=?", (run["task_id"],))
        return {"run": run}

    # -- approvals ---------------------------------------------------------
    if head == "approvals":
        if not rid:
            rows = db.q("SELECT * FROM approvals WHERE state='pending' ORDER BY created_at")
            for r in rows:
                r["args"] = db.jload(r["args"], {})
            return {"approvals": rows}
        if sub == "decide" and method == "POST":
            ok = engine.decide_approval(rid, bool(body.get("approved")))
            if not ok:
                raise ApiError("That approval is no longer pending")
            return {"ok": True}

    # -- duties (standing responsibilities) --------------------------------
    if head == "duties":
        if not rid:
            if method == "POST":
                _require(body, "title", "agent_id")
                did = db.nid()
                cadence = max(1, int(body.get("cadence_minutes", 1440)))
                db.ex("""INSERT INTO duties(id,agent_id,title,brief,cadence_minutes,
                         next_run_at,active,created_at) VALUES(?,?,?,?,?,?,1,?)""",
                      (did, body["agent_id"], body["title"].strip(), body.get("brief", ""),
                       cadence, db.now() + (0 if body.get("start_now") else cadence * 60),
                       db.now()))
                security.audit("operator", "duty.created",
                               {"id": did, "title": body["title"], "cadence": cadence})
                return {"duty": db.q1("SELECT * FROM duties WHERE id=?", (did,))}
            return {"duties": db.q("SELECT * FROM duties ORDER BY created_at DESC")}
        if method == "DELETE":
            db.ex("DELETE FROM duties WHERE id=?", (rid,))
            security.audit("operator", "duty.deleted", {"id": rid})
            return {"ok": True}
        if method == "PATCH":
            if "active" in body:
                db.ex("UPDATE duties SET active=? WHERE id=?", (1 if body["active"] else 0, rid))
            if "cadence_minutes" in body:
                db.ex("UPDATE duties SET cadence_minutes=? WHERE id=?",
                      (max(1, int(body["cadence_minutes"])), rid))
            return {"duty": db.q1("SELECT * FROM duties WHERE id=?", (rid,))}
        return {"duty": db.q1("SELECT * FROM duties WHERE id=?", (rid,))}

    # -- escalations -------------------------------------------------------
    if head == "escalations":
        if not rid:
            state = query.get("state", ["open"])[0]
            rows = (db.q("SELECT * FROM escalations ORDER BY created_at DESC LIMIT 100")
                    if state == "all" else
                    db.q("SELECT * FROM escalations WHERE state=? ORDER BY created_at", (state,)))
            for r in rows:
                a = db.q1("SELECT name,emoji FROM agents WHERE id=?", (r["agent_id"],)) or {}
                r["agent_name"] = a.get("name", "?")
                r["agent_emoji"] = a.get("emoji", "")
                t = db.q1("SELECT title FROM tasks WHERE id=?", (r["task_id"],)) or {}
                r["task_title"] = t.get("title", "")
            return {"escalations": rows}
        if sub == "answer" and method == "POST":
            _require(body, "answer")
            security.audit("operator", "escalation.answered",
                           {"id": rid, "answer": body["answer"][:200]})
            return workforce.answer_escalation(rid, body["answer"])
        if sub == "dismiss" and method == "POST":
            db.ex("UPDATE escalations SET state='dismissed', resolved_at=? WHERE id=?",
                  (db.now(), rid))
            return {"ok": True}

    # -- workforce control -------------------------------------------------
    if head == "workforce":
        if rid == "start" and method == "POST":
            db.set_setting("workforce.enabled", "1")
            workforce.start()
            security.audit("operator", "workforce.started", {})
            return {"running": True}
        if rid == "stop" and method == "POST":
            db.set_setting("workforce.enabled", "0")
            workforce.stop()
            security.audit("operator", "workforce.stopped", {})
            return {"running": False}
        return {"running": workforce.running(),
                "queued": db.q1("SELECT COUNT(*) c FROM tasks WHERE status='queued'")["c"],
                "active": db.q1("SELECT COUNT(*) c FROM runs WHERE status='running'")["c"],
                "open_escalations": db.q1(
                    "SELECT COUNT(*) c FROM escalations WHERE state='open'")["c"]}

    # -- security ----------------------------------------------------------
    if head == "security":
        if rid == "audit":
            limit = int(query.get("limit", ["200"])[0])
            return {"chain": security.verify_audit(),
                    "entries": db.q("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))}
        if rid == "rotate-token" and method == "POST":
            return {"token": security.rotate_token()}
        return {"chain": security.verify_audit(),
                "protected_paths": security.SENSITIVE_PATHS,
                "blocked_commands": [{"pattern": p, "why": w}
                                     for p, w in security.DESTRUCTIVE_COMMANDS],
                "caps": {"per_run": db.setting("safety.max_cost_per_run"),
                         "per_day": db.setting("safety.max_cost_per_day")},
                "spent_24h": round(db.q1(
                    "SELECT COALESCE(SUM(cost),0) c FROM runs WHERE started_at>?",
                    (db.now() - 86400,))["c"], 4)}

    # -- templates ---------------------------------------------------------
    if head == "templates":
        if not rid:
            return {"templates": templates.catalogue()}
        tpl = templates.BY_ID.get(rid)
        if not tpl:
            raise ApiError("No such template", 404)
        if sub == "hire" and method == "POST":
            name = (body.get("name") or tpl["name"]).strip()
            if not name:
                raise ApiError("Give the agent a name.")
            if db.q1("SELECT id FROM agents WHERE lower(name)=lower(?) AND archived=0", (name,)):
                raise ApiError(f"You already have an agent called {name}.")
            provider = body.get("provider") or db.setting("default.provider", "ollama")
            model = body.get("model")
            if not model:
                found = providers.list_models(provider)
                model = found[0] if found else ""
            if not model:
                raise ApiError(f"No model available for {provider}. Set it up in Settings first.")
            aid = db.nid()
            grants = tpl["grants"] or tools.default_grants()
            db.ex("""INSERT INTO agents(id,name,role,emoji,accent,system_prompt,provider,model,
                     temperature,max_steps,grants,scopes,status,created_at,updated_at,
                     autonomy,shift)
                     VALUES(?,?,?,?,?,?,?,?,0.7,18,?,?,'idle',?,?,?,'always')""",
                  (aid, name, tpl["role"], tpl["emoji"], tpl["accent"], tpl["system_prompt"],
                   provider, model, json.dumps(grants),
                   json.dumps({"fs_roots": [str(config.WORKSPACE)], "net_allow": []}),
                   db.now(), db.now(),
                   body.get("autonomy") or tpl["autonomy"]))
            duties = []
            if body.get("with_duties", True):
                for d in tpl["duties"]:
                    did = db.nid()
                    db.ex("""INSERT INTO duties(id,agent_id,title,brief,cadence_minutes,
                             next_run_at,active,created_at) VALUES(?,?,?,?,?,?,1,?)""",
                          (did, aid, d["title"], d["brief"], d["cadence_minutes"],
                           db.now() + d["cadence_minutes"] * 60, db.now()))
                    duties.append(d["title"])
            security.audit("operator", "agent.hired",
                           {"id": aid, "name": name, "template": rid,
                            "autonomy": body.get("autonomy") or tpl["autonomy"],
                            "duties": duties})
            return {"agent": _agent_row(db.q1("SELECT * FROM agents WHERE id=?", (aid,))),
                    "duties": duties,
                    "needs_email": tpl.get("needs_email", False)}
        return {"template": tpl}

    # -- email account -----------------------------------------------------
    if head == "email":
        from .runtime import mail
        if rid == "presets":
            return {"presets": [{"id": k, **{kk: vv for kk, vv in v.items()}} for k, v in mail.PRESETS.items()]}
        if rid == "connect" and method == "POST":
            _require(body, "address", "password", "imap_host", "smtp_host")
            db.set_setting("email.address", body["address"].strip())
            db.set_setting("email.password", config.encrypt(body["password"]))
            for f in ("imap_host", "smtp_host", "imap_port", "smtp_port"):
                if body.get(f):
                    db.set_setting(f"email.{f}", str(body[f]).strip())
            db.set_setting("email.allowed_recipients", body.get("allowed_recipients", ""))
            security.audit("operator", "email.connected",
                           {"address": body["address"], "imap": body["imap_host"]})
            try:
                probe = mail.t_email_list({"id": "probe"}, {"limit": 1}, {})
                return {"ok": True, "detail": "Connected. " + probe.split("\n")[0]}
            except Exception as e:
                return {"ok": False, "detail": str(e)}
        if rid == "disconnect" and method == "POST":
            for f in ("address", "password", "imap_host", "smtp_host"):
                db.set_setting(f"email.{f}", "")
            security.audit("operator", "email.disconnected", {})
            return {"ok": True}
        return {"configured": mail.configured(),
                "address": db.setting("email.address", ""),
                "imap_host": db.setting("email.imap_host", ""),
                "smtp_host": db.setting("email.smtp_host", ""),
                "imap_port": db.setting("email.imap_port", "993"),
                "smtp_port": db.setting("email.smtp_port", "465"),
                "allowed_recipients": db.setting("email.allowed_recipients", "")}

    # -- ollama convenience ------------------------------------------------
    if head == "ollama" and rid == "pull" and method == "POST":
        _require(body, "model")
        threading.Thread(target=_pull_ollama, args=(body["model"],), daemon=True).start()
        return {"ok": True, "pulling": body["model"]}

    raise ApiError(f"Unknown endpoint: {method} {path}", 404)


def _pull_ollama(model: str) -> None:
    import subprocess
    bus.emit("ollama_pull", {"model": model, "state": "started"})
    try:
        r = subprocess.run(["ollama", "pull", model], capture_output=True, text=True, timeout=3600)
        ok = r.returncode == 0
        bus.emit("ollama_pull", {"model": model, "state": "done" if ok else "failed",
                                 "detail": (r.stderr or r.stdout)[-400:]})
    except Exception as e:
        bus.emit("ollama_pull", {"model": model, "state": "failed", "detail": str(e)})


# ----------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = f"Hermes/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet; the console is for Hermes' own output

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        allowed = set(ALLOWED_HOSTS)
        if BIND_HOST not in ("127.0.0.1", "localhost"):
            allowed.add(BIND_HOST)
            extra = db.setting("server.allowed_hosts", "")
            allowed.update(h.strip() for h in extra.split(",") if h.strip())
        if host not in allowed and "*" not in allowed:
            self._send(403, {"error": f"Refused: '{host}' is not an allowed Host. "
                                      "Add it to server.allowed_hosts in Settings."})
            return False

        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return True  # static assets carry no data

        ip = self.client_address[0] if self.client_address else "?"
        wait = _throttled(ip)
        if wait > 0:
            self._send(429, {"error": f"Too many failed attempts. Try again in {int(wait)}s."})
            return False

        supplied = (self.headers.get("X-Hermes-Token")
                    or parse_qs(parsed.query).get("token", [""])[0])
        if not security.check_token(supplied):
            _record_fail(ip)
            self._send(401, {"error": "Unauthorised. Open Hermes from the link printed "
                                      "in your terminal, or paste your session token."})
            return False
        with _auth_lock:
            _auth_fails.pop(ip, None)
        return True

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = rel.split("?")[0]
        target = (config.WEB_DIR / rel).resolve()
        if not str(target).startswith(str(config.WEB_DIR.resolve())) or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = bus.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(q)

    def _handle(self, method: str) -> None:
        if not self._guard():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/events":
            self._stream()
            return
        if not parsed.path.startswith("/api/"):
            if method == "GET":
                self._static(parsed.path)
            else:
                self.send_error(405)
            return
        body = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode() or "{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "Request body was not valid JSON"})
                return
        try:
            self._send(200, api(method, parsed.path, parse_qs(parsed.query), body))
        except ApiError as e:
            self._send(e.code, {"error": str(e)})
        except providers.ProviderError as e:
            self._send(502, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


def serve(port: int = config.DEFAULT_PORT, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    global BIND_HOST
    BIND_HOST = host
    db.init()
    security.init_audit()
    security.session_token()
    seed_if_empty()
    if db.setting("workforce.enabled", "1") == "1":
        workforce.start()
    security.audit("system", "server.started", {"port": port, "host": host})
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
