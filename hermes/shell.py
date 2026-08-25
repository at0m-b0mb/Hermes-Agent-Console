"""`hermes shell` — the interactive terminal console.

Same engine, same permissions, same audit trail as the browser console. Built
for the case where there is no browser: an SSH session into a server.
"""
from __future__ import annotations

import json
import shlex
import threading
import time

from . import db, security
from .runtime import bus, engine, evaluator, tools, workforce

G = "\033[38;5;179m"; I = "\033[38;5;62m"; D = "\033[2m"; B = "\033[1m"
OFF = "\033[0m"; GR = "\033[38;5;79m"; R = "\033[38;5;167m"; V = "\033[38;5;140m"

HELP = f"""
  {B}Talking to your agents{OFF}
    {G}<anything you type>{OFF}      assign it to the current agent and watch it work
    {G}@name <task>{OFF}             assign to a specific agent, e.g. {D}@Forge fix the build{OFF}

  {B}Commands{OFF}
    {G}/agents{OFF}                  list your team and what each one may touch
    {G}/use <name>{OFF}              make an agent the default for plain messages
    {G}/new{OFF}                     create an agent, guided
    {G}/tasks{OFF} [n]               recent tasks and their status
    {G}/queue{OFF}                   what is waiting and what is running now
    {G}/inbox{OFF}                   approvals and questions waiting on you
    {G}/duties{OFF}                  standing recurring responsibilities
    {G}/score{OFF} [name]            performance scorecards
    {G}/autonomy <name> <level>{OFF} supervised | trusted | autonomous
    {G}/workforce on|off{OFF}        let agents pick up their own queue
    {G}/audit{OFF} [n]               tamper-evident action log
    {G}/doctor{OFF}                  backend and configuration check
    {G}/help{OFF}   {G}/quit{OFF}
"""


def _agents() -> list[dict]:
    return db.q("SELECT * FROM agents WHERE archived=0 ORDER BY created_at")


