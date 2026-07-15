# polytree

**One feature, many repos.**

Your frontend and backend live in separate repositories, and every feature touches both. Git doesn't know they're related: creating a worktree in one doesn't create one in the other, and your coding agent only sees the repo you launched it in — so you keep telling it *"the frontend is over there, modify that too"*.

`polytree` fixes both halves:

```console
$ polytree new checkout-redesign
Creando worktrees 'checkout-redesign' en 2 repos (backend: git)…
  my-api               -> ~/polytree/checkout-redesign/my-api
  my-web               -> ~/polytree/checkout-redesign/my-web
→ claude en:
    host : ~/polytree/checkout-redesign/my-api
    +dir : ~/polytree/checkout-redesign/my-web
```

One command: a worktree on the **same branch** in every repo, plus **one agent session that sees all of them**.

Already have the worktrees? `polytree link` finds the siblings by shared branch name — you never type a path, and you never have to remember what you called them.

## Why not just a monorepo?

If your repos always ship in lockstep, a monorepo is probably the right answer and you don't need this. `polytree` is for teams that have deliberately separate repos and still need a feature to span them.

## Install

Requires **Python 3.11+** and **git**. No packages to install.

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/polytree/main/polytree -o ~/.local/bin/polytree
chmod +x ~/.local/bin/polytree
```

## Configure

`~/.config/polytree/config.toml`:

```toml
backend = "auto"            # auto | git | orca   (auto = orca if installed, else git)
agent   = "claude"          # any key under [agents], or a built-in (claude, codex)
root    = "~/polytree"      # git backend: worktrees live at <root>/<feature>/<repo>

[[repos]]                   # the first repo (or host = true) hosts the agent
path = "~/code/my-api"
base = "origin/main"        # optional; defaults to the repo's origin/HEAD
host = true

[[repos]]
path = "~/code/my-web"
```

## Commands

| Command | What it does |
|---|---|
| `polytree new <name>` | Create a worktree on branch `<name>` in **every** repo, then launch the linked agent |
| `polytree link [branch]` | Existing worktrees: find the siblings and launch the linked agent. No branch = the one you're standing in |
| `polytree paths [branch]` | Print the sibling worktree paths. No side effects |
| `polytree ls` | Show the resolved config (backend, agent, repos) |

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

The practical takeaway: **hooks and settings only ever come from the host repo.** If one side has hooks you can't lose, put it first in the config.

## Backends

- **`git`** (default) — plain `git worktree`. Nothing else required.
- **`orca`** (auto-detected) — creates worktrees through [Orca](https://www.onorca.dev/) and launches the agent in an Orca-managed terminal, so the whole set shows up in the app. Purely a bonus; `polytree` never needs it.

Discovery is always plain git, so `polytree link` and `polytree paths` work on worktrees created by either backend — or by hand.

## Any agent

`claude` and `codex` ship built in. Any agent that accepts extra roots works via config alone — no code changes:

```toml
[agents.my-agent]
attach = "--root {dir}"                                    # how it takes an extra directory
env    = { MY_AGENT_LOAD_EXTRA_CONFIG = "1" }              # optional
attach_if_exists = { ".mcp.json" = "--mcp-config {dir}/.mcp.json" }   # optional, only if the file exists
```

The one thing no wrapper can fix: an agent that can't accept multiple roots at all can't be linked.

## Notes on the workflow

`polytree` handles the mechanics, not the discipline. What still helps:

- **Contract first** — agree on the API/schema before implementing either side.
- **Two PRs, cross-linked** — separate repos mean separate PRs; reference each in the other and merge in dependency order.
- **The branch name is the link.** Same name in every repo is what makes discovery work.

## License

MIT
