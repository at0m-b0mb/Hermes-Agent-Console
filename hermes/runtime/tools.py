"""OpenClaw tool surface.

Every capability an agent can reach lives here, and nothing an agent does
bypasses this module. Each tool declares a `danger` level which drives the
default grant mode, so a newly created agent is safe by default: reads are
allowed, writes and shell ask first, and nothing is silently destructive.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import config, db, security

ALLOW, ASK, DENY = "allow", "ask", "deny"


class ToolError(RuntimeError):
    pass


class Denied(RuntimeError):
    pass


# ------------------------------------------------------------------ sandbox

def _roots(agent: dict) -> list[Path]:
    scopes = db.jload(agent.get("scopes"), {})
    raw = scopes.get("fs_roots") or [str(config.WORKSPACE)]
    out = []
    for r in raw:
        try:
            out.append(Path(os.path.expanduser(r)).resolve())
        except OSError:
            continue
    return out or [config.WORKSPACE.resolve()]


def _safe_path(agent: dict, path: str) -> Path:
    roots = _roots(agent)
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = roots[0] / p
    try:
        p = p.resolve()
    except OSError as e:
        raise ToolError(f"Bad path: {e}") from None
    for root in roots:
        if p == root or root in p.parents:
            security.guard_path(p)
            return p
    allowed = ", ".join(str(r) for r in roots)
    raise Denied(f"Path '{p}' is outside this agent's filesystem scope. Allowed: {allowed}")


def _check_host(agent: dict, url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise Denied("Only http/https URLs are permitted.")
    allow = db.jload(agent.get("scopes"), {}).get("net_allow") or []
    if allow and not any(parsed.hostname == d or (parsed.hostname or "").endswith("." + d)
                         for d in allow):
        raise Denied(f"Host '{parsed.hostname}' is not in this agent's network allowlist: {allow}")
    return url


# -------------------------------------------------------------------- tools

def t_read_file(agent, args, ctx):
    p = _safe_path(agent, args["path"])
    if not p.is_file():
        raise ToolError(f"Not a file: {p}")
    if p.stat().st_size > 400_000:
        raise ToolError(f"File too large ({p.stat().st_size} bytes). Read a smaller file.")
    return p.read_text(errors="replace")


def t_write_file(agent, args, ctx):
    p = _safe_path(agent, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(args.get("content", ""))
    return f"{'Overwrote' if existed else 'Created'} {p} ({len(args.get('content', ''))} bytes)"


def t_list_dir(agent, args, ctx):
    p = _safe_path(agent, args.get("path", "."))
    if not p.is_dir():
        raise ToolError(f"Not a directory: {p}")
    rows = []
    for item in sorted(p.iterdir())[:300]:
        kind = "dir " if item.is_dir() else "file"
        size = "" if item.is_dir() else f"  {item.stat().st_size}b"
        rows.append(f"{kind}  {item.name}{size}")
    return f"{p}\n" + ("\n".join(rows) or "(empty)")


def t_search_files(agent, args, ctx):
    root = _safe_path(agent, args.get("path", "."))
    raw = str(args["query"])
    needle = raw.lower()
    # A query like "*.md" is a filename pattern, not text to grep for. Models
    # reach for this constantly, and answering "no matches" sends them off to
    # invent a workaround — one of them wrote "no markdown files found" over a
    # folder full of markdown. So: globs search names, plain queries search
    # content *and* names.
    is_glob = any(ch in raw for ch in "*?[")
    hits = []
    named = 0
    skipped = 0

    def _footer(body):
        note = (f"\n[{skipped} protected file(s) skipped — credential and key material is "
                "excluded from search the same way it is excluded from read_file]") if skipped else ""
        return body + note

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "__", "node_modules"))]
        for fn in filenames:
            fp = Path(dirpath) / fn
            # A grep is a read. Protected material is protected here too, or
            # the hard floor would only be a floor for one of the two tools
            # that can put a file's contents in front of the model.
            try:
                security.guard_path(fp)
            except security.SecurityViolation:
                skipped += 1
                continue
            if fnmatch.fnmatch(fn.lower(), needle) if is_glob else needle in fn.lower():
                hits.append(f"{fp}  (filename match)")
                named += 1
                if len(hits) >= 80:
                    return _footer("\n".join(hits) + "\n… (truncated at 80 matches)")
            if is_glob:
                continue          # a glob is about names; grepping for it is noise
            try:
                if fp.stat().st_size > 2_000_000:
                    continue
                for i, line in enumerate(fp.read_text(errors="ignore").splitlines(), 1):
                    if needle in line.lower():
                        hits.append(f"{fp}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 80:
                            return _footer("\n".join(hits) + "\n… (truncated at 80 matches)")
            except (OSError, UnicodeDecodeError):
                continue
    if not hits:
        return _footer(f"No matches for '{raw}' under {root}")
    head = (f"{len(hits)} match(es) for '{raw}' under {root}"
            + (f", {named} by filename" if named else "") + "\n")
    return _footer(head + "\n".join(hits))


def t_run_shell(agent, args, ctx):
    cmd = args["command"]
    security.guard_command(cmd)
    cwd = _roots(agent)[0]
    timeout = int(db.setting("safety.shell_timeout", "60"))
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command timed out after {timeout}s") from None
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    out = out.strip() or "(no output)"
    if len(out) > 20_000:
        out = out[:20_000] + "\n… (truncated)"
    return f"exit={r.returncode}\n{out}"


class _AllowlistedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check the allowlist on every hop, not just the first one.

    Checking only the URL the agent typed is checking the wrong thing: a page
    on an allowed domain can answer with a 302 to anywhere, and the fetch that
    actually happens is the one at the end of the chain.
    """

    def __init__(self, agent):
        self.agent = agent

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(self.agent, urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def t_http_fetch(agent, args, ctx):
    url = _check_host(agent, args["url"])
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-OpenClaw/1.0"})
    opener = urllib.request.build_opener(_AllowlistedRedirects(agent))
    try:
        with opener.open(req, timeout=30) as r:
            body = r.read(500_000).decode(errors="replace")
            final = r.geturl()
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} fetching {url}") from None
    except urllib.error.URLError as e:
        raise ToolError(f"Could not fetch {url}: {e.reason}") from None
    source = f"web page {final}" if final != url else f"web page {url}"
    return security.wrap_untrusted(body[:40_000], source)


