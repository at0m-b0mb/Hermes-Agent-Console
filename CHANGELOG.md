# Changelog

All notable changes to Hermes are recorded here.
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-08-27

Hardening for anything reachable from a network, plus the UI fixes that came out
of finally looking at the console at full size.

### Denial of service — found by attacking a running instance

- **A client that never authenticated could park every worker thread.** Two hundred
  half-finished requests took the thread count from 3 to 203, and nothing timed them
  out. There is now a **20-second socket timeout**, a **64-connection ceiling**, and —
  the part that matters — a **per-client share of 8**, so one client saturating the
  port cannot take the console away from everyone else.
- **The brute-force lockout could be turned against the operator.** It keyed on client
  IP, so somebody spraying wrong tokens locked out the real user; behind the reverse
  proxy the README recommends, *everyone* shares one address, so this was worse than
  it looks. The token is now checked **before** the lockout is consulted: a valid token
  is never throttled, no matter how much noise anyone else is making.
- **A body that was announced and never sent held a thread open.** Bodies are capped at
  2 MB and read with a timeout.
- **A malformed `Content-Length` killed the handler thread** with an unhandled
  `ValueError` and printed a traceback full of absolute paths. It is a 400 now.

### What the server tells the network

- **Security headers on every response**: `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, a `Permissions-Policy` that turns off camera,
  microphone, geolocation, payment and USB, and a CSP that pins every fetch, script,
  style and frame ancestor to this origin.
- **The Python version is no longer advertised** in `Server:`.
- **The session token is a header everywhere except the live event stream**, which
  cannot set one. It used to be accepted in the query string on every endpoint, which
  put it in proxy logs, browser history and `Referer`.
- **A 500 returns a reference, not an exception string.** The detail goes to the
  operator's terminal and the audit log.
- **`X-Forwarded-For` is read only from an address you name** in `server.trusted_proxy`.
  Believing it unconditionally would let any client claim to be anyone.
- Static file serving now asks the path whether it is inside the web directory instead
  of comparing string prefixes.

### `hermes doctor` reports the security posture

Vault and home directory permissions, the Host allowlist, whether a proxy is trusted,
which agents have shell access or a wide filesystem scope, and whether the audit chain
still verifies — with the fixed, non-configurable floor stated at the bottom.

### The console

- **Task cards were unreadable on the board.** The card was a two-column flex, so in a
  265px column the buttons claimed their width first and left the title four characters
  — titles wrapped one word per line and result text became an unreadable ribbon. The
  actions now wrap underneath exactly when there is no room for them.
- **Keyboard focus is visible.** There is a whole keyboard layer in this console and no
  focus ring anywhere; `:focus-visible` rings now show for keyboard and assistive-tech
  users without appearing for pointer users. Added a skip link past the sidebar and
  proper labels on the icon-only buttons.
- **`prefers-reduced-motion` is respected** — none of the motion carries information.
- **`prefers-contrast: more`** strengthens borders and muted text.
- Layouts hold together at narrower window widths instead of only at full screen.

### Documentation

Six real screenshots of the running console, and a README security section that
describes the ceilings and headers as they actually are.

---

## [1.4.0] — 2026-08-27

### Know which model can actually do the job

- **`hermes bench`** runs two fixed scenarios against every model your backends offer and
  grades **what ended up on disk**, not what the model said it did. *Basics* asks whether it
  can call a tool at all and put the result where it was told. *Assistant* is the real shape
  of the work: read several files, apply a rule, use the real date instead of inventing one,
  and produce an artefact in an exact format without disturbing the sources.
- Verdicts run excellent / good / usable / weak / unusable, with the specific checks each
  model missed. Being good at conversation and being able to drive a tool loop are different
  skills, and the gap is enormous at small sizes.
- The first cut of this graded every model "excellent", which meant it was measuring nothing.
  The assistant scenario exists because a benchmark that cannot separate your models is not
  worth running.
- It cleans up completely — no throwaway agents, tasks or runs left in your history.

### Tools daily assistant work actually needs

- **`append_file`** — add to a file instead of replacing it. With only `write_file`, an agent
  building something up across several steps either loses the earlier work or has to read the
  whole file back and resend it every time.
- **`move_file`** — move and rename, scope-checked at both ends, refusing to clobber unless
  told to.
- **`now`** — the real date, time and weekday. A model's idea of today comes from its
  training data and is confidently wrong, so anything dated needs a clock.
- **`calc`** — exact arithmetic, which matters the moment an agent is adding up invoices.
  Parsed to a literal expression tree and refused unless it is pure arithmetic, so nothing
  here can reach a name or call a function.

### Agents made before a tool existed can now use it

- A grant dict is a closed list, so an agent created before a tool shipped could never call
  it and nothing in the console hinted why. Missing entries are filled with that tool's
  **default** on startup — `ask` for anything that writes, `deny` for outbound mail — so this
  widens what an agent asks about, never what it may do unsupervised.

### See what your agents produced

- **A Files view** over the workspace, with breadcrumbs, a preview drawer and a download.
  Agents write real files and the only way to see them used to be a terminal.
- It enforces the same floor as the agents: protected paths are marked as such in the
  listing and refuse to open, and nothing outside the workspace is reachable.

### Fixed

- **The benchmark could hang for fifteen minutes per model.** Its throwaway agent was
  `supervised`, so the moment a model reached for a tool left on "ask", the run parked on an
  approval nobody was there to give. The bench agent is now explicitly allowed exactly the
  tools its scenario needs and denied everything else, so it can never wait on a human.

---

## [1.3.0] — 2026-08-26

Features aimed at the two things that were actually painful in use: not knowing what a
run was doing, and small models fighting the tools.

### You can see what a run is doing

- **A live elapsed clock** on every running task and run row, ticking each second. A run
  that has been going twenty minutes and one that started ten seconds ago used to look
  identical, and that difference is the whole question you are asking when you look at
  the board.
- **A "waiting on you" badge** on any run blocked on your approval — the reason a run
  looks frozen is usually you, and nothing said so.
- **`hermes tasks`** shows the same board from a terminal, including elapsed time and
  what is waiting on a decision.

### Less retyping, less scrolling

- **Re-run any finished task** with ↻. It opens the composer prefilled with the original
  brief, agent and priority — because the reason you re-run something is usually that the
  brief needed a word changing.
- **Filter boxes** on the Work board and the Runs list, matching title, brief, agent,
  model and status. The board filter survives the six-second auto-refresh instead of
  eating what you were typing.

### Tools that small models can actually use

- **`read_file` takes `from_line` and `max_lines`.** A big file used to be a flat refusal,
  which left the model with nowhere to go — it would usually just call the same thing
  again. The error now tells it how to ask for a slice, and the response says which lines
  it got and how many remain.
- **`list_dir` takes `depth`.** Walking a tree one call per directory burns a step limit
  fast, and small models do exactly that.
- **Numeric arguments accept strings**, because models send `"3"` about half the time.

### The console cannot ship broken

- **CI now runs `node --check` on the console.** It is a single plain script tag with no
  build step, so one stray bracket takes the whole UI down while every Python check still
  passes. That happened while building this release; the check would have caught it in
  seconds, and now it does.

---

## [1.2.0] — 2026-08-26

Found by running the published build end to end against a local model — a fresh
`curl | bash` install, real multi-step agent tasks, the dispatcher working an unattended
queue, and the security floor probed from inside a live run. The agent loop, the quality
gate, the approval path, the sandbox and the audit chain all held. These are the gaps that
showed up around them.

### The record was missing the decisions that matter most

- **A human approving or denying a tool call was never audited.** The README promised the
  chain covered "every tool call, approval, key change and run", and the operator's yes or
  no — the single most consequential action in the system — was the one thing absent from
  it. `tool.approved` / `tool.denied` now go into the chain with the tool and run.
- **Storing or clearing an API key was not audited either**, from the console or the CLI.
  It is now, recording only that the key moved, never its value.
- **Pausing the workforce left no trace.** Starting the dispatcher announced itself in the
  live feed and stopping it did not, so a paused workforce looked like a quiet one.

### An API call could fail silently

- **`POST /api/agents/<id>/<anything>` answered 200 OK and did nothing.** An unrecognised
  subresource fell through to the plain GET, so a script setting an agent's autonomy got a
  success and a completely unchanged agent. It now returns 404 naming the right route, and
  an unsupported method on an agent returns 405.

### `search_files` could not find files

- **A query like `*.md` returned "no matches"** because the tool only grepped contents,
  never filenames — and models reach for it as a file finder constantly. In a real run this
  made an agent conclude a folder full of markdown was empty and write "No Markdown Files
  Found" over it. A glob now searches names, a plain query searches contents *and* names,
  and the result says how many matched which way. The protected-path floor still applies to
  both.

### `hermes run` hid its best feature

- **Quality-gate activity was invisible from the CLI.** The gate ran, rejected work and sent
  agents back to redo it — and `hermes run` printed none of it, so the most valuable thing
  the system did happened silently. It now shows the gate checking, passing, and exactly
  what it sent back.
- **`hermes doctor` reported "0 configured" agents on a fresh install**, then `hermes agents`
  showed three. Doctor seeds like the other commands now.

### Automatic scoring says that it is off

- Scoring needs a judge model and is off by default, because it costs a second model call
  per run. Nothing said so: **Performance** simply showed blanks forever. It now explains
  the blank and links to the setting, and the README no longer implies grading is automatic.
  The quality gate, which is always on, is described separately from scoring, which is not.

---

## [1.1.0] — 2026-08-26

### The console gained a keyboard

- **Command palette** on <kbd>⌘K</kbd> / <kbd>Ctrl K</kbd> / <kbd>/</kbd> — every view,
  every agent, and the actions worth reaching in one keystroke. Matches on a subsequence,
  so "asw" finds "Assign work".
- **Single-key navigation** — <kbd>g</kbd> then a letter to jump between views,
  <kbd>n</kbd> to assign work, <kbd>t</kbd> for the theme, <kbd>esc</kbd> to close whatever
  is open, and <kbd>?</kbd> for a sheet listing all of it.

### Light theme

- A full light palette alongside the original dark one, toggled with <kbd>t</kbd> or the
  topbar button and remembered per browser. With no stored choice it follows the operating
  system, and it is applied before the first paint so there is no flash of the wrong ground.
- Two chrome surfaces — the sidebar gradient and the topbar — were painted with literal
  colours and stayed dark in light mode. Both now read through tokens.

### Elsewhere in the console

- **Desktop notifications**, opt-in, for the two things worth interrupting you: an agent
  waiting on your decision, and a run finishing. They fire only while the tab is in the
  background.
- **Export a run as Markdown** from the run drawer — metadata, full transcript, final result.
- **Agent icons render.** They are emoji, and the font stack had no emoji family in it, so
  they appeared as tofu boxes on Linux and Windows.
- The capability editor now says on `email_send` itself that "allow" does not remove the
  approval step, because it does not and cannot.

### `hermes run` finishes what it starts

- **Approvals are answered in the terminal.** A supervised agent asks before every write, so
  `hermes run` used to sit in silence for the full fifteen-minute approval timeout with no
  way to say yes — the console was the only place a decision could be made. It now prompts
  where you are, the way `hermes shell` always did.
- **`--yes`** approves tool calls as they come up, for scripts and unattended runs. It
  deliberately stops short of the actions that leave the machine: `email_send` still asks,
  every time. `security.blanket_approval_covers` states that rule in one place and a test
  holds it there.
- With no terminal attached, requests are **denied immediately with the remedy printed**
  rather than hanging until timeout.

### Documentation

- The README is rebuilt around **"How do I…?"** — four tables mapping a thing you want to do
  to the way to do it and the file that implements it, plus a code map saying where to go to
  change each behaviour and a diagram of how a tool call actually travels through the system.

---

## [1.0.0] — 2026-08-25

First public release.

### The console

- **Agents that own a queue.** A dispatcher hands queued work to whoever is free
  and on shift. Nobody presses Run.
- **A quality gate before any task is called done.** A second model checks whether
  the work was performed or merely described, and sends it back with the specific
  gap named when it was not.
- **Three starter agents** — Atlas (research), Forge (code), Ledger (files) — plus
  eight hireable templates, each arriving with a job description, granted tools and
  its own standing duties.
- **Two front ends on one engine** — a graphical console at `localhost:4317` with a
  live SSE activity feed, and `hermes shell` for SSH sessions with no browser.
- **Six AI backends** — Ollama, Groq, Gemini, Anthropic, OpenAI, and any
  OpenAI-compatible endpoint. Chosen per agent, so a cheap local model can do the
  routine work and a strong one the hard jobs.
- **Runs fully offline.** With Ollama and no API key, every feature works.
- **Standing duties, escalation and retries** — recurring work is created when it
  comes due; a stuck agent asks you a question instead of failing silently; failed
  tasks retry with the error fed back.
- **Scoring** — an LLM judge grades correctness, completeness, efficiency and safety;
  your own 0–100 rating always overrides it.
- **Zero third-party dependencies.** Python standard library only, asserted by a test
  that walks every import in the package.

### Security

- Hard floor enforced in code, not in a prompt: 23 protected path patterns, 18 blocked
  command patterns, and Hermes' own vault, token and database off-limits to agents.
- Untrusted content from web pages and email is framed as data and scanned for
  injection patterns, which are surfaced rather than hidden.
- `email_send` requires a human decision on that specific message at every autonomy
  level and every capability setting.
- Token auth with rate-limited lockout, loopback-only binding by default, an encrypted
  key vault, secret redaction, spend ceilings, and a hash-linked tamper-evident audit
  chain.

### Fixed before release

Found while auditing the first cut of the code. Each has a test that fails without
the fix.

- **`search_files` could read files `read_file` refused.** An agent scoped to a
  directory containing `.env` files, `*.pem` keys or private keys could pull their
  contents out 200 characters at a time through the grep tool, which never consulted
  the protected-path guard. A grep is a read; it now goes through the same guard, and
  reports how many files it skipped rather than dropping them silently.
- **A capability grant of `allow` bypassed the human-approval requirement for
  `email_send`.** Autonomy correctly refused to auto-approve it, but the agent loop
  only consulted autonomy when the grant was `ask` — setting the tool to `allow` in
  the console skipped the gate entirely and sent without asking. The requirement is
  now enforced at the single chokepoint every tool call passes through, so no grant,
  autonomy level or caller can stand in for a human.
- **The network allowlist was checked only on the first hop.** A page on an allowed
  domain could answer with a redirect to any host and the fetch would follow it. Every
  hop is now re-checked against the allowlist.
- **Optional tool arguments were treated as required**, which made most of the email
  tools impossible for an agent to call: `email_list` demanded `folder`, `limit` and
  `unread_only`; every non-reply `email_send` was rejected for a missing `in_reply_to`.
  Each tool now declares the arguments it genuinely needs, and the manual shown to the
  model marks the rest as optional.
- **`hermes run` crashed on a fresh install.** The audit table was created by the
  server and the shell but not by the CLI, so the first audited action hit a missing
  table. `db.init()` now produces a complete database for every entry point.
- **`hermes run` and `hermes agents` found no agents on a fresh install.** The starter
  agents were seeded only when the console or shell started, so the first command in
  the README answered "no agent named Forge". Both now seed on first use.
- **The installer died when piped into bash.** `curl … | bash` leaves `BASH_SOURCE`
  unset, and `set -u` turns that into a hard error — breaking the one command the
  README leads with. It also meant the "install from this directory" branch keyed off
  the current working directory rather than the script's own location. The script now
  requires a real file on disk before taking that branch. A CI job runs the installer
  through a pipe on every push.

[1.4.0]: https://github.com/at0m-b0mb/Hermes-Agent-Console/releases/tag/v1.4.0
[1.3.0]: https://github.com/at0m-b0mb/Hermes-Agent-Console/releases/tag/v1.3.0
[1.2.0]: https://github.com/at0m-b0mb/Hermes-Agent-Console/releases/tag/v1.2.0
[1.1.0]: https://github.com/at0m-b0mb/Hermes-Agent-Console/releases/tag/v1.1.0
[1.0.0]: https://github.com/at0m-b0mb/Hermes-Agent-Console/releases/tag/v1.0.0
