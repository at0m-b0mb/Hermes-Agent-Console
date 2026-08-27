# Changelog

All notable changes to Hermes are recorded here.
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.2.0]: https://github.com/at0m-b0mb/Hermes-Agent-Console/releases/tag/v1.2.0
[1.1.0]: https://github.com/at0m-b0mb/Hermes-Agent-Console/releases/tag/v1.1.0
[1.0.0]: https://github.com/at0m-b0mb/Hermes-Agent-Console/releases/tag/v1.0.0
