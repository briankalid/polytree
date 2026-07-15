# polytree

**One feature, many repos.**

Your frontend and backend live in separate repositories, and every feature touches both. Git doesn't know they're related: creating a worktree in one doesn't create one in the other, and your coding agent only sees the repo you launched it in — so you keep telling it *"the frontend is over there, modify that too"*.

`polytree` fixes both halves:

```console
$ polytree new checkout-redesign
Creating 'checkout-redesign' worktrees in 2 repos (backend: git)…
  my-api               -> /home/you/polytree/checkout-redesign/my-api
  my-web               -> /home/you/polytree/checkout-redesign/my-web
→ claude in:
    host : /home/you/polytree/checkout-redesign/my-api
    +dir : /home/you/polytree/checkout-redesign/my-web
```

One command: a worktree on the **same branch** in every repo, plus **one agent session that sees all of them**.

Already have the worktrees? `polytree link` finds the siblings by shared branch name — you never type a path, and you never have to remember what you called them.

## Why not just a monorepo?

If your repos always ship in lockstep, a monorepo is probably the right answer and you don't need this. `polytree` is for teams that have deliberately separate repos and still need a feature to span them.

## Install

Requires **Python 3.11+** and **git 2.36+** (for `worktree list -z`). No packages to install.

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/polytree/main/polytree -o ~/.local/bin/polytree
chmod +x ~/.local/bin/polytree
```

Hacking on polytree itself? Symlink it instead of copying, so the command always
runs what's in your checkout:

```bash
git clone https://github.com/<you>/polytree && cd polytree
ln -s "$PWD/polytree" ~/.local/bin/polytree
```

## Configure

`~/.config/polytree/config.toml`:

```toml
backend = "auto"            # auto | git | orca  (auto = orca if installed, else git)
agent   = "claude"          # any key under [agents], or a built-in (claude, codex)
root    = "~/polytree"      # git backend: worktrees live at <root>/<feature>/<repo>
base    = "origin/main"     # optional global default base ref (both backends)

[[repos]]                   # the first repo (or host = true) hosts the agent
path = "~/code/my-api"
base = "origin/dev"         # optional; overrides the global default
host = true

[[repos]]
path = "~/code/my-web"
```

Each repo's directory name must be unique — it's the folder name under `<root>/<branch>/`. Set `name = "..."` explicitly if two repos share a basename.

### Which ref do the branches start from?

Per repo, in order: that repo's `base` → the global `base` → the repo's `origin/HEAD` → its local default branch. If none of those resolve, polytree tells you instead of guessing.

If you leave `base` unset the two backends differ: the git backend uses `origin/HEAD`, while the orca backend defers to the base ref you configured for that repo in Orca. Set `base` explicitly if you need them to agree.

### Upstream

Branches are created with **no upstream**, on purpose. Branching off a remote ref like `origin/dev` would otherwise make your feature branch track `dev` (git's default `branch.autoSetupMerge`): `git push` then fails and suggests `git push origin HEAD:dev` — which pushes unreviewed work straight onto the shared branch. Publish the normal way instead:

```bash
git push -u origin <branch>
```

## Commands

| Command | What it does |
|---|---|
| `polytree new <name>` | Create a worktree on branch `<name>` in **every** repo, then launch the agent. If any repo fails, everything is rolled back — you never get half a set |
| `polytree link [branch]` | Existing worktrees: find the siblings and launch the agent. No branch = the one you're standing in |
| `polytree rm <branch>` | Remove the worktree set. Checks every repo first and removes nothing if any worktree is dirty, locked, or has populated submodules. `--force` overrides all three and deletes the branch even if unmerged. The main checkout is never removed |
| `polytree paths [branch]` | Print the sibling worktree paths. No side effects |
| `polytree ls` | Show the resolved config (backend, agent, repos) |

`new` and `link` take `--host <repo>` to choose which repo runs the agent (default: the first in your config — see the hooks caveat below). `new` takes `--no-launch` to create the worktrees without starting anything.

## What your agent actually picks up

This is the part nobody documents in one place. When a second repo is attached as an extra directory, **most of its configuration is silently ignored**. `polytree` works around what it can and is honest about the rest.

Verified empirically against **Claude Code 2.1.209**:

| From the attached repo | Loaded by default? | polytree's fix |
|---|---|---|
| `.claude/skills/` | ✅ Yes | — (works out of the box) |
| `CLAUDE.md` | ❌ No — not at startup, **not even lazily** when reading its files | Sets `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` ✅ |
| `.mcp.json` (MCP servers) | ❌ No | Adds `--mcp-config <dir>/.mcp.json` ✅ |
| `settings.json`, hooks, subagents | ❌ No *(per docs; not independently tested)* | **None possible** — make that repo the host |
| `AGENTS.md` | ❌ Claude Code reads `CLAUDE.md`, never `AGENTS.md` | Add `@AGENTS.md` to that repo's `CLAUDE.md`, or symlink |

**Codex** reads `AGENTS.md` hierarchically across roots and needs no env var.

The practical takeaway: **hooks and settings only ever come from the host repo.** The host is the first repo in your config — deliberately not the directory you happen to be standing in, so this stays predictable. If that repo has no worktree for the branch, `link` refuses rather than silently promoting another repo (and changing which hooks load); pass `--host <repo>` to choose deliberately.

## Backends

- **`auto`** (the default) — Orca if its CLI is installed, otherwise git. `polytree ls` and `polytree new` both print which backend is in use.
- **`git`** — plain `git worktree`. Nothing else required. Set this explicitly if you have Orca installed but don't want polytree to use it.
- **`orca`** — creates worktrees through [Orca](https://www.onorca.dev/) and launches the agent in an Orca-managed terminal, so the whole set shows up in the app.

Discovery is always plain git, so `polytree link`, `paths` and `rm` work on worktrees created by either backend. (With the `orca` backend the *launch* goes through Orca, so the host worktree does need to be one Orca knows about.)

## Any agent

`claude` and `codex` ship built in. Any agent that accepts extra roots works via config alone — no code changes:

```toml
[agents.my-agent]
cmd    = "my-agent --fast"                 # optional; defaults to the table key
attach = "--root {dir}"                    # required: how it takes an extra directory
env    = { MY_AGENT_EXTRA = "1" }          # optional
attach_if_exists = { ".mcp.json" = "--mcp-config {dir}/.mcp.json" }   # optional
```

`{dir}` is substituted per attached directory; `attach_if_exists` only fires when that file is present in the attached repo.

The one thing no wrapper can fix: an agent that can't accept multiple roots at all can't be linked.

## Notes on the workflow

`polytree` handles the mechanics, not the discipline. What still helps:

- **Contract first** — agree on the API/schema before implementing either side.
- **Two PRs, cross-linked** — separate repos mean separate PRs; reference each in the other and merge in dependency order.
- **The branch name is the link.** Same name in every repo is what makes discovery work.

## Tests

```bash
python3 -m unittest discover -s tests
```

They cover the failure modes rather than the happy path: partial-failure rollback, Ctrl-C mid-create, re-runs, dirty/locked/submodule refusals, detached/prunable/newline worktrees, and config validation.

## License

MIT
