"""Tests for polytree. Run: python3 -m unittest discover -s tests -v

These deliberately cover failure modes, not the happy path: partial failure,
re-runs, and the porcelain parsing edge cases that a single successful run
would never surface.
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("polytree", str(ROOT / "polytree"))
spec = importlib.util.spec_from_loader("polytree", loader)
pt = importlib.util.module_from_spec(spec)
loader.exec_module(pt)


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    ).stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.api = make_repo(self.tmp / "repos" / "api")
        self.web = make_repo(self.tmp / "repos" / "web")

    def write_config(self, extra: str = "", api_base: str = "main", web_base: str = "main") -> dict:
        cfg = self.tmp / "config.toml"
        cfg.write_text(
            f'backend = "git"\nagent = "fake"\nroot = "{self.tmp}/wt"\n\n'
            f'[[repos]]\npath = "{self.api}"\nbase = "{api_base}"\nhost = true\n\n'
            f'[[repos]]\npath = "{self.web}"\nbase = "{web_base}"\n\n'
            f'[agents.fake]\ncmd = "true"\nattach = "--add-dir {{dir}}"\n{extra}'
        )
        pt.CONFIG = cfg
        return pt.load_config()

    def new(self, cfg, name, **kw):
        d = dict(name=name, host=None, no_launch=True, base=None, existing=False,
                 agent=None, issue=None, prompt=None, pr=None)
        d.update(kw)
        args = argparse.Namespace(**d)
        pt.cmd_new(cfg, args)


class TestWorktreeParsing(Base):
    def test_skips_detached(self):
        """A detached worktree has no branch line; it must not leak into the map."""
        git(self.api, "worktree", "add", "-q", "--detach", str(self.tmp / "det"))
        self.assertNotIn("HEAD", pt.worktrees_of(str(self.api)))
        self.assertEqual(list(pt.worktrees_of(str(self.api))), ["main"])

    def test_skips_prunable(self):
        """A worktree whose directory is gone is still listed by git until pruned."""
        wt = self.tmp / "gone"
        git(self.api, "worktree", "add", "-q", "-b", "ghost", str(wt))
        subprocess.run(["rm", "-rf", str(wt)])
        self.assertNotIn("ghost", pt.worktrees_of(str(self.api)))

    def test_path_with_newline(self):
        """-z parsing: a path containing a newline must survive intact."""
        wt = self.tmp / "we\nird"
        git(self.api, "worktree", "add", "-q", "-b", "nl", str(wt))
        self.assertEqual(pt.worktrees_of(str(self.api))["nl"], str(wt))

    def test_path_with_spaces(self):
        wt = self.tmp / "with spaces"
        git(self.api, "worktree", "add", "-q", "-b", "sp", str(wt))
        self.assertEqual(pt.worktrees_of(str(self.api))["sp"], str(wt))


class TestRealBranchShapes(Base):
    """Every branch name in this suite used to be lowercase and slash-free, which
    is not what real repos look like. These are the shapes from the repos polytree
    was built against: Msf/merge-in-to-dev, chore/add_new_builtins, ReginaRRJ-new_uma.
    Two bugs hid in the gap between those and 'feat'.
    """

    SHAPES = [
        "Msf/merge-in-to-dev",  # capital + slash
        "chore/add_new_builtins",  # underscore + slash
        "ReginaRRJ-new_uma",  # capitals, no slash
        "feature/proposal-auto_load_swagger",  # the long real one
        "release/v1.2.3",  # dots
        "feat/a/b/c",  # nested slashes
    ]

    def test_new_list_and_rm_survive_every_shape(self):
        cfg = self.write_config()
        for name in self.SHAPES:
            with self.subTest(branch=name):
                self.new(cfg, name)
                self.assertIn(name, pt.worktrees_of(str(self.api)), f"{name} not created")
                self.assertIn(name, pt.worktrees_of(str(self.web)))
                # discovery must find it under the exact name asked for
                self.assertEqual(
                    [p for _, p in pt.sibling_map(cfg, name) if p],
                    [pt.worktrees_of(str(r))[name] for r in (self.api, self.web)],
                )
                pt.cmd_rm(cfg, argparse.Namespace(branch=name, force=True))
                self.assertNotIn(name, pt.worktrees_of(str(self.api)))

    def test_nested_slashes_do_not_strand_directories(self):
        """<root>/feat/a/b/c/<repo> is four levels deep; cleanup has to unwind it."""
        cfg = self.write_config()
        self.new(cfg, "feat/a/b/c")
        pt.cmd_rm(cfg, argparse.Namespace(branch="feat/a/b/c", force=True))
        self.assertFalse((self.tmp / "wt" / "feat").exists(), "empty shell left behind")

    def test_case_differing_branches_are_not_confused(self):
        """On a case-insensitive filesystem these would collide; on Linux they
        are two branches and must stay two."""
        cfg = self.write_config()
        self.new(cfg, "Feature/X")
        self.assertIn("Feature/X", pt.worktrees_of(str(self.api)))
        self.assertNotIn("feature/x", pt.worktrees_of(str(self.api)))


class TestBuildArgv(Base):
    def test_cmd_key_and_attach(self):
        cfg = self.write_config()
        argv, env = pt.build_argv(cfg, cfg["agents"]["fake"], ["/a", "/b"])
        self.assertEqual(argv, ["true", "--add-dir", "/a", "--add-dir", "/b"])
        self.assertEqual(env, {})

    def test_attach_if_exists_only_when_present(self):
        cfg = self.write_config(
            extra='attach_if_exists = { ".mcp.json" = "--mcp-config {dir}/.mcp.json" }\n'
        )
        (self.tmp / "has").mkdir()
        (self.tmp / "has" / ".mcp.json").write_text("{}")
        (self.tmp / "hasnt").mkdir()
        spec_ = cfg["agents"]["fake"]
        argv, _ = pt.build_argv(cfg, spec_, [str(self.tmp / "has")])
        self.assertIn("--mcp-config", argv)
        argv, _ = pt.build_argv(cfg, spec_, [str(self.tmp / "hasnt")])
        self.assertNotIn("--mcp-config", argv)

    def test_literal_braces_do_not_crash(self):
        """expand() uses replace(), not format(): braces are data, not a template."""
        self.assertEqual(pt.expand("--filter {a} {dir}", "/x"), ["--filter", "{a}", "/x"])

    def test_env_is_carried(self):
        cfg = self.write_config(extra='env = { FOO = "1" }\n')
        _, env = pt.build_argv(cfg, cfg["agents"]["fake"], ["/a"])
        self.assertEqual(env, {"FOO": "1"})


class TestConfigValidation(Base):
    def test_duplicate_repo_names_rejected(self):
        a = make_repo(self.tmp / "x" / "api")
        b = make_repo(self.tmp / "y" / "api")  # same basename
        cfg = self.tmp / "dup.toml"
        cfg.write_text(f'backend = "git"\n[[repos]]\npath = "{a}"\n\n[[repos]]\npath = "{b}"\n')
        pt.CONFIG = cfg
        with self.assertRaises(pt.Fail) as e:
            pt.load_config()
        self.assertIn("duplicate repo name", str(e.exception))

    def test_unknown_agent_rejected(self):
        cfg = self.write_config()
        cfg["agent"] = "nope"
        with self.assertRaises(pt.Fail) as e:
            pt.resolve_agent(cfg)
        self.assertIn("unknown agent", str(e.exception))

    def test_missing_agent_binary_rejected(self):
        cfg = self.write_config()
        cfg["agents"]["fake"]["cmd"] = "definitely-not-a-real-binary-xyz"
        with self.assertRaises(pt.Fail) as e:
            pt.resolve_agent(cfg)
        self.assertIn("not found in PATH", str(e.exception))

    def test_host_ordering(self):
        cfg = self.write_config()
        self.assertEqual(cfg["repos"][0]["name"], "api")  # host = true wins

    def test_bad_branch_names_rejected(self):
        for bad in ("-x", "a..b", "has space", ""):
            with self.assertRaises(pt.Fail):
                pt.check_branch_name(bad)


class TestUpstream(Base):
    def test_branch_gets_no_upstream_when_based_on_a_remote_ref(self):
        """Branching off origin/dev must NOT leave the feature tracking dev:
        git would then suggest `git push origin HEAD:dev` on a failed push."""
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        git(self.api, "remote", "add", "origin", str(origin))
        git(self.api, "push", "-q", "-u", "origin", "main")
        git(self.api, "checkout", "-q", "-b", "dev")
        git(self.api, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "dev")
        git(self.api, "push", "-q", "-u", "origin", "dev")
        git(self.api, "checkout", "-q", "main")

        cfg = self.write_config(api_base="origin/dev")
        self.new(cfg, "feat-up")
        wt = pt.worktrees_of(str(self.api))["feat-up"]
        upstream = subprocess.run(
            ["git", "-C", wt, "rev-parse", "--abbrev-ref", "@{upstream}"], capture_output=True, text=True
        )
        self.assertNotEqual(upstream.returncode, 0, f"expected no upstream, got {upstream.stdout.strip()!r}")
        # …and the base is still right: the branch must contain dev's commit.
        self.assertEqual(git(wt, "log", "--oneline", "-1", "--format=%s").strip(), "dev")


class TestNewAndRollback(Base):
    def test_creates_in_every_repo(self):
        cfg = self.write_config()
        self.new(cfg, "feat")
        self.assertIn("feat", pt.worktrees_of(str(self.api)))
        self.assertIn("feat", pt.worktrees_of(str(self.web)))

    def test_preflight_existing_branch_creates_nothing(self):
        """Re-running new must fail cleanly, without creating a partial set."""
        git(self.web, "branch", "collide", "main")
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "collide")
        self.assertIn("already exists", str(e.exception))
        self.assertNotIn("collide", pt.worktrees_of(str(self.api)))  # api untouched

    def test_preflight_rejects_a_bad_base_before_creating_anything(self):
        """A base that doesn't resolve is caught up front, so there is nothing
        to roll back in the first place."""
        cfg = self.write_config(web_base="origin/does-not-exist")
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "badbase")
        self.assertIn("does not exist", str(e.exception))
        self.assertIn("nothing was created", str(e.exception))
        self.assertNotIn("badbase", pt.worktrees_of(str(self.api)))

    def test_rollback_when_a_later_repo_fails(self):
        """A failure the preflight cannot foresee (disk, races, git itself):
        api is created first, then must be rolled back."""
        cfg = self.write_config()
        real = pt.create_git

        def boom(c, repo, branch, override=None, existing=False):
            if repo["name"] == "web":
                pt.die("simulated failure creating web's worktree")
            return real(c, repo, branch, override, existing)

        pt.create_git = boom
        self.addCleanup(lambda: setattr(pt, "create_git", real))
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "boom")
        self.assertIn("Rolled back", str(e.exception))
        self.assertNotIn("boom", pt.worktrees_of(str(self.api)))
        self.assertNotIn("boom", git(self.api, "branch", "--list", "boom"))  # branch gone too

    def test_rerun_after_rollback_succeeds(self):
        """The whole point of rolling back: you are not wedged."""
        cfg = self.write_config(web_base="origin/does-not-exist")
        with self.assertRaises(pt.Fail):
            self.new(cfg, "retry")
        cfg = self.write_config()  # base fixed
        self.new(cfg, "retry")
        self.assertIn("retry", pt.worktrees_of(str(self.api)))
        self.assertIn("retry", pt.worktrees_of(str(self.web)))


class TestRm(Base):
    def test_rm_removes_worktrees_and_branch(self):
        cfg = self.write_config()
        self.new(cfg, "gone")
        pt.cmd_rm(cfg, argparse.Namespace(branch="gone", force=True))
        self.assertNotIn("gone", pt.worktrees_of(str(self.api)))
        self.assertNotIn("gone", pt.worktrees_of(str(self.web)))
        self.assertEqual(git(self.api, "branch", "--list", "gone").strip(), "")

    def test_backend_deleting_the_branch_does_not_lose_unmerged_work(self):
        """`orca worktree rm` deletes the branch too, merged or not. Without
        --force the branch must survive anyway, and the report must not claim
        'kept' when it is gone (or vice versa)."""
        cfg = self.write_config()
        self.new(cfg, "keepme")
        wt = pt.worktrees_of(str(self.api))["keepme"]
        git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "unmerged work")
        sha = git(self.api, "rev-parse", "refs/heads/keepme").strip()

        real = pt.remove_worktree

        def orca_like(c, repo, path, force):  # simulate: worktree AND branch gone
            real(c, repo, path, force)
            git(repo["path"], "branch", "-D", "keepme", check=False)

        pt.remove_worktree = orca_like
        self.addCleanup(lambda: setattr(pt, "remove_worktree", real))
        pt.cmd_rm(cfg, argparse.Namespace(branch="keepme", force=False))

        self.assertNotIn("keepme", pt.worktrees_of(str(self.api)))  # worktree gone
        self.assertTrue(pt.branch_exists(cfg["repos"][0], "keepme"))  # unmerged branch restored
        self.assertEqual(git(self.api, "rev-parse", "refs/heads/keepme").strip(), sha)  # same commit

    def test_rm_force_still_deletes_the_branch(self):
        cfg = self.write_config()
        self.new(cfg, "bye")
        git(pt.worktrees_of(str(self.api))["bye"], "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "unmerged")
        pt.cmd_rm(cfg, argparse.Namespace(branch="bye", force=True))
        self.assertFalse(pt.branch_exists(cfg["repos"][0], "bye"))

    def test_rm_reports_the_kept_branch_and_how_to_drop_it(self):
        """The note and the hint were deletable green: nothing captured cmd_rm's
        output, while a docstring claimed the report was under test."""
        cfg = self.write_config()
        self.new(cfg, "keptnote")
        git(pt.worktrees_of(str(self.api))["keptnote"], "-c", "user.email=t@t", "-c",
            "user.name=t", "commit", "-q", "--allow-empty", "-m", "unmerged")
        out = []
        real = pt.info
        self.addCleanup(lambda: setattr(pt, "info", real))
        pt.info = out.append
        pt.cmd_rm(cfg, argparse.Namespace(branch="keptnote", force=False))
        text = "\n".join(out)
        self.assertIn("branch kept: not merged", text)
        self.assertIn(f"git -C {self.api} branch -D keptnote", text)  # a command that works
        self.assertTrue(pt.branch_exists(cfg["repos"][0], "keptnote"))  # and it is true

    def test_rm_says_nothing_about_keeping_a_branch_it_deleted(self):
        cfg = self.write_config()
        self.new(cfg, "gonenote")  # merged into main: -d succeeds
        out = []
        real = pt.info
        self.addCleanup(lambda: setattr(pt, "info", real))
        pt.info = out.append
        pt.cmd_rm(cfg, argparse.Namespace(branch="gonenote", force=False))
        self.assertNotIn("branch kept", "\n".join(out))
        self.assertFalse(pt.branch_exists(cfg["repos"][0], "gonenote"))

    def test_rm_unknown_branch_fails(self):
        cfg = self.write_config()
        with self.assertRaises(pt.Fail):
            pt.cmd_rm(cfg, argparse.Namespace(branch="never", force=False))

    def test_rm_refuses_to_destroy_uncommitted_work(self):
        """Without --force, git refuses to remove a dirty worktree. Don't override that."""
        cfg = self.write_config()
        self.new(cfg, "wip")
        wt = pt.worktrees_of(str(self.api))["wip"]
        (Path(wt) / "IMPORTANT.txt").write_text("uncommitted work")
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_rm(cfg, argparse.Namespace(branch="wip", force=False))
        self.assertIn("uncommitted changes", str(e.exception))
        self.assertTrue((Path(wt) / "IMPORTANT.txt").exists())  # survived
        self.assertIn("wip", pt.worktrees_of(str(self.api)))

    def test_rm_preflight_leaves_nothing_half_removed(self):
        """web is dirty -> api must NOT be removed first."""
        cfg = self.write_config()
        self.new(cfg, "half")
        (Path(pt.worktrees_of(str(self.web))["half"]) / "dirty.txt").write_text("x")
        with self.assertRaises(pt.Fail):
            pt.cmd_rm(cfg, argparse.Namespace(branch="half", force=False))
        self.assertIn("half", pt.worktrees_of(str(self.api)))  # untouched
        self.assertIn("half", pt.worktrees_of(str(self.web)))

    def test_rm_force_does_discard(self):
        cfg = self.write_config()
        self.new(cfg, "nuke")
        (Path(pt.worktrees_of(str(self.api))["nuke"]) / "dirty.txt").write_text("x")
        pt.cmd_rm(cfg, argparse.Namespace(branch="nuke", force=True))
        self.assertNotIn("nuke", pt.worktrees_of(str(self.api)))
        self.assertNotIn("nuke", pt.worktrees_of(str(self.web)))
        self.assertEqual(git(self.api, "branch", "--list", "nuke").strip(), "")