def t_remember(agent, args, ctx):
    db.ex("INSERT INTO memories(id,agent_id,key,value,created_at) VALUES(?,?,?,?,?)",
          (db.nid(), agent["id"], args["key"], args["value"], db.now()))
    return f"Stored memory '{args['key']}'."


def t_recall(agent, args, ctx):
    rows = db.q("SELECT key,value FROM memories WHERE agent_id=? ORDER BY created_at DESC LIMIT 40",
                (agent["id"],))
    if not rows:
        return "No memories stored yet."
    return "\n".join(f"- {r['key']}: {r['value']}" for r in rows)


def t_delegate(agent, args, ctx):
    target = db.q1("SELECT * FROM agents WHERE (name=? OR id=?) AND archived=0",
                   (args["agent"], args["agent"]))
    if not target:
        names = [a["name"] for a in db.q("SELECT name FROM agents WHERE archived=0")]
        raise ToolError(f"No agent named '{args['agent']}'. Available: {', '.join(names) or 'none'}")
    if target["id"] == agent["id"]:
        raise ToolError("An agent cannot delegate to itself.")
    tid = db.nid()
    db.ex("""INSERT INTO tasks(id,title,brief,agent_id,parent_task_id,status,priority,created_at)
             VALUES(?,?,?,?,?,'queued','normal',?)""",
          (tid, args["title"], args.get("brief", ""), target["id"], ctx.get("task_id"), db.now()))
    return f"Delegated to {target['name']}. Task {tid} queued — it will not block this run."


def t_escalate(agent, args, ctx):
    """The employee equivalent of knocking on the manager's door."""
    eid = db.nid()
    db.ex("""INSERT INTO escalations(id,task_id,run_id,agent_id,reason,question,state,created_at)
             VALUES(?,?,?,?,?,?,'open',?)""",
          (eid, ctx.get("task_id"), ctx.get("run_id"), agent["id"],
           args.get("reason", ""), args.get("question", ""), db.now()))
    from . import bus
    bus.emit("escalation", {"id": eid, "agent": agent["name"],
                            "reason": args.get("reason", ""),
                            "question": args.get("question", "")},
             run_id=ctx.get("run_id", ""), task_id=ctx.get("task_id", ""), agent_id=agent["id"])
    return ("Escalated to the operator. You are now blocked on a human answer — "
            "call finish immediately and state clearly what you need to proceed.")


