"""Hermes command line."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
import threading
import time
import webbrowser

from . import __engine__, __version__, config, db, providers, security
from .runtime import engine, evaluator, tools, workforce

GOLD = "\033[38;5;179m"
INDIGO = "\033[38;5;62m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"
GREEN = "\033[38;5;79m"
RED = "\033[38;5;167m"

BANNER = rf"""{GOLD}
  ╦ ╦ ╔═╗ ╦═╗ ╔╦╗ ╔═╗ ╔═╗
  ╠═╣ ║╣  ╠╦╝ ║║║ ║╣  ╚═╗
  ╩ ╩ ╚═╝ ╩╚═ ╩ ╩ ╚═╝ ╚═╝{OFF}
  {DIM}Agent Operations Console · {__engine__} runtime v{__version__}{OFF}
"""


def _seed() -> None:
    """Put the starter agents in place on first use.

    They used to arrive only when the console or the shell started, so a fresh
    install answered `hermes run Forge ...` with "no agent named Forge" — the
    exact command the README opens with.
    """
    from .server import seed_if_empty
    seed_if_empty()


def cmd_serve(args) -> int:
    db.init()
    from . import server
    host = getattr(args, "host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost") and not getattr(args, "i_understand_the_risk", False):
        print(f"\n  {RED}Refusing to bind {host}.{OFF}\n")
        print(f"  Binding beyond localhost exposes your agents — and the machine they run on —")
        print(f"  to your network. Hermes has token auth and rate limiting, but it has")
        print(f"  {BOLD}no TLS of its own{OFF}, so the token would cross the wire in plaintext.\n")
        print(f"  {BOLD}Do this instead:{OFF}")
        print(f"    · Keep Hermes on localhost and reach it over SSH:")
        print(f"      {INDIGO}ssh -N -L 4317:localhost:4317 you@server{OFF}")
        print(f"    · Or put a TLS reverse proxy (Caddy, nginx) in front of localhost:{args.port}\n")
        print(f"  {DIM}If you have TLS terminated in front and still want this, re-run with{OFF}")
        print(f"  {DIM}--host {host} --i-understand-the-risk{OFF}\n")
        return 2
    try:
        httpd = server.serve(args.port, host)
    except OSError as e:
        print(f"{RED}Cannot bind port {args.port}: {e}{OFF}")
        print(f"{DIM}Another Hermes may already be running. Try: hermes serve --port {args.port + 1}{OFF}")
        return 1
    token = security.session_token()
    url = f"http://localhost:{args.port}"
    print(BANNER)
    print(f"  {BOLD}Console{OFF}    {INDIGO}{url}/?token={token}{OFF}")
    print(f"  {BOLD}Workspace{OFF}  {DIM}{config.WORKSPACE}{OFF}")
    print(f"  {BOLD}Data{OFF}       {DIM}{config.DB_PATH}{OFF}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"  {RED}Bound to {host} — make sure TLS terminates in front of this.{OFF}")
    print()
    ready = [p for p in providers.catalogue() if p["ok"]]
    if ready:
        print(f"  {GREEN}●{OFF} Ready: " + ", ".join(p["label"] for p in ready))
    else:
        print(f"  {RED}●{OFF} No backend configured yet — set one up in Settings.")
    if workforce.running():
        print(f"  {GREEN}●{OFF} Workforce dispatcher active — agents pick up their own queue.")
    else:
        print(f"  {DIM}○ Workforce dispatcher paused.{OFF}")
    chain = security.verify_audit()
    seal = "intact" if chain["ok"] else f"BROKEN at #{chain.get('broken_at')}"
    print(f"  {GREEN if chain['ok'] else RED}●{OFF} Audit chain {seal} "
          f"{DIM}({chain['entries']} entries){OFF}")
    print(f"\n  {DIM}Ctrl-C to stop.{OFF}\n")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"{url}/?token={token}")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n  {DIM}Hermes stopped.{OFF}")
        httpd.shutdown()
    return 0


def cmd_doctor(args) -> int:
    db.init()
    _seed()
    print(BANNER)
    print(f"  {BOLD}Python{OFF}      {sys.version.split()[0]}")
    print(f"  {BOLD}Home{OFF}        {config.HOME}")
    print(f"  {BOLD}Database{OFF}    {config.DB_PATH} "
          f"({'exists' if config.DB_PATH.exists() else 'will be created'})")
    print(f"\n  {BOLD}Backends{OFF}")
    for p in providers.catalogue():
        dot = f"{GREEN}●{OFF}" if p["ok"] else f"{RED}○{OFF}"
        print(f"   {dot} {p['label']:<26} {DIM}{p['detail']}{OFF}")
        if p["ok"]:
            models = providers.list_models(p["id"])[:4]
            if models:
                print(f"     {DIM}models: {', '.join(models)}{OFF}")
    agents = db.q("SELECT * FROM agents WHERE archived=0")
    print(f"\n  {BOLD}Agents{OFF}      {len(agents)} configured")
    for a in agents:
        card = evaluator.scorecard(a["id"])
        score = f"{card['avg_score']}" if card["avg_score"] is not None else "—"
        print(f"   {a['emoji']} {a['name']:<12} {DIM}{a['provider']}/{a['model']}  "
              f"runs={card['runs']} score={score}{OFF}")

    _print_security_posture()
    return 0


def _print_security_posture() -> None:
    """What is actually protecting this install, and what is not.

    Most of the security model is fixed in code and needs no checking. What
    varies is the deployment: where it is bound, who can reach it, how wide the
    agents' scopes are. Those are the questions worth answering out loud, so
    nobody discovers the answer by being surprised.
    """
    from .runtime import tools

    print(f"\n  {BOLD}Security{OFF}")

    def row(ok, label, detail):
        dot = f"{GREEN}●{OFF}" if ok else f"{GOLD}!{OFF}"
        print(f"   {dot} {label:<26} {DIM}{detail}{OFF}")

    secret_mode = oct(config.SECRET_PATH.stat().st_mode & 0o777)[2:] if config.SECRET_PATH.exists() else None
    home_mode = oct(config.HOME.stat().st_mode & 0o777)[2:] if config.HOME.exists() else None
    row(secret_mode in (None, "600"), "Key vault permissions",
        f"secret.key is {secret_mode or 'not created yet'}"
        + ("" if secret_mode in (None, "600") else " — should be 600"))
    row(home_mode in (None, "700"), "Home directory",
        f"{config.HOME} is {home_mode or 'not created yet'}"
        + ("" if home_mode in (None, "700") else " — should be 700"))

    allowed = db.setting("server.allowed_hosts", "")
    row("*" not in allowed, "Host allowlist",
        "any Host accepted — DNS rebinding is possible" if "*" in allowed
        else (allowed or "loopback only"))

    proxy = db.setting("server.trusted_proxy", "")
    print(f"   {DIM}·{OFF} {'Trusted proxy':<26} "
          f"{DIM}{proxy or 'none — X-Forwarded-For is ignored, which is right '
                          'unless a proxy is in front'}{OFF}")

    wide, shell_agents = [], []
    home = str(Path.home())
    for a in db.q("SELECT * FROM agents WHERE archived=0"):
        roots = db.jload(a["scopes"], {}).get("fs_roots") or [str(config.WORKSPACE)]
        for r in roots:
            expanded = os.path.expanduser(r)
            if expanded in ("/", home) or expanded.rstrip("/") == home:
                wide.append(f"{a['name']} → {r}")
        if tools.grant_of(a, "run_shell") != tools.DENY:
            shell_agents.append(f"{a['name']} ({tools.grant_of(a, 'run_shell')})")
    row(not wide, "Filesystem scopes",
        "; ".join(wide) + " — very wide" if wide else "all agents scoped to a folder")
    row(not shell_agents, "Shell access",
        ", ".join(shell_agents) + " — can do anything your user can"
        if shell_agents else "no agent may run commands")

    chain = security.verify_audit()
    row(chain["ok"], "Audit chain",
        f"intact · {chain['entries']} entries" if chain["ok"]
        else f"BROKEN at entry {chain.get('broken_at')} — {chain.get('reason')}")

    print(f"\n   {DIM}Enforced in code and not configurable: "
          f"{len(security.SENSITIVE_PATHS)} protected path patterns, "
          f"{len(security.DESTRUCTIVE_COMMANDS)} blocked command patterns, "
          f"a human on every outgoing email.{OFF}")
    print(f"   {DIM}Exposing this beyond localhost: see \"Running on a server\" "
          f"in the README. Put TLS in front; Hermes has none.{OFF}")
    return 0


def cmd_audit(args) -> int:
    db.init()
    security.init_audit()
    chain = security.verify_audit()
    print(BANNER)
    if chain["ok"]:
        print(f"  {GREEN}● Audit chain intact{OFF} {DIM}· {chain['entries']} entries · "
              f"head {chain['head']}{OFF}\n")
    else:
        print(f"  {RED}● AUDIT CHAIN BROKEN at entry #{chain['broken_at']}{OFF}")
        print(f"    {chain['reason']}\n")
    for row in reversed(db.q("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (args.limit,))):
        import datetime
        ts = datetime.datetime.fromtimestamp(row["ts"]).strftime("%m-%d %H:%M:%S")
        print(f"  {DIM}{ts}{OFF}  {row['actor']:<10} {INDIGO}{row['action']:<22}{OFF} "
              f"{DIM}{row['detail'][:90]}{OFF}")
    return 0 if chain["ok"] else 1


def cmd_key(args) -> int:
    db.init()
    if args.provider not in providers.PROVIDERS:
        print(f"{RED}Unknown provider '{args.provider}'.{OFF} "
              f"Choose from: {', '.join(providers.PROVIDERS)}")
        return 1
    raw = args.value or getpass.getpass(f"API key for {args.provider} (hidden): ")
    db.set_key(args.provider, raw.strip())
    security.audit("operator", "key.set" if raw.strip() else "key.cleared",
                   {"provider": args.provider})
    st = providers.status(args.provider)
    dot = f"{GREEN}●{OFF}" if st["ok"] else f"{RED}○{OFF}"
    print(f"  {dot} {args.provider}: {st['detail']}  {DIM}(encrypted in {config.HOME}){OFF}")
    return 0


def cmd_agents(args) -> int:
    db.init()
    _seed()
    for a in db.q("SELECT * FROM agents WHERE archived=0 ORDER BY created_at"):
        inv = tools.inventory(a)
        print(f"\n  {a['emoji']} {BOLD}{a['name']}{OFF} {DIM}({a['id']}){OFF}")
        print(f"     role     {a['role'] or '—'}")
        print(f"     model    {a['provider']}/{a['model']}")
        print(f"     tools    {inv['counts']['allow']} allowed · "
              f"{inv['counts']['ask']} ask-first · {inv['counts']['deny']} blocked")
        print(f"     scope    {', '.join(inv['fs_roots'])}")
        level = workforce.LEVELS.get(a.get("autonomy") or "supervised", {})
        print(f"     autonomy {level.get('label', '?')} {DIM}— {level.get('blurb', '')}{OFF}")
    return 0


def _approvals_on_the_terminal(pending, done, args) -> None:
    """Ask for approvals here rather than sending the operator to the console.

    A supervised agent asks before every write, so `hermes run` used to sit in
    silence for the full fifteen-minute approval timeout with no way to say yes
    — the console was the only place a decision could be made. This is the same
    prompt the interactive shell already used.
    """
    auto = getattr(args, "yes", False)
    while not done.is_set():
        time.sleep(0.25)
        if not pending:
            continue
        p = pending.pop(0)
        row = db.q1("SELECT * FROM approvals WHERE id=? AND state='pending'", (p["id"],))
        if not row:
            continue

        print(f"\n  {GOLD}{BOLD}⏸ {p['agent']} needs approval to run {p['tool']}{OFF}")
        detail = json.dumps(p.get("args") or {}, indent=2)
        print(f"  {DIM}{detail[:600]}{OFF}")

        # --yes is the operator pre-authorising, and it deliberately stops short
        # of the outward-facing actions: those are the ones a human is required
        # to see, and a flag typed minutes earlier is not seeing them.
        if auto and security.blanket_approval_covers(p["tool"]):
            engine.decide_approval(p["id"], True)
            print(f"  {GREEN}approved{OFF} {DIM}(--yes){OFF}")
            continue
        if auto:
            print(f"  {GOLD}--yes does not cover {p['tool']}: it goes outside this "
                  f"machine, so it always asks.{OFF}")

        if not sys.stdin.isatty():
            engine.decide_approval(p["id"], False)
            print(f"  {RED}denied{OFF} {DIM}(no terminal to ask on — "
                  f"use --yes, or run it from the console){OFF}")
            continue
        try:
            ans = input(f"  {BOLD}Approve? [y/N]{OFF} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        ok = ans in ("y", "yes")
        engine.decide_approval(p["id"], ok)
        print(f"  {GREEN if ok else RED}{'approved' if ok else 'denied'}{OFF}")


def cmd_bench(args) -> int:
    """Which of your models can actually be an agent."""
    db.init()
    from . import bench

    print(BANNER)
    picks = bench.candidates(getattr(args, "provider", "") or "", getattr(args, "model", None))
    if not picks:
        print(f"  {RED}No reachable models.{OFF} {DIM}Run `hermes doctor` to see why.{OFF}\n")
        return 1

    which = [args.scenario] if getattr(args, "scenario", "") else list(bench.SCENARIOS)
    print(f"  {BOLD}Benchmarking {len(picks)} model(s) over {len(which)} scenario(s){OFF}")
    for s in which:
        print(f"  {DIM}· {bench.SCENARIOS[s]['label']}{OFF}")
    print(f"  {DIM}Graded on what ended up on disk, not on what the model said it did.{OFF}\n")

    C = {"excellent": GREEN, "good": GREEN, "usable": GOLD, "weak": GOLD, "unusable": RED}

    def started(provider, model, scenario):
        print(f"  {INDIGO}▸{OFF} {BOLD}{model}{OFF} {DIM}· {scenario}{OFF}", flush=True)

    def finished(r):
        for name, ok in r["results"].items():
            print(f"      {GREEN + '✓' + OFF if ok else RED + '✗' + OFF} {DIM}{name}{OFF}")
        print(f"      {DIM}{r['passed']}/{r['total']} · {r['steps']} steps · {r['seconds']}s{OFF}")
        if r["error"]:
            print(f"      {RED}{r['error'][:140]}{OFF}")
        print()

    rows = bench.run(getattr(args, "provider", "") or "", getattr(args, "model", None),
                     which, on_start=started, on_result=finished)

    print(f"  {BOLD}Verdict{OFF}\n")
    for r in rows:
        c = C[r["tier"]]
        print(f"   {c}●{OFF} {BOLD}{r['model']:<22}{OFF}{c}{r['tier']:<11}{OFF}"
              f"{DIM}{r['passed']}/{r['total']}  {r['seconds']:>6.1f}s  {r['advice']}{OFF}")
        for sub in r["runs"]:
            missed = [k for k, v in sub["results"].items() if not v]
            mark = GREEN + "✓" + OFF if not missed else GOLD + "!" + OFF
            detail = "all checks" if not missed else "missed: " + ", ".join(missed[:3])
            print(f"       {mark} {DIM}{sub['scenario']:<10} {sub['passed']}/{sub['total']}  "
                  f"{detail}{OFF}")
    best = rows[0]
    if best["passed"] >= best["total"] * 0.85:
        print(f"\n  {DIM}Best of these: {OFF}{BOLD}{best['model']}{OFF}"
              f"{DIM} — set it per agent, or as the default in Settings.{OFF}")
    else:
        print(f"\n  {GOLD}None of these drove the loop cleanly.{OFF} "
              f"{DIM}Try a larger local model, or add a free key: hermes key groq{OFF}")
    print()
    return 0


def cmd_tasks(args) -> int:
    """The board, for people who are already in a terminal."""
    db.init()
    _seed()
    agents = {a["id"]: a for a in db.q("SELECT id,name,emoji FROM agents")}
    running = {r["task_id"]: r for r in db.q("SELECT * FROM runs WHERE status='running'")}
    waiting = {a["run_id"] for a in db.q("SELECT run_id FROM approvals WHERE state='pending'")}

    groups = [("In progress", ["running"], INDIGO),
              ("Queued", ["queued"], GOLD),
              ("Needs attention", ["failed", "incomplete", "halted", "cancelled"], RED),
              ("Completed", ["done"], GREEN)]
    rows = db.q("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (int(args.limit),))
    if not rows:
        print(f"\n  {DIM}Nothing on the board. Assign something with "
              f"`hermes run <agent> \"<task>\"`.{OFF}\n")
        return 0

    print()
    for label, states, colour in groups:
        items = [t for t in rows if t["status"] in states]
        if not items:
            continue
        print(f"  {colour}{BOLD}{label}{OFF} {DIM}· {len(items)}{OFF}")
        for t in items:
            a = agents.get(t["agent_id"], {})
            bits = [f"{a.get('emoji', '')} {a.get('name', 'unassigned')}".strip()]
            if t["priority"] == "high":
                bits.append(f"{GOLD}high{OFF}")
            if t["attempts"]:
                bits.append(f"attempt {t['attempts'] + 1}")
            run = running.get(t["id"])
            if run:
                mins = int((db.now() - run["started_at"]) // 60)
                bits.append(f"{mins}m" if mins else "just started")
                if run["id"] in waiting:
                    bits.append(f"{GOLD}waiting on you{OFF}")
            print(f"    {t['title'][:62]:<64}{DIM}{' · '.join(bits)}{OFF}")
        print()

    pending = len(waiting)
    if pending:
        print(f"  {GOLD}{pending} approval(s) waiting.{OFF} "
              f"{DIM}Open the console, or run the task from here to be asked.{OFF}\n")
    return 0


def cmd_run(args) -> int:
    db.init()
    _seed()
    agent = db.q1("SELECT * FROM agents WHERE (name=? OR id=?) AND archived=0",
                  (args.agent, args.agent))
    if not agent:
        print(f"{RED}No agent named '{args.agent}'.{OFF} Run `hermes agents` to list them.")
        return 1
    tid = db.nid()
    db.ex("""INSERT INTO tasks(id,title,brief,agent_id,status,priority,created_at)
             VALUES(?,?,?,?,'queued','normal',?)""",
          (tid, args.task[:120], args.task, agent["id"], db.now()))
    print(f"\n  {agent['emoji']} {BOLD}{agent['name']}{OFF} → {args.task}\n")

    from .runtime import bus
    q = bus.subscribe()
    done = threading.Event()
    pending: list = []

    def watch():
        while not done.is_set():
            try:
                ev = q.get(timeout=0.5)
            except Exception:
                continue
            k, p = ev["kind"], ev["payload"]
            if k == "step":
                print(f"  {DIM}step {p['n']}/{p['of']}{OFF}")
            elif k == "thought":
                print(f"  {DIM}{p['text'][:300]}{OFF}")
            elif k == "tool_call":
                print(f"  {INDIGO}→ {p['tool']}{OFF} {DIM}{json.dumps(p['args'])[:160]}{OFF}")
            elif k == "tool_result":
                mark = f"{GREEN}✓{OFF}" if p["ok"] else f"{RED}✗{OFF}"
                print(f"  {mark} {DIM}{p['output'][:300]}{OFF}")
            elif k == "verifying":
                print(f"  {DIM}⚖ quality gate checking the work…{OFF}")
            elif k == "verify_passed":
                print(f"  {GREEN}⚖ quality gate passed{OFF}")
            elif k == "verify_rejected":
                print(f"  {GOLD}⚖ sent back — still outstanding: {p['missing'][:200]}{OFF}")
            elif k == "approval_requested":
                pending.append(p)
            elif k == "escalation":
                print(f"\n  {GOLD}🖐 escalated: {p.get('question') or p.get('reason')}{OFF}")
            elif k == "run_finished":
                done.set()

    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=lambda: _approvals_on_the_terminal(pending, done, args),
                     daemon=True).start()
    engine.run_task(tid)
    done.set()
    time.sleep(0.3)   # let the last events drain before the summary

    task = db.q1("SELECT * FROM tasks WHERE id=?", (tid,))
    print(f"\n  {BOLD}{'Result' if task['status'] == 'done' else task['status'].title()}{OFF}\n")
    print("  " + (task["result"] or task["error"] or "(nothing returned)").replace("\n", "\n  "))
    print()
    return 0 if task["status"] == "done" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes", description="Hermes — Agent Operations Console")
    parser.add_argument("--version", action="version", version=f"Hermes {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the console (default)")
    s.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address; anything but localhost needs --i-understand-the-risk")
    s.add_argument("--i-understand-the-risk", action="store_true",
                   dest="i_understand_the_risk",
                   help="acknowledge that non-localhost binding needs TLS in front")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_serve)

    sh = sub.add_parser("shell", help="interactive terminal console (no browser needed)")
    sh.set_defaults(fn=lambda a: __import__("hermes.shell", fromlist=["main"]).main())

    d = sub.add_parser("doctor", help="check backends, keys and agents")
    d.set_defaults(fn=cmd_doctor)

    k = sub.add_parser("key", help="store an API key (encrypted)")
    k.add_argument("provider")
    k.add_argument("value", nargs="?")
    k.set_defaults(fn=cmd_key)

    au = sub.add_parser("audit", help="show and verify the tamper-evident audit log")
    au.add_argument("--limit", type=int, default=40)
    au.set_defaults(fn=cmd_audit)

    a = sub.add_parser("agents", help="list agents and their capabilities")
    a.set_defaults(fn=cmd_agents)

    bn = sub.add_parser("bench", help="test which of your models can actually drive an agent")
    bn.add_argument("--model", action="append", help="test just this model (repeatable)")
    bn.add_argument("--provider", default="", help="test only this backend's models")
    bn.add_argument("--scenario", default="", choices=["", "basics", "assistant"],
                    help="run just one scenario instead of both")
    bn.set_defaults(fn=cmd_bench)

    tk = sub.add_parser("tasks", help="show the board: queued, running and finished work")
    tk.add_argument("--limit", type=int, default=40)
    tk.set_defaults(fn=cmd_tasks)

    r = sub.add_parser("run", help="assign a task to an agent from the terminal")
    r.add_argument("agent")
    r.add_argument("task")
    r.add_argument("-y", "--yes", action="store_true",
                   help="approve tool calls as they come up, except the ones that "
                        "reach outside this machine — those always ask")
    r.set_defaults(fn=cmd_run)

    args = parser.parse_args(argv)
    if not args.cmd:
        args = parser.parse_args(["serve"])
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