class TestAgentValidatedBeforeSideEffects(Base):
    """The fix is the ORDER: a bad agent must not leave worktrees behind."""

    def test_unknown_agent_creates_nothing(self):
        cfg = self.write_config()
        cfg["agent"] = "typo-agent"
        with self.assertRaises(pt.Fail):
            pt.cmd_new(cfg, argparse.Namespace(name="x", host=None, no_launch=False, base=None, existing=False, agent=None, issue=None, prompt=None, pr=None))
        self.assertNotIn("x", pt.worktrees_of(str(self.api)))
        self.assertNotIn("x", pt.worktrees_of(str(self.web)))

    def test_missing_binary_creates_nothing(self):
        cfg = self.write_config()
        cfg["agents"]["fake"]["cmd"] = "definitely-not-a-real-binary-xyz"
        with self.assertRaises(pt.Fail):
            pt.cmd_new(cfg, argparse.Namespace(name="y", host=None, no_launch=False, base=None, existing=False, agent=None, issue=None, prompt=None, pr=None))
        self.assertNotIn("y", pt.worktrees_of(str(self.api)))


class TestLockedWorktrees(Base):
    def test_rm_refuses_locked_and_removes_nothing(self):
        """git refuses to remove a locked worktree; the preflight must catch it."""
        cfg = self.write_config()
        self.new(cfg, "lk")
        git(self.web, "worktree", "lock", pt.worktrees_of(str(self.web))["lk"])
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_rm(cfg, argparse.Namespace(branch="lk", force=False))
        self.assertIn("is locked", str(e.exception))
        self.assertIn("lk", pt.worktrees_of(str(self.api)))  # api NOT half-removed
        self.assertIn("lk", pt.worktrees_of(str(self.web)))

    def test_rm_force_removes_locked(self):
        cfg = self.write_config()
        self.new(cfg, "lk2")
        git(self.web, "worktree", "lock", pt.worktrees_of(str(self.web))["lk2"])
        pt.cmd_rm(cfg, argparse.Namespace(branch="lk2", force=True))
        self.assertNotIn("lk2", pt.worktrees_of(str(self.web)))

    def test_rm_refuses_populated_submodules_and_removes_nothing(self):
        """git refuses worktrees with populated submodules even when clean, and
        `status --porcelain` shows nothing — the case that slipped three times."""
        sub = make_repo(self.tmp / "repos" / "sub")
        git(
            self.web, "-c", "protocol.file.allow=always", "-c", "user.email=t@t",
            "-c", "user.name=t", "submodule", "add", "-q", str(sub), "vendor",
        )
        git(self.web, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add submodule")
        cfg = self.write_config()
        self.new(cfg, "sm")
        wt = pt.worktrees_of(str(self.web))["sm"]
        git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
        self.assertTrue(pt.has_populated_submodules(wt))
        self.assertFalse(pt.is_dirty(wt))  # clean: is_dirty would never catch this

        with self.assertRaises(pt.Fail) as e:
            pt.cmd_rm(cfg, argparse.Namespace(branch="sm", force=False))
        self.assertIn("populated submodules", str(e.exception))
        self.assertIn("sm", pt.worktrees_of(str(self.api)))  # api NOT half-removed
        self.assertIn("sm", pt.worktrees_of(str(self.web)))

    def test_rm_never_touches_main_checkout(self):
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_rm(cfg, argparse.Namespace(branch="main", force=True))
        self.assertIn("main checkout", str(e.exception))
        self.assertTrue((self.api / ".git").exists())


class TestRollbackOnInterrupt(Base):
    def test_keyboard_interrupt_rolls_back(self):
        """Ctrl-C mid-create must not leave a half-made set either."""
        cfg = self.write_config()
        real = pt.create_git
        calls = []

        def boom(c, repo, branch, override=None, existing=False):
            if repo["name"] == "web":
                raise KeyboardInterrupt
            calls.append(repo["name"])
            return real(c, repo, branch, override, existing)

        pt.create_git = boom
        self.addCleanup(lambda: setattr(pt, "create_git", real))
        with self.assertRaises(KeyboardInterrupt):
            self.new(cfg, "irq")
        self.assertEqual(calls, ["api"])
        self.assertNotIn("irq", pt.worktrees_of(str(self.api)))  # rolled back
        self.assertEqual(git(self.api, "branch", "--list", "irq").strip(), "")  # branch too


class TestLink(Base):
    def test_needs_two_worktrees(self):
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_link(cfg, argparse.Namespace(branch="nope", host=None, agent=None, prompt=None))
        self.assertIn("need >=2", str(e.exception))
        self.assertIn("polytree new nope", str(e.exception))
        self.assertNotIn("--existing", str(e.exception))  # no branch -> plain new

    def test_suggests_existing_when_the_branch_is_already_there(self):
        """A colleague's branch with no worktrees: plain `new` would refuse, so
        the hint must not send you into it."""
        for r in (self.api, self.web):
            git(r, "branch", "their-pr", "main")
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_link(cfg, argparse.Namespace(branch="their-pr", host=None, agent=None, prompt=None))
        self.assertIn("polytree new their-pr --existing", str(e.exception))

    def test_refuses_to_shift_host_silently(self):
        """Host (api) lacks the worktree but web+lib have it: 2 present, so the
        >=2 check passes and the host would silently shift to web. Needs 3 repos
        to reproduce at all.
        """
        lib = make_repo(self.tmp / "repos" / "lib")
        cfg_file = self.tmp / "three.toml"
        cfg_file.write_text(
            f'backend = "git"\nagent = "fake"\nroot = "{self.tmp}/wt3"\n\n'
            f'[[repos]]\npath = "{self.api}"\nhost = true\n\n'
            f'[[repos]]\npath = "{self.web}"\n\n[[repos]]\npath = "{lib}"\n\n'
            f'[agents.fake]\ncmd = "true"\nattach = "--add-dir {{dir}}"\n'
        )
        pt.CONFIG = cfg_file
        cfg = pt.load_config()
        for r in (self.web, lib):  # only the two non-host repos get a worktree
            git(r, "worktree", "add", "-q", "-b", "shift2", str(self.tmp / f"s-{r.name}"))

        with self.assertRaises(pt.Fail) as e:
            pt.cmd_link(cfg, argparse.Namespace(branch="shift2", host=None, agent=None, prompt=None))
        self.assertIn("has no worktree", str(e.exception))

    def test_explicit_host_allows_the_shift(self):
        """--host makes it a decision instead of a silent surprise."""
        lib = make_repo(self.tmp / "repos" / "lib")
        cfg_file = self.tmp / "three.toml"
        cfg_file.write_text(
            f'backend = "git"\nagent = "fake"\nroot = "{self.tmp}/wt3"\n\n'
            f'[[repos]]\npath = "{self.api}"\nhost = true\n\n'
            f'[[repos]]\npath = "{self.web}"\n\n[[repos]]\npath = "{lib}"\n\n'
            f'[agents.fake]\ncmd = "true"\nattach = "--add-dir {{dir}}"\n'
        )
        pt.CONFIG = cfg_file
        cfg = pt.load_config()
        for r in (self.web, lib):
            git(r, "worktree", "add", "-q", "-b", "shift3", str(self.tmp / f"t-{r.name}"))
        launched = {}
        real_launch = pt.launch
        self.addCleanup(lambda: setattr(pt, "launch", real_launch))
        pt.launch = lambda c, s, host, others, prompt=None: launched.update(host=host, others=others)
        pt.cmd_link(cfg, argparse.Namespace(branch="shift3", host="web", agent=None, prompt=None))
        self.assertEqual(launched["host"], str(self.tmp / "t-web"))
        self.assertEqual(launched["others"], [str(self.tmp / "t-lib")])


class TestDefaultBase(Base):
    def test_falls_back_to_local_head(self):
        """No origin/HEAD (no remote at all) -> use the local default branch."""
        self.assertEqual(pt.default_base({"path": str(self.api), "name": "api"}), "main")


class TestEmptyDirCleanup(Base):
    def test_rm_leaves_no_empty_shell(self):
        cfg = self.write_config()
        self.new(cfg, "shell")
        pt.cmd_rm(cfg, argparse.Namespace(branch="shell", force=True))
        self.assertFalse((self.tmp / "wt" / "shell").exists())


class TestExisting(Base):
    def test_checks_out_an_existing_branch(self):
        """Reviewing someone's branch: it exists in both repos already."""
        for r in (self.api, self.web):
            git(r, "branch", "colleague-pr", "main")
        cfg = self.write_config()
        self.new(cfg, "colleague-pr", existing=True)
        self.assertIn("colleague-pr", pt.worktrees_of(str(self.api)))
        self.assertIn("colleague-pr", pt.worktrees_of(str(self.web)))

    def test_without_existing_an_existing_branch_is_rejected(self):
        git(self.api, "branch", "there", "main")
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "there")
        self.assertIn("--existing", str(e.exception))  # tells you the way out

    def test_existing_needs_the_branch_in_every_repo(self):
        git(self.api, "branch", "half", "main")  # only api has it
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "half", existing=True)
        self.assertIn("neither locally nor on origin in web", str(e.exception))
        self.assertNotIn("half", pt.worktrees_of(str(self.api)))  # nothing created

    def test_existing_with_base_is_rejected(self):
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "x", existing=True, base="origin/master")
        self.assertIn("--base", str(e.exception))