def t_plan(agent, args, ctx):
    """Break a large objective into queued work the agent picks up afterwards."""
    steps = args.get("steps") or []
    if isinstance(steps, str):
        steps = [line.strip("-* ") for line in steps.splitlines() if line.strip()]
    if not steps:
        raise ToolError("plan requires a non-empty 'steps' list.")
    created = []
    for step in steps[:12]:
        title = step if isinstance(step, str) else str(step.get("title", ""))
        brief = "" if isinstance(step, str) else str(step.get("brief", ""))
        if not title.strip():
            continue
        tid = db.nid()
        db.ex("""INSERT INTO tasks(id,title,brief,agent_id,parent_task_id,status,priority,
                 created_at,source) VALUES(?,?,?,?,?,'queued','normal',?,'plan')""",
              (tid, title[:160], brief, agent["id"], ctx.get("task_id"), db.now()))
        created.append(title[:80])
    return ("Queued for yourself, and you will pick them up automatically:\n"
            + "\n".join(f"  {i}. {t}" for i, t in enumerate(created, 1)))


def t_finish(agent, args, ctx):
    return args.get("summary", "")


SPECS = [
    {"name": "read_file", "fn": t_read_file, "required": ["path"], "group": "Filesystem", "danger": "low",
     "desc": "Read a text file inside your filesystem scope.",
     "params": {"path": "path to the file"}},
    {"name": "list_dir", "fn": t_list_dir, "required": [], "group": "Filesystem", "danger": "low",
     "desc": "List the contents of a directory.",
     "params": {"path": "directory path (default '.')"}},
    {"name": "search_files", "fn": t_search_files, "required": ["query"], "group": "Filesystem", "danger": "low",
     "desc": "Find files under a directory. A plain query searches file contents and "
             "names; a glob like '*.md' searches names only.",
     "params": {"query": "text to find, or a filename glob like *.md",
                "path": "root to search (default '.')"}},
    {"name": "write_file", "fn": t_write_file, "required": ["path", "content"], "group": "Filesystem", "danger": "high",
     "desc": "Create or overwrite a file. Overwrites without warning.",
     "params": {"path": "path to write", "content": "full file contents"}},
    {"name": "run_shell", "fn": t_run_shell, "required": ["command"], "group": "System", "danger": "critical",
     "desc": "Run a shell command in your working directory.",
     "params": {"command": "the shell command"}},
    {"name": "http_fetch", "fn": t_http_fetch, "required": ["url"], "group": "Network", "danger": "medium",
     "desc": "Fetch a URL and return the response body.",
     "params": {"url": "http(s) URL"}},
    {"name": "remember", "fn": t_remember, "required": ["key", "value"], "group": "Memory", "danger": "low",
     "desc": "Save a durable note you will still have on future tasks.",
     "params": {"key": "short label", "value": "what to remember"}},
    {"name": "recall", "fn": t_recall, "required": [], "group": "Memory", "danger": "low",
     "desc": "List everything you have remembered.", "params": {}},
    {"name": "delegate", "fn": t_delegate, "required": ["agent", "title"], "group": "Team", "danger": "medium",
     "desc": "Queue a subtask for a different agent. Does not block you.",
     "params": {"agent": "target agent name", "title": "task title", "brief": "what they must do"}},
    {"name": "escalate", "fn": t_escalate, "required": ["question"], "group": "Control", "danger": "low",
     "desc": "Ask the operator a question when you are genuinely blocked. Use sparingly.",
     "params": {"reason": "why you are blocked", "question": "the exact question you need answered"}},
    {"name": "plan", "fn": t_plan, "required": ["steps"], "group": "Control", "danger": "low",
     "desc": "Split a large objective into steps queued as your own follow-up tasks.",
     "params": {"steps": "list of step titles"}},
    {"name": "finish", "fn": t_finish, "required": [], "group": "Control", "danger": "low",
     "desc": "End the task and report your result. Always call this last.",
     "params": {"summary": "your complete answer or report"}},
]

# Email lives in its own module but shares one permission surface.
from . import mail  # noqa: E402
SPECS.extend(mail.SPECS)

BY_NAME = {s["name"]: s for s in SPECS}

DEFAULT_GRANTS = {
    "low": ALLOW,
    "medium": ASK,
    "high": ASK,
    "critical": ASK,
}


def default_grants() -> dict:
    g = {s["name"]: DEFAULT_GRANTS[s["danger"]] for s in SPECS}
    for always_on in ("finish", "escalate", "plan"):
        g[always_on] = ALLOW
    # Nothing that sends mail is on by default. You turn it on deliberately.
    for name in ("email_send",):
        g[name] = DENY
    return g


