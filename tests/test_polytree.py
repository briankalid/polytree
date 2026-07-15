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
        args = argparse.Namespace(name=name, host=None, no_launch=True, **kw)
        pt.cmd_new(cfg, args)


class TestWorktreeParsing(Base):
    def test_skips_detached_and_bare(self):
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

    def test_rollback_when_a_later_repo_fails(self):
        """web has a bad base: api is created first, then must be rolled back."""
        cfg = self.write_config(web_base="origin/does-not-exist")
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

    def test_rm_unknown_branch_fails(self):
        cfg = self.write_config()
        with self.assertRaises(pt.Fail):
            pt.cmd_rm(cfg, argparse.Namespace(branch="never", force=False))


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