class TestExistingFromRemote(Base):
    def _with_origin(self):
        """Both repos share an origin that has a branch neither has locally —
        i.e. a colleague just pushed their PR."""
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        for r in (self.api, self.web):
            git(r, "remote", "add", "origin", str(origin))
        git(self.api, "push", "-q", "origin", "main")
        git(self.api, "checkout", "-q", "-b", "their-pr")
        git(self.api, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
            "-m", "their work")
        git(self.api, "push", "-q", "origin", "their-pr")
        git(self.api, "checkout", "-q", "main")
        git(self.api, "branch", "-q", "-D", "their-pr")  # remote-only from here on
        for r in (self.api, self.web):
            git(r, "fetch", "-q", "origin")

    def test_existing_works_on_a_remote_only_branch(self):
        """The whole point of --existing: a branch you have not checked out yet.
        git creates the local branch from origin/<branch> by itself."""
        self._with_origin()
        self.assertFalse(pt.git_ok(str(self.api), "show-ref", "--verify", "--quiet",
                                   "refs/heads/their-pr"))  # nothing local
        cfg = self.write_config()
        self.new(cfg, "their-pr", existing=True)
        wt = pt.worktrees_of(str(self.api))["their-pr"]
        self.assertEqual(git(wt, "log", "--oneline", "-1", "--format=%s").strip(), "their work")

    def test_existing_rejects_a_branch_nobody_has(self):
        self._with_origin()
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "ghost-branch", existing=True)
        self.assertIn("neither locally nor on origin", str(e.exception))