def grant_of(agent: dict, tool: str) -> str:
    grants = db.jload(agent.get("grants"), {})
    return grants.get(tool, DENY)


def granted_tools(agent: dict) -> list[dict]:
    """The agent's live capability inventory — what it can touch, and how."""
    grants = db.jload(agent.get("grants"), {})
    scopes = db.jload(agent.get("scopes"), {})
    out = []
    for s in SPECS:
        mode = grants.get(s["name"], DENY)
        out.append({
            "name": s["name"], "group": s["group"], "danger": s["danger"],
            "desc": s["desc"], "params": s["params"], "mode": mode,
            # The console must not present "allow" as though it removed the
            # approval step for these — it does not, and cannot.
            "human_only": security.requires_human(s["name"]),
        })
    return out


def inventory(agent: dict) -> dict:
    """Everything this agent has access to, in one payload."""
    scopes = db.jload(agent.get("scopes"), {})
    tools = granted_tools(agent)
    mem = db.q("SELECT key,value,created_at FROM memories WHERE agent_id=? ORDER BY created_at DESC",
               (agent["id"],))
    return {
        "tools": tools,
        "counts": {
            "allow": sum(1 for t in tools if t["mode"] == ALLOW),
            "ask": sum(1 for t in tools if t["mode"] == ASK),
            "deny": sum(1 for t in tools if t["mode"] == DENY),
        },
        "fs_roots": scopes.get("fs_roots") or [str(config.WORKSPACE)],
        "net_allow": scopes.get("net_allow") or [],
        "memories": mem,
        "model": {"provider": agent.get("provider"), "model": agent.get("model")},
    }


def render_tool_docs(agent: dict) -> str:
    """The tool manual injected into the agent's system prompt."""
    lines = []
    for s in SPECS:
        mode = grant_of(agent, s["name"])
        if mode == DENY:
            continue
        required = s.get("required", ())
        params = ", ".join(f'"{k}": <{v}>' if k in required else f'["{k}": <{v}>]'
                           for k, v in s["params"].items())
        gate = "  (asks the operator for approval first)" if mode == ASK else ""
        lines.append(f'- {s["name"]}: {s["desc"]}{gate}\n  args: {{{params}}}')
    return "\n".join(lines)


def execute(agent: dict, tool: str, args: dict, ctx: dict) -> str:
    spec = BY_NAME.get(tool)
    if not spec:
        raise ToolError(f"No such tool '{tool}'. Available: {', '.join(BY_NAME)}")
    mode = grant_of(agent, tool)
    if mode == DENY:
        raise Denied(f"Tool '{tool}' is not granted to this agent.")

    # The last gate before anything irreversible leaves the building. The
    # caller is expected to have obtained a human decision, but this is the
    # chokepoint every tool call passes through, so the guarantee is enforced
    # here rather than trusted to whoever is driving the loop.
    if security.requires_human(tool) and not ctx.get("human_approved"):
        raise security.SecurityViolation(
            f"Blocked: '{tool}' is irreversible and outward-facing, so it requires an "
            "explicit human approval for this specific call. No autonomy level and no "
            "capability grant can stand in for that.")

    # Each spec names the arguments it genuinely cannot run without. Anything
    # else in `params` is optional and has a documented default — treating the
    # whole params dict as mandatory made every tool with an optional argument
    # impossible for an agent to call.
    missing = [k for k in spec.get("required", ()) if k not in args]
    if missing:
        raise ToolError(f"Missing required argument(s) for {tool}: {', '.join(missing)}")

    base = {"agent": agent["name"], "agent_id": agent["id"], "tool": tool,
            "args": args, "run_id": ctx.get("run_id"), "task_id": ctx.get("task_id"),
            "mode": mode, "danger": spec["danger"]}
    try:
        result = spec["fn"](agent, args, ctx)
    except security.SecurityViolation as e:
        security.audit(agent["name"], "tool.blocked", {**base, "reason": str(e)})
        raise
    except mail.MailError as e:
        security.audit(agent["name"], "tool.error", {**base, "error": str(e)[:400]})
        raise ToolError(str(e)) from None
    except Exception as e:
        security.audit(agent["name"], "tool.error", {**base, "error": str(e)[:400]})
        raise
    security.audit(agent["name"], "tool.executed",
                   {**base, "output_bytes": len(str(result))})
    # Secrets never make it back into the transcript or the next prompt.
    return security.redact(str(result))