def _find(name: str) -> dict | None:
    return db.q1("SELECT * FROM agents WHERE (lower(name)=lower(?) OR id=?) AND archived=0",
                 (name, name))


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(f"  {prompt}{f' {D}[{default}]{OFF}' if default else ''}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return v or default


class Shell:
    def __init__(self):
        self.agent: dict | None = None
        self.stop = threading.Event()

    # ------------------------------------------------------------ watching
    def watch_run(self, task_id: str) -> None:
        """Stream one run to the terminal, handling approvals inline."""
        q = bus.subscribe()
        done = threading.Event()
        pending: list[str] = []

        def pump():
            while not done.is_set():
                try:
                    ev = q.get(timeout=0.4)
                except Exception:
                    continue
                k, p = ev["kind"], ev["payload"]
                if k == "step":
                    print(f"  {D}· step {p['n']}/{p['of']}{OFF}")
                elif k == "thought" and p.get("text"):
                    print(f"  {D}{p['text'][:400]}{OFF}")
                elif k == "tool_call":
                    print(f"  {I}→ {p['tool']}{OFF} {D}{json.dumps(p['args'])[:180]}{OFF}")
                elif k == "auto_approved":
                    print(f"    {D}auto-approved ({p['autonomy']}){OFF}")
                elif k == "tool_result":
                    mark = f"{GR}✓{OFF}" if p["ok"] else f"{R}✗{OFF}"
                    body = p["output"].replace("\n", "\n      ")[:700]
                    print(f"  {mark} {D}{body}{OFF}")
                elif k == "security_block":
                    print(f"  {R}🛡 BLOCKED{OFF} {p['reason'][:220]}")
                elif k == "malformed_call":
                    print(f"  {D}(reformatting the call…){OFF}")
                elif k == "verifying":
                    print(f"  {V}⚖ quality gate checking the work…{OFF}")
                elif k == "verify_passed":
                    print(f"  {GR}⚖ quality gate passed{OFF}")
                elif k == "verify_rejected":
                    print(f"  {G}⚖ sent back: {p['missing'][:200]}{OFF}")
                elif k == "approval_requested":
                    pending.append(p["id"])
                    print(f"\n  {G}{B}⏸ {p['agent']} needs approval to run {p['tool']}{OFF}")
                    print(f"  {D}{json.dumps(p['args'], indent=2)[:600]}{OFF}")
                elif k == "escalation":
                    print(f"\n  {V}🖐 escalated: {p.get('question') or p.get('reason')}{OFF}")
                    print(f"  {D}Answer it with /inbox{OFF}")
                elif k == "run_finished":
                    done.set()

        threading.Thread(target=pump, daemon=True).start()
        engine.start(task_id)

        # Poll for approvals so we can prompt on the main thread.
        while not done.is_set():
            time.sleep(0.3)
            if pending:
                aid = pending.pop(0)
                row = db.q1("SELECT * FROM approvals WHERE id=? AND state='pending'", (aid,))
                if not row:
                    continue
                try:
                    ans = input(f"  {B}Approve? [y/N]{OFF} ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = "n"
                engine.decide_approval(aid, ans in ("y", "yes"))
                print(f"  {GR if ans in ('y','yes') else R}{'approved' if ans in ('y','yes') else 'denied'}{OFF}")
        time.sleep(0.5)

        task = db.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
        print()
        if task["status"] == "done":
            print(f"  {GR}{B}✓ done{OFF}\n")
            print("  " + (task["result"] or "").replace("\n", "\n  "))
        else:
            print(f"  {R}{B}✗ {task['status']}{OFF}  {D}{task['error'][:400]}{OFF}")
            if task["result"]:
                print("  " + task["result"].replace("\n", "\n  ")[:1200])
        print()

    # ------------------------------------------------------------ dispatch
    def assign(self, text: str, agent: dict) -> None:
        tid = db.nid()
        db.ex("""INSERT INTO tasks(id,title,brief,agent_id,status,priority,created_at,source)
                 VALUES(?,?,?,?,'queued','normal',?,'operator')""",
              (tid, text[:120], text, agent["id"], db.now()))
        print(f"\n  {agent['emoji']} {B}{agent['name']}{OFF} {D}· {agent['provider']}/{agent['model']}{OFF}\n")
        self.watch_run(tid)

    def cmd(self, line: str) -> bool:
        parts = shlex.split(line) if line.strip() else [""]
        c = parts[0].lower()
        args = parts[1:]

        if c in ("/quit", "/exit", "/q"):
            return False
        if c in ("/help", "/?"):
            print(HELP); return True

        if c == "/agents":
            print()
            for a in _agents():
                inv = tools.inventory(a)
                lvl = workforce.LEVELS.get(a.get("autonomy") or "supervised", {})
                mark = f"{G}▸{OFF}" if self.agent and a["id"] == self.agent["id"] else " "
                print(f"  {mark} {a['emoji']} {B}{a['name']:<12}{OFF} {D}{a['role'] or '—'}{OFF}")
                print(f"      {D}{a['provider']}/{a['model']} · {lvl.get('label','?')} · "
                      f"{inv['counts']['allow']} free, {inv['counts']['ask']} ask, "
                      f"{inv['counts']['deny']} blocked{OFF}")
            print()
            return True

        if c == "/use":
            if not args: print(f"  {R}Which agent? /use Forge{OFF}"); return True
            a = _find(args[0])
            if not a: print(f"  {R}No agent called '{args[0]}'.{OFF}"); return True
            self.agent = a
            print(f"  {GR}Talking to {a['emoji']} {a['name']}.{OFF}")
            return True

        if c == "/new":
            self.create_agent(); return True

        if c == "/tasks":
            n = int(args[0]) if args and args[0].isdigit() else 12
            print()
            for t in db.q("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (n,)):
                a = db.q1("SELECT name,emoji FROM agents WHERE id=?", (t["agent_id"],)) or {}
                col = {"done": GR, "running": G, "queued": D}.get(t["status"], R)
                print(f"  {col}{t['status']:<10}{OFF} {a.get('emoji','')} {a.get('name','?'):<10} "
                      f"{t['title'][:60]}")
            print()
            return True

        if c == "/queue":
            wf = {"queued": db.q1("SELECT COUNT(*) c FROM tasks WHERE status='queued'")["c"],
                  "running": db.q1("SELECT COUNT(*) c FROM runs WHERE status='running'")["c"]}
            print(f"\n  dispatcher {GR + 'on' + OFF if workforce.running() else D + 'off' + OFF}"
                  f"  ·  {wf['running']} running  ·  {wf['queued']} queued\n")
            return True

        if c == "/inbox":
            self.inbox(); return True

        if c == "/duties":
            print()
            rows = db.q("SELECT * FROM duties ORDER BY created_at DESC")
            if not rows: print(f"  {D}No standing duties.{OFF}\n"); return True
            for d in rows:
                a = db.q1("SELECT name,emoji FROM agents WHERE id=?", (d["agent_id"],)) or {}
                state = f"{GR}active{OFF}" if d["active"] else f"{D}paused{OFF}"
                print(f"  {state}  {a.get('emoji','')} {a.get('name','?'):<10} "
                      f"every {d['cadence_minutes']}m  {d['title'][:50]}")
            print()
            return True

        if c == "/score":
            print()
            board = evaluator.leaderboard()
            for a in board:
                if args and a["name"].lower() != args[0].lower():
                    continue
                score = a["avg_score"] if a["avg_score"] is not None else "—"
                rate = f"{a['success_rate']}%" if a["success_rate"] is not None else "—"
                print(f"  {a['emoji']} {B}{a['name']:<12}{OFF} score {G}{score:<6}{OFF} "
                      f"success {rate:<6} runs {a['runs']:<4} ${a['cost']:.4f}")
            print()
            return True

        if c == "/autonomy":
            if len(args) < 2:
                print(f"  {R}Usage: /autonomy Forge trusted{OFF}")
                print(f"  {D}levels: " + " · ".join(
                    f"{k} ({v['blurb']})" for k, v in workforce.LEVELS.items()) + OFF)
                return True
            a = _find(args[0])
            if not a: print(f"  {R}No agent '{args[0]}'.{OFF}"); return True
            if args[1] not in workforce.LEVELS:
                print(f"  {R}Level must be one of: {', '.join(workforce.LEVELS)}{OFF}"); return True
            db.ex("UPDATE agents SET autonomy=? WHERE id=?", (args[1], a["id"]))
            security.audit("operator", "agent.autonomy_changed",
                           {"agent": a["name"], "level": args[1]})
            print(f"  {GR}{a['name']} is now {workforce.LEVELS[args[1]]['label'].lower()}.{OFF} "
                  f"{D}{workforce.LEVELS[args[1]]['blurb']}{OFF}")
            return True

        if c == "/workforce":
            if args and args[0] in ("on", "off"):
                if args[0] == "on":
                    db.set_setting("workforce.enabled", "1"); workforce.start()
                else:
                    db.set_setting("workforce.enabled", "0"); workforce.stop()
            print(f"  dispatcher is {GR + 'on' + OFF if workforce.running() else D + 'off' + OFF}")
            return True

        if c == "/audit":
            n = int(args[0]) if args and args[0].isdigit() else 15
            chain = security.verify_audit()
            print(f"\n  {GR if chain['ok'] else R}chain {'intact' if chain['ok'] else 'BROKEN'}{OFF}"
                  f" {D}· {chain['entries']} entries{OFF}\n")
            import datetime
            for r in reversed(db.q("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (n,))):
                ts = datetime.datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
                print(f"  {D}{ts}{OFF} {r['actor']:<9} {I}{r['action']:<22}{OFF} "
                      f"{D}{r['detail'][:70]}{OFF}")
            print()
            return True

        if c == "/doctor":
            from .__main__ import cmd_doctor
            cmd_doctor(type("A", (), {})()); return True

        print(f"  {R}Unknown command {c}.{OFF} {D}/help for the list.{OFF}")
        return True

    def inbox(self) -> None:
        approvals = db.q("SELECT * FROM approvals WHERE state='pending' ORDER BY created_at")
        escs = db.q("SELECT * FROM escalations WHERE state='open' ORDER BY created_at")
        if not approvals and not escs:
            print(f"\n  {GR}Nothing waiting. Your agents are unblocked.{OFF}\n"); return
        for a in approvals:
            ag = db.q1("SELECT name,emoji FROM agents WHERE id=?", (a["agent_id"],)) or {}
            print(f"\n  {G}⏸ {ag.get('name','?')} wants to run {B}{a['tool']}{OFF}")
            print(f"  {D}{a['args'][:600]}{OFF}")
            ans = _ask("approve? [y/N]", "n").lower()
            engine.decide_approval(a["id"], ans in ("y", "yes"))
            print(f"  {GR + 'approved' if ans in ('y','yes') else R + 'denied'}{OFF}")
        for e in escs:
            ag = db.q1("SELECT name,emoji FROM agents WHERE id=?", (e["agent_id"],)) or {}
            print(f"\n  {V}🖐 {ag.get('name','?')} is blocked{OFF}")
            print(f"  {D}{e['reason']}{OFF}")
            print(f"  {B}{e['question']}{OFF}")
            ans = _ask("your answer (blank to skip)")
            if ans:
                workforce.answer_escalation(e["id"], ans)
                print(f"  {GR}answered — task requeued{OFF}")
        print()

    def create_agent(self) -> None:
        from . import providers
        print(f"\n  {B}New agent{OFF}")
        name = _ask("name")
        if not name:
            print(f"  {D}cancelled{OFF}\n"); return
        role = _ask("speciality", "generalist")
        emoji = _ask("icon", "🤖")
        ready = [p for p in providers.catalogue() if p["ok"]]
        if not ready:
            print(f"  {R}No backend is set up. Run `hermes key groq <key>` or start Ollama.{OFF}\n")
            return
        print(f"  {D}backends: {', '.join(p['id'] for p in ready)}{OFF}")
        provider = _ask("backend", ready[0]["id"])
        models = providers.list_models(provider)
        print(f"  {D}models: {', '.join(models[:8])}{OFF}")
        model = _ask("model", models[0] if models else "")
        print(f"  {D}autonomy: " + " · ".join(workforce.LEVELS) + OFF)
        autonomy = _ask("autonomy", "supervised")
        prompt = _ask("standing instructions", "Work carefully and verify your results.")

        aid = db.nid()
        db.ex("""INSERT INTO agents(id,name,role,emoji,accent,system_prompt,provider,model,
                 temperature,max_steps,grants,scopes,status,created_at,updated_at,autonomy,shift)
                 VALUES(?,?,?,?,'#F5B93B',?,?,?,0.7,18,?,?,'idle',?,?,?,'always')""",
              (aid, name, role, emoji, prompt, provider, model,
               json.dumps(tools.default_grants()),
               json.dumps({"fs_roots": [str(db.config.WORKSPACE)], "net_allow": []}),
               db.now(), db.now(),
               autonomy if autonomy in workforce.LEVELS else "supervised"))
        security.audit("operator", "agent.created", {"id": aid, "name": name, "via": "shell"})
        self.agent = db.q1("SELECT * FROM agents WHERE id=?", (aid,))
        print(f"\n  {GR}{emoji} {name} is on the team, and is now your current agent.{OFF}")
        print(f"  {D}Adjust what it may touch in the browser console, or with /autonomy.{OFF}\n")

    # ---------------------------------------------------------------- loop
    def run(self) -> int:
        from . import __version__
        db.init(); security.init_audit()
        from .server import seed_if_empty
        seed_if_empty()
        if db.setting("workforce.enabled", "1") == "1":
            workforce.start()

        agents = _agents()
        self.agent = agents[0] if agents else None
        print(f"\n{G}  ╦ ╦ ╔═╗ ╦═╗ ╔╦╗ ╔═╗ ╔═╗\n"
              f"  ╠═╣ ║╣  ╠╦╝ ║║║ ║╣  ╚═╗\n"
              f"  ╩ ╩ ╚═╝ ╩╚═ ╩ ╩ ╚═╝ ╚═╝{OFF}")
        print(f"  {D}interactive shell · v{__version__} · /help for commands{OFF}\n")
        if self.agent:
            print(f"  Talking to {self.agent['emoji']} {B}{self.agent['name']}{OFF} "
                  f"{D}— just type what you need done.{OFF}\n")
        else:
            print(f"  {D}No agents yet. Type /new to create one.{OFF}\n")

        while True:
            try:
                who = self.agent["name"] if self.agent else "hermes"
                line = input(f"{G}{who}{OFF} {D}›{OFF} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n  {D}bye{OFF}\n"); return 0
            if not line:
                continue
            if line.startswith("/"):
                if not self.cmd(line):
                    print(f"  {D}bye{OFF}\n"); return 0
                continue
            if line.startswith("@"):
                head, _, rest = line[1:].partition(" ")
                target = _find(head)
                if not target:
                    print(f"  {R}No agent called '{head}'.{OFF}"); continue
                if not rest.strip():
                    self.agent = target
                    print(f"  {GR}Talking to {target['emoji']} {target['name']}.{OFF}"); continue
                self.assign(rest.strip(), target); continue
            if not self.agent:
                print(f"  {R}No agent selected. /new to create one, /use to pick one.{OFF}"); continue
            self.assign(line, self.agent)


def main() -> int:
    return Shell().run()