class TestPrSpec(Base):
    """--pr resolution. PR numbers are per-repo: in the real repos this was built
    against, #378 is a different pull request in each one, so an unqualified
    number must never quietly reach for the wrong repo."""

    def _fake_gh(self, mapping):
        """mapping: repo name -> branch that `gh pr view` should return."""
        real = pt.run
        self.addCleanup(lambda: setattr(pt, "run", real))

        def fake(args, cwd=None, check=True):
            if args and args[0] == "gh":
                for name, branch in mapping.items():
                    if cwd and cwd.endswith("/" + name):
                        return branch + "\n"
                return ""
            return real(args, cwd=cwd, check=check)

        pt.run = fake

    def test_unqualified_uses_the_host_repo(self):
        cfg = self.write_config()
        self._fake_gh({"api": "from-api", "web": "from-web"})
        repo, number, branch = pt.branch_of_pr(cfg, "378")
        self.assertEqual((repo["name"], number, branch), ("api", "378", "from-api"))

    def test_qualified_picks_that_repo(self):
        cfg = self.write_config()
        self._fake_gh({"api": "from-api", "web": "from-web"})
        repo, _, branch = pt.branch_of_pr(cfg, "web#378")
        self.assertEqual((repo["name"], branch), ("web", "from-web"))

    def test_unknown_repo_rejected(self):
        cfg = self.write_config()
        self._fake_gh({"api": "x"})
        with self.assertRaises(pt.Fail) as e:
            pt.branch_of_pr(cfg, "nope#1")
        self.assertIn("unknown repo", str(e.exception))

    def test_non_numeric_rejected(self):
        cfg = self.write_config()
        for bad in ("abc", "web#abc", ""):
            with self.assertRaises(pt.Fail):
                pt.branch_of_pr(cfg, bad)

    def test_unresolvable_pr_rejected(self):
        cfg = self.write_config()
        self._fake_gh({})  # gh returns nothing
        with self.assertRaises(pt.Fail) as e:
            pt.branch_of_pr(cfg, "999")
        self.assertIn("could not resolve PR #999", str(e.exception))

    def test_pr_with_a_name_is_rejected(self):
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "some-name", pr="1")
        self.assertIn("drop the name", str(e.exception))


