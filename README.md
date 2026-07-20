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

Requires **Python 3.11+** and **git 2.36+** (for `worktree list -z`). polytree has
**no third-party dependencies** — it is pure standard library.

**With [pipx](https://pipx.pypa.io) (recommended)** — installs into its own isolated
environment, on your `PATH`, and upgrades/uninstalls cleanly:

```bash
pipx install polytree                                   # once it is on PyPI
pipx install git+https://github.com/briankalid/polytree      # straight from GitHub
```

**With pip**, into whatever environment you like:

```bash
pip install polytree
```

**Single file, no packaging** — polytree is one self-contained script, so you can
also just drop it on your `PATH`:

```bash
curl -fsSL https://raw.githubusercontent.com/briankalid/polytree/main/polytree -o ~/.local/bin/polytree
chmod +x ~/.local/bin/polytree
```

Hacking on polytree itself? Symlink it instead of copying, so the command always
runs what's in your checkout:

```bash
git clone https://github.com/briankalid/polytree && cd polytree
ln -s "$PWD/polytree" ~/.local/bin/polytree
```

### Publishing to PyPI (maintainers)

The version is read straight from the `polytree` script (`VERSION = "…"`), so there
is nothing else to bump. Build and upload with:

```bash
python -m build            # writes dist/polytree-<version>-py3-none-any.whl + .tar.gz
python -m twine upload dist/*
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

Per repo, in order: `--base` on the command line → that repo's `base` → the global `base` → the repo's `origin/HEAD` → its local default branch. If none of those resolve, polytree tells you instead of guessing. The chosen ref is checked in every repo before anything is created.

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
| `polytree list` | Your feature sets: every branch with a worktree in 2+ repos, and whether each is clean or dirty |
| `polytree link [branch]` | Existing worktrees: find the siblings and launch the agent. No branch = the one you're standing in |
| `polytree rm [branch]` | Remove the worktree set. No branch = the one you're standing in. Checks every repo first and removes nothing if any worktree is dirty, locked, or has populated submodules. `--force` overrides all three and deletes the branch even if unmerged. The main checkout is never removed |
| `polytree paths [branch]` | Print the sibling worktree paths. No side effects |
| `polytree ls` | Show the resolved config (backend, agent, repos) |

`new` and `link` take `--host <repo>` to choose which repo runs the agent (default: the first in your config — see the hooks caveat below). `new` takes `--no-launch` to create the worktrees without starting anything, and `--base <ref>` to branch off something other than the configured base — the hotfix case:

```bash
polytree new hotfix-payments --base origin/master   # config still says origin/dev
```

`--base` applies the same ref to every repo, so it wants a name they share (`master`, `main`). If a repo doesn't have it, polytree says which one and creates nothing.

Reviewing a branch someone else pushed? It already exists — locally or on `origin` — so `new` refuses rather than shadow it with a new branch off the base. Use `--existing` to check it out into a worktree set instead:

```bash
polytree new their-feature --existing
```

Pass the plain branch name, not `origin/their-feature`: polytree fetches first, and a branch that only exists on the remote is checked out into a local one for you.

Or point at the pull request and let polytree find the branch (needs [`gh`](https://cli.github.com/)). The PR's branch has to be on `origin` — a PR from a fork is not supported yet, and polytree says so rather than inventing an empty branch:

```bash
polytree new --pr 378              # the PR in the host repo
polytree new --pr my-web#378       # the PR in a specific repo
```

**Qualify the repo.** PR numbers are per-repo, so #378 is a different pull request in each one — polytree names the repo it resolved against every time, and never guesses beyond the host default.

What this does *not* do is attach the PR to Orca's card: that badge is metadata in Orca's own store, which only its UI writes, and git has no such concept. You get the branch, the worktrees and the agent — the work — just not the label. (`--issue <n>` does set a link, and GitHub numbers issues and PRs together, so `--issue 378` will point at the PR — labelled as an issue.)

Both also take `--agent <name>` to override the configured agent for one run, and `--prompt "..."` to hand the agent its task on startup. On the orca backend, `new --issue <n>` links the worktrees to a GitHub issue.

### new, new --existing, or link?

| The branch… | The worktrees… | Use |
|---|---|---|
| doesn't exist | — | `polytree new <branch>` |
| exists (someone's PR) | don't exist yet | `polytree new <branch> --existing` |
| exists | exist | `polytree link <branch>` |

`link` never creates anything; it only attaches the agent to worktrees that are already there.

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

Two things the Orca CLI does that polytree corrects, so both backends behave the same: it slugifies the name it is given into the branch (`feature/x` becomes `feature-x`), and it cannot check out an existing branch — it always creates a new one off the base. polytree renames the branch back to what you asked for, and for `--existing` puts the worktree on the real branch. Orca picks both up; only its directory name stays slugified.

If the slug lands on a branch that already exists, Orca reuses that branch — renaming it would take someone's work, so polytree refuses and tells you which branch is in the way. Asking for `feature/x` while a `feature-x` branch exists is the case to know about.

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