class TestAgentAndPromptOverrides(Base):
    def test_agent_override(self):
        cfg = self.write_config(extra='\n[agents.other]\ncmd = "echo"\nattach = "--dir {dir}"\n')
        launched = {}
        real = pt.launch
        self.addCleanup(lambda: setattr(pt, "launch", real))
        pt.launch = lambda c, s, h, o, prompt=None: launched.update(agent=c["agent"], spec=s)
        pt.cmd_new(cfg, argparse.Namespace(name="ov", host=None, no_launch=False, base=None,
                                           existing=False, agent="other", issue=None, prompt=None, pr=None))
        self.assertEqual(launched["agent"], "other")
        self.assertEqual(launched["spec"]["attach"], "--dir {dir}")

    def test_prompt_is_appended_as_positional(self):
        cfg = self.write_config()
        argv, _ = pt.build_argv(cfg, cfg["agents"]["fake"], ["/a"], "implement the thing")
        self.assertEqual(argv[-1], "implement the thing")

    def test_no_prompt_appends_nothing(self):
        cfg = self.write_config()
        argv, _ = pt.build_argv(cfg, cfg["agents"]["fake"], ["/a"])
        self.assertEqual(argv, ["true", "--add-dir", "/a"])

    def test_issue_needs_orca_backend(self):
        cfg = self.write_config()  # backend = git
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "iss", issue="42")
        self.assertIn("orca backend", str(e.exception))


class TestList(Base):
    def test_lists_only_sets_present_in_two_repos(self):
        cfg = self.write_config()
        self.new(cfg, "both-repos")
        git(self.api, "worktree", "add", "-q", "-b", "api-only", str(self.tmp / "solo"))
        out = []
        real = pt.info
        self.addCleanup(lambda: setattr(pt, "info", real))
        pt.info = out.append
        pt.cmd_list(cfg, argparse.Namespace())
        text = "\n".join(out)
        self.assertIn("both-repos", text)
        self.assertIn("(2/2 repos)", text)
        self.assertNotIn("api-only", text)  # a lone worktree is not a feature set
        self.assertNotIn("main", text)  # main checkouts are not either

    def test_reports_dirty(self):
        cfg = self.write_config()
        self.new(cfg, "messy")
        (Path(pt.worktrees_of(str(self.web))["messy"]) / "x.txt").write_text("x")
        out = []
        real = pt.info
        self.addCleanup(lambda: setattr(pt, "info", real))
        pt.info = out.append
        pt.cmd_list(cfg, argparse.Namespace())
        text = "\n".join(out)
        self.assertRegex(text, r"web\s+dirty")
        self.assertRegex(text, r"api\s+clean")


class TestOrcaBackend(Base):
    """The orca backend had no coverage, which is how its worst bug survived.
    Orca is stubbed here: the point is polytree's logic, not Orca's."""

    def _fake_orca(self):
        """Stand in for `orca worktree create`, slug and all.

        Orca turns the name into a branch by replacing '/' with '-', and if that
        branch already exists it checks it out instead of making a new one — both
        verified against the real CLI. Its `rm` deletes the branch too.
        """
        real_run, real_bin = pt.run, pt.orca_bin
        self.addCleanup(lambda: (setattr(pt, "run", real_run), setattr(pt, "orca_bin", real_bin)))
        pt.orca_bin = lambda: "fake-orca"

        def fake(args, cwd=None, check=True):
            if args and args[0] == "fake-orca" and "create" in args:
                name = args[args.index("--name") + 1]
                slug = name.replace("/", "-")
                sel = args[args.index("--repo") + 1].removeprefix("path:")
                dest = str(self.tmp / "orcawt" / Path(sel).name / slug)  # per-repo, like Orca
                if slug in pt.local_branches({"path": sel}):
                    git(sel, "worktree", "add", "-q", dest, slug)  # reuse, like Orca
                else:
                    git(sel, "worktree", "add", "-q", "-b", slug, dest, "main")
                return "{}"
            if args and args[0] == "fake-orca" and "rm" in args:
                # The real `orca worktree rm` takes the branch with it, merged or
                # not. Stubbing this as a no-op made polytree's whole reason for
                # restoring branches pass by construction.
                target = args[args.index("--worktree") + 1].removeprefix("path:")
                for repo in (self.api, self.web):
                    for branch, path in pt.worktrees_of(str(repo)).items():
                        if path == target:
                            git(repo, "worktree", "remove", "--force", target, check=False)
                            git(repo, "branch", "-D", branch, check=False)
                return "{}"
            if args and args[0] == "fake-orca":
                return "{}"
            return real_run(args, cwd=cwd, check=check)

        pt.run = fake

    def _orca_config(self):
        cfg = self.tmp / "orca.toml"
        cfg.write_text(
            f'backend = "orca"\nagent = "fake"\n\n[[repos]]\npath = "{self.api}"\nhost = true\n\n'
            f'[[repos]]\npath = "{self.web}"\n\n[agents.fake]\ncmd = "true"\nattach = "--add-dir {{dir}}"\n'
        )
        pt.CONFIG = cfg
        return pt.load_config()

    def test_slugified_branch_is_renamed_back(self):
        """Orca makes 'feature-x'; the user asked for 'feature/x' and must get it."""
        self._fake_orca()
        cfg = self._orca_config()
        self.new(cfg, "feature/x")
        self.assertIn("feature/x", pt.worktrees_of(str(self.api)))
        self.assertNotIn("feature-x", pt.local_branches(cfg["repos"][0]))  # no orphan slug

    def test_refuses_to_steal_a_branch_the_slug_collides_with(self):
        """'zz-collide' already exists and is someone's work. Asking for
        'zz/collide' makes Orca reuse it — renaming it would be theft."""
        for r in (self.api, self.web):
            git(r, "branch", "zz-collide", "main")
        sha = git(self.api, "rev-parse", "zz-collide").strip()
        self._fake_orca()
        cfg = self._orca_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "zz/collide")
        self.assertIn("already exists", str(e.exception))
        self.assertIn("zz-collide", pt.local_branches(cfg["repos"][0]))  # survived
        self.assertEqual(git(self.api, "rev-parse", "zz-collide").strip(), sha)  # untouched
        self.assertNotIn("zz/collide", pt.local_branches(cfg["repos"][0]))
        self.assertNotIn("zz-collide", pt.worktrees_of(str(self.api)))  # worktree taken back


class TestRollbackNeverTakesSomeoneElsesBranch(Base):
    """--existing/--pr work on branches polytree did not create. A partial
    failure must take back the worktrees and nothing else."""

    def test_rollback_keeps_a_preexisting_branch(self):
        for r in (self.api, self.web):
            git(r, "branch", "my-wip", "main")
        git(self.api, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "unpushed")  # work only on api's branch tip
        git(self.api, "branch", "-f", "my-wip", "HEAD")
        sha = git(self.api, "rev-parse", "my-wip").strip()

        cfg = self.write_config()
        real = pt.create_git

        def boom(c, repo, branch, override=None, existing=False):
            if repo["name"] == "web":
                pt.die("simulated failure on the second repo")
            return real(c, repo, branch, override, existing)

        pt.create_git = boom
        self.addCleanup(lambda: setattr(pt, "create_git", real))
        with self.assertRaises(pt.Fail):
            self.new(cfg, "my-wip", existing=True)

        self.assertIn("my-wip", pt.local_branches(cfg["repos"][0]))  # survived
        self.assertEqual(git(self.api, "rev-parse", "my-wip").strip(), sha)  # untouched
        self.assertNotIn("my-wip", pt.worktrees_of(str(self.api)))  # worktree taken back

    def test_rollback_does_delete_a_branch_we_created(self):
        cfg = self.write_config()
        real = pt.create_git

        def boom(c, repo, branch, override=None, existing=False):
            if repo["name"] == "web":
                pt.die("simulated failure on the second repo")
            return real(c, repo, branch, override, existing)

        pt.create_git = boom
        self.addCleanup(lambda: setattr(pt, "create_git", real))
        with self.assertRaises(pt.Fail):
            self.new(cfg, "ours-to-drop")
        self.assertNotIn("ours-to-drop", pt.local_branches(cfg["repos"][0]))


class TestOrcaRollbackKeepsForeignBranches(TestOrcaBackend):
    """The property this tool exists not to violate, under real Orca semantics:
    `orca worktree rm` deletes branches, and rollback uses it. A colleague's
    branch must survive a partial failure anyway."""

    def test_rollback_restores_the_branch_orca_deleted(self):
        for r in (self.api, self.web):
            git(r, "branch", "their-wip", "main")
        git(self.api, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "their unpushed work")
        git(self.api, "branch", "-f", "their-wip", "HEAD")
        sha = git(self.api, "rev-parse", "their-wip").strip()

        self._fake_orca()  # rm deletes branches, like the real thing
        cfg = self._orca_config()
        real = pt.create_orca

        def boom(c, repo, branch, override=None, existing=False, issue=None):
            if repo["name"] == "web":
                pt.die("simulated failure on the second repo")
            return real(c, repo, branch, override, existing, issue)

        pt.create_orca = boom
        self.addCleanup(lambda: setattr(pt, "create_orca", real))
        with self.assertRaises(pt.Fail):
            self.new(cfg, "their-wip", existing=True)

        self.assertIn("their-wip", pt.local_branches(cfg["repos"][0]))  # restored
        self.assertEqual(git(self.api, "rev-parse", "their-wip").strip(), sha)  # same commit
        self.assertNotIn("their-wip", pt.worktrees_of(str(self.api)))  # worktree gone


class TestOrcaExistingFromRemote(TestOrcaBackend):
    """--existing on the orca backend had no coverage at all, and none of it set
    up a remote — which is how the flagship --pr path shipped broken."""

    def _remote_branch(self, name):
        """A branch that exists only on origin, like every PR branch you fetch."""
        origin = self.tmp / "origin.git"
        if not origin.exists():
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
            for r in (self.api, self.web):
                git(r, "remote", "add", "origin", str(origin))
            git(self.api, "push", "-q", "origin", "main")
        git(self.api, "checkout", "-q", "-b", name)
        git(self.api, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "their work")
        git(self.api, "push", "-q", "origin", name)
        git(self.api, "checkout", "-q", "main")
        git(self.api, "branch", "-q", "-D", name)  # remote-only from here
        for r in (self.api, self.web):
            git(r, "fetch", "-q", "origin")

    def _run(self, name):
        self._fake_orca()
        cfg = self._orca_config()
        self.new(cfg, name, existing=True)
        wt = pt.worktrees_of(str(self.api))[name]
        return git(wt, "log", "--oneline", "-1", "--format=%s").strip()

    def test_remote_only_branch_without_a_slash(self):
        """The PR-branch shape: no slash, so Orca's throwaway is named exactly
        like the branch we want. Checking out first is a silent no-op."""
        self._remote_branch("fix-login")
        self.assertEqual(self._run("fix-login"), "their work")  # not 'init' (the base)

    def test_remote_only_branch_with_a_slash(self):
        """Control: the slug differs, which is the only shape that used to work."""
        self._remote_branch("fix/login")
        self.assertEqual(self._run("fix/login"), "their work")


class TestRemoteOnlyBranchIsNotShadowed(Base):
    """`new` on a branch that exists only on origin used to create a divergent
    branch off the base — a shadow whose first push is rejected."""

    def setUp(self):
        super().setUp()
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        for r in (self.api, self.web):
            git(r, "remote", "add", "origin", str(origin))
        git(self.api, "push", "-q", "origin", "main")
        git(self.api, "checkout", "-q", "-b", "shadowme")
        git(self.api, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "their work")
        git(self.api, "push", "-q", "origin", "shadowme")
        git(self.api, "checkout", "-q", "main")
        git(self.api, "branch", "-q", "-D", "shadowme")
        for r in (self.api, self.web):
            git(r, "fetch", "-q", "origin")

    def test_new_refuses_a_branch_that_exists_on_origin(self):
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "shadowme")
        self.assertIn("already exists on origin", str(e.exception))
        self.assertNotIn("shadowme", pt.worktrees_of(str(self.api)))

    def test_link_hint_points_at_existing_not_at_a_shadow(self):
        """The hint has to consult remotes too, or it walks you into the shadow."""
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_link(cfg, argparse.Namespace(branch="shadowme", host=None, agent=None, prompt=None))
        self.assertIn("polytree new shadowme --existing", str(e.exception))


class TestOrcaLeak(Base):
    """A failure after Orca's create is invisible to the caller's rollback, so
    create_orca has to clean up after itself."""

    def test_rename_hitting_a_refs_conflict_leaves_nothing_behind(self):
        """A branch 'zz' exists, so git cannot create 'refs/heads/zz/x' — the
        rename fails. Deterministic, not a race."""
        for r in (self.api, self.web):
            git(r, "branch", "zz", "main")
        orca = TestOrcaBackend._fake_orca.__get__(self, TestOrcaBackend)
        orca()
        cfg_file = self.tmp / "orca.toml"
        cfg_file.write_text(
            f'backend = "orca"\nagent = "fake"\n\n[[repos]]\npath = "{self.api}"\nhost = true\n\n'
            f'[[repos]]\npath = "{self.web}"\n\n[agents.fake]\ncmd = "true"\nattach = "--add-dir {{dir}}"\n'
        )
        pt.CONFIG = cfg_file
        cfg = pt.load_config()
        with self.assertRaises(pt.Fail):
            self.new(cfg, "zz/x")
        for repo in cfg["repos"]:
            self.assertNotIn("zz-x", pt.local_branches(repo))  # no orphan slug branch
            self.assertIn("zz", pt.local_branches(repo))  # their branch untouched
            self.assertNotIn("zz/x", pt.local_branches(repo))
            self.assertEqual([w for w in pt.worktrees_of(repo["path"]) if "zz" in w], [])


class TestBrokenWorldRecovery(Base):
    """State polytree did not create and cannot prevent: someone rm -rf'd a
    worktree, or git is in a shape the tool never made. It has to cope, not crash."""

    def test_link_after_a_worktree_was_deleted_by_hand(self):
        cfg = self.write_config()
        self.new(cfg, "halfgone")
        subprocess.run(["rm", "-rf", pt.worktrees_of(str(self.web))["halfgone"]])
        # git still lists it (prunable) but it is gone: link must say so, not launch
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_link(cfg, argparse.Namespace(branch="halfgone", host=None, agent=None, prompt=None))
        self.assertIn("need >=2", str(e.exception))

    def test_list_ignores_a_worktree_deleted_by_hand(self):
        cfg = self.write_config()
        self.new(cfg, "halfgone")
        subprocess.run(["rm", "-rf", pt.worktrees_of(str(self.web))["halfgone"]])
        out = []
        real = pt.info
        self.addCleanup(lambda: setattr(pt, "info", real))
        pt.info = out.append
        pt.cmd_list(cfg, argparse.Namespace())
        self.assertNotIn("halfgone", "\n".join(out))  # 1 repo left is not a set

    def test_rm_after_a_worktree_was_deleted_by_hand(self):
        """The set is half gone; rm must clean BOTH — including the damaged repo.
        This test used to check only the healthy one, which is precisely how the
        silent skip survived: the damaged repo kept its branch and a stale
        registration, and `new` then refused while pointing back at `rm`."""
        cfg = self.write_config()
        self.new(cfg, "halfgone")
        subprocess.run(["rm", "-rf", pt.worktrees_of(str(self.api))["halfgone"]])
        pt.cmd_rm(cfg, argparse.Namespace(branch="halfgone", force=True))
        self.assertNotIn("halfgone", pt.worktrees_of(str(self.web)))
        self.assertNotIn("halfgone", pt.local_branches(cfg["repos"][0]))  # damaged repo too
        self.assertNotIn("halfgone", pt.local_branches(cfg["repos"][1]))
        self.new(cfg, "halfgone")  # and you are not wedged: it can be recreated
        self.assertIn("halfgone", pt.worktrees_of(str(self.api)))

    def test_new_when_the_destination_is_occupied_by_a_stray_directory(self):
        cfg = self.write_config()
        dest = Path(cfg["root"]) / "squatter" / "api"
        dest.mkdir(parents=True)
        (dest / "junk.txt").write_text("not a worktree")
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "squatter")
        self.assertIn("already exists", str(e.exception))
        self.assertNotIn("squatter", pt.local_branches(cfg["repos"][0]))  # nothing created

    def test_prune_empty_dirs_never_eats_a_directory_with_content(self):
        """Today rmdir refuses non-empty dirs, so the guard is belt-and-braces —
        but this pins the property against the tempting refactor to rmtree."""
        cfg = self.write_config()
        self.new(cfg, "keepdir")
        sibling = Path(cfg["root"]) / "keepdir" / "something-of-mine"
        sibling.mkdir()
        (sibling / "file.txt").write_text("mine")
        pt.cmd_rm(cfg, argparse.Namespace(branch="keepdir", force=True))
        self.assertTrue(sibling.exists(), "cleanup deleted a directory it did not create")


class TestBaseResolution(Base):
    def test_base_can_be_a_sha(self):
        cfg = self.write_config()
        git(self.api, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "second")
        sha = git(self.api, "rev-parse", "HEAD~1").strip()
        for r in (self.api, self.web):
            git(r, "tag", "v1", "main")
        self.new(cfg, "from-tag", base="v1")
        self.assertIn("from-tag", pt.worktrees_of(str(self.api)))

    def test_default_base_without_any_remote_uses_the_local_head(self):
        self.assertEqual(pt.default_base({"path": str(self.api), "name": "api"}), "main")

    def test_a_repo_with_no_commits_fails_in_the_preflight_not_halfway(self):
        """`git symbolic-ref --short HEAD` names the default branch even before
        the first commit, so default_base rightly answers 'main'. The base having
        no commit yet is the preflight's job to catch — and it must catch it
        before anything is created, not after the first repo."""
        empty_a, empty_b = self.tmp / "ea", self.tmp / "eb"
        for p in (empty_a, empty_b):
            p.mkdir()
            git(p, "init", "-q", "-b", "main")
        cfg_file = self.tmp / "empty.toml"
        cfg_file.write_text(
            f'backend = "git"\nagent = "fake"\nroot = "{self.tmp}/wte"\n\n'
            f'[[repos]]\npath = "{empty_a}"\nhost = true\n\n[[repos]]\npath = "{empty_b}"\n\n'
            f'[agents.fake]\ncmd = "true"\nattach = "--add-dir {{dir}}"\n'
        )
        pt.CONFIG = cfg_file
        cfg = pt.load_config()
        self.assertEqual(pt.default_base(cfg["repos"][0]), "main")  # names it, correctly
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "x")
        self.assertIn("does not exist", str(e.exception))
        self.assertIn("nothing was created", str(e.exception))
        self.assertFalse((self.tmp / "wte").exists())


class TestConfigIsHostile(Base):
    def _cfg(self, text):
        p = self.tmp / "hostile.toml"
        p.write_text(text)
        pt.CONFIG = p
        return p

    def test_broken_toml(self):
        self._cfg('backend = "git"\n[[repos]\npath = "/x"\n')
        with self.assertRaises(pt.Fail) as e:
            pt.load_config()
        self.assertIn("invalid TOML", str(e.exception))

    def test_one_repo_is_not_a_set(self):
        self._cfg(f'backend = "git"\n[[repos]]\npath = "{self.api}"\n')
        with self.assertRaises(pt.Fail) as e:
            pt.load_config()
        self.assertIn("at least 2", str(e.exception))

    def test_repo_path_that_is_not_a_git_repo(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self._cfg(f'backend = "git"\n[[repos]]\npath = "{self.api}"\n\n[[repos]]\npath = "{plain}"\n')
        with self.assertRaises(pt.Fail) as e:
            pt.load_config()
        self.assertIn("not a git repo", str(e.exception))

    def test_repo_path_that_does_not_exist(self):
        self._cfg(f'backend = "git"\n[[repos]]\npath = "{self.api}"\n\n[[repos]]\npath = "/nope/zzz"\n')
        with self.assertRaises(pt.Fail) as e:
            pt.load_config()
        self.assertIn("does not exist", str(e.exception))

    def test_invalid_backend(self):
        self._cfg(f'backend = "svn"\n[[repos]]\npath = "{self.api}"\n\n[[repos]]\npath = "{self.web}"\n')
        with self.assertRaises(pt.Fail) as e:
            pt.load_config()
        self.assertIn("invalid backend", str(e.exception))

    def test_two_hosts(self):
        self._cfg(
            f'backend = "git"\n[[repos]]\npath = "{self.api}"\nhost = true\n\n'
            f'[[repos]]\npath = "{self.web}"\nhost = true\n'
        )
        with self.assertRaises(pt.Fail) as e:
            pt.load_config()
        self.assertIn("only one", str(e.exception))

    def test_repo_name_that_escapes_the_root(self):
        self._cfg(
            f'backend = "git"\n[[repos]]\npath = "{self.api}"\nname = "../evil"\n\n'
            f'[[repos]]\npath = "{self.web}"\n'
        )
        with self.assertRaises(pt.Fail) as e:
            pt.load_config()
        self.assertIn("invalid repo name", str(e.exception))

    def test_agent_without_attach(self):
        cfg = self.write_config(extra="\n[agents.broken]\ncmd = \"true\"\n")
        cfg["agent"] = "broken"
        with self.assertRaises(pt.Fail) as e:
            pt.resolve_agent(cfg)
        self.assertIn("needs `attach`", str(e.exception))

    def test_agent_with_an_invalid_env_var_name(self):
        cfg = self.write_config(extra='env = { "not a var" = "1" }\n')
        with self.assertRaises(pt.Fail) as e:
            pt.resolve_agent(cfg)
        self.assertIn("invalid env var name", str(e.exception))


class TestPickHost(Base):
    def test_default_is_config_order(self):
        cfg = self.write_config()
        pairs = [(cfg["repos"][0], "/a"), (cfg["repos"][1], "/b")]
        self.assertEqual(pt.pick_host(pairs, None)[1], "/a")

    def test_explicit_host(self):
        cfg = self.write_config()
        pairs = [(cfg["repos"][0], "/a"), (cfg["repos"][1], "/b")]
        self.assertEqual(pt.pick_host(pairs, "web")[1], "/b")

    def test_unknown_host_fails(self):
        cfg = self.write_config()
        pairs = [(cfg["repos"][0], "/a")]
        with self.assertRaises(pt.Fail):
            pt.pick_host(pairs, "nope")


if __name__ == "__main__":
    unittest.main()
