"""Adversarial tests for polytree. Run: python3 -m unittest discover -s tests -v

These deliberately avoid the comfortable inputs that let past bugs survive:
branch names are the user's real shapes (capitals, underscores, slashes,
unicode), refs are SHAs and annotated tags, worktrees get deleted by hand,
repos arrive through symlinks, and every command's OUTPUT is captured where
the report itself is the contract.

Tests marked KNOWN-FAILING in their docstring assert a property the code at
cd70bcc does not hold. They are findings, not mistakes: do not delete them,
fix the code.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_polytree import Base, git, make_repo, pt  # noqa: E402


def commit(repo, msg):
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", msg)


class HardeningBase(Base):
    def capture(self) -> list:
        """Route pt.info into a list; restored automatically."""
        out = []
        real = pt.info
        self.addCleanup(lambda: setattr(pt, "info", real))
        pt.info = out.append
        return out

    def load_raw(self, text: str) -> dict:
        cfgf = self.tmp / "config.toml"
        cfgf.write_text(text)
        pt.CONFIG = cfgf
        return pt.load_config()

    def two_repo_toml(self, head: str = 'backend = "git"\n') -> str:
        return (
            f'{head}agent = "fake"\nroot = "{self.tmp}/wt"\n\n'
            f'[[repos]]\npath = "{self.api}"\nbase = "main"\nhost = true\n\n'
            f'[[repos]]\npath = "{self.web}"\nbase = "main"\n\n'
            f'[agents.fake]\ncmd = "true"\nattach = "--add-dir {{dir}}"\n'
        )

    def forbid_launch(self):
        real = pt.launch
        self.addCleanup(lambda: setattr(pt, "launch", real))
        pt.launch = lambda *a, **k: self.fail("launch() must not be reached")

    def chdir(self, path):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(path)


class TestRealBranchShapes(HardeningBase):
    """Every past test used lowercase, slash-free branch names. The user's real
    branches look like 'Msf/merge-in-to-dev' — capitals, underscores, slashes."""

    REAL = ("Msf/merge-in-to-dev", "chore/add_new_builtins", "ReginaRRJ-new_uma")

    def test_validator_accepts_the_users_real_branch_names(self):
        """check_branch_name must not be tightened into lowercase-ascii-only:
        these are verbatim branch names from the repos polytree was built for."""
        for name in (*self.REAL, "área/ñandú-y_Más"):
            with self.subTest(name=name):
                pt.check_branch_name(name)  # must not raise

    def test_full_lifecycle_with_real_branch_names(self):
        """new + list + rm must survive capitals, underscores and slashes: the
        branch is also a filesystem path under <root>/, so a slash means nested
        directories that rm must clean back up."""
        cfg = self.write_config()
        for name in self.REAL:
            with self.subTest(name=name):
                self.new(cfg, name)
                self.assertEqual(pt.worktrees_of(str(self.api))[name],
                                 str(self.tmp / "wt" / name / "api"))
                self.assertEqual(pt.worktrees_of(str(self.web))[name],
                                 str(self.tmp / "wt" / name / "web"))
                out = self.capture()
                pt.cmd_list(cfg, argparse.Namespace())
                self.assertIn(name, "\n".join(out))
                pt.cmd_rm(cfg, argparse.Namespace(branch=name, force=True, yes=True))
                self.assertNotIn(name, pt.worktrees_of(str(self.api)))
                self.assertFalse(pt.branch_exists(cfg["repos"][0], name))
        # nested shells (wt/Msf, wt/chore) pruned; the root itself survives
        self.assertEqual(list((self.tmp / "wt").iterdir()), [])

    def test_unicode_branch_round_trip(self):
        """Branch names are not ASCII: accents and ñ must survive create,
        discovery (porcelain parsing) and removal."""
        cfg = self.write_config()
        name = "área/ñandú-y_Más"
        self.new(cfg, name)
        self.assertIn(name, pt.worktrees_of(str(self.api)))
        self.assertIn(name, pt.worktrees_of(str(self.web)))
        pt.cmd_rm(cfg, argparse.Namespace(branch=name, force=True, yes=True))
        self.assertNotIn(name, pt.worktrees_of(str(self.api)))
        self.assertFalse((self.tmp / "wt" / "área").exists())

    def test_more_invalid_shapes_rejected(self):
        """Shapes git itself refuses; passing them through would fail later,
        mid-creation, with a raw git error instead of up front."""
        for bad in ("feat/", "feat.lock", "a//b", "a/.b"):
            with self.subTest(bad=bad), self.assertRaises(pt.Fail):
                pt.check_branch_name(bad)


class TestBaseRefForms(HardeningBase):
    """--base and `base =` are documented as taking a REF, and a ref is not
    always a branch name: hotfixes start from tags and bisected SHAs."""

    def test_configured_base_can_be_a_sha(self):
        """A repo-level `base = "<sha>"` must produce a worktree at exactly
        that commit, not at the branch tip."""
        commit(self.api, "newer work")
        sha = git(self.api, "rev-parse", "HEAD~1").strip()
        cfg = self.write_config(api_base=sha)
        self.new(cfg, "from-sha")
        wt = pt.worktrees_of(str(self.api))["from-sha"]
        self.assertEqual(git(wt, "rev-parse", "HEAD").strip(), sha)

    def test_base_flag_can_be_an_annotated_tag(self):
        """--base v1: annotated tags are tag objects, so the preflight's
        `rev-parse {base}^{commit}` peeling must hold end to end."""
        for r in (self.api, self.web):
            git(r, "-c", "user.email=t@t", "-c", "user.name=t",
                "tag", "-a", "v1", "-m", "release")
            commit(r, "after the tag")
        cfg = self.write_config()
        self.new(cfg, "hotfix/v1-Fix", base="v1")
        for r in (self.api, self.web):
            wt = pt.worktrees_of(str(r))["hotfix/v1-Fix"]
            self.assertEqual(git(wt, "rev-parse", "HEAD").strip(),
                             git(r, "rev-parse", "v1^{commit}").strip())

    def test_base_sha_missing_in_a_sibling_repo_creates_nothing(self):
        """--base applies to EVERY repo. A SHA that only exists in one of them
        must be refused by the preflight, before anything is created — not blow
        up on the second repo and need a rollback.

        (The repos must genuinely diverge here: make_repo's initial commits are
        byte-identical across repos and therefore share a SHA — cosy test data
        of exactly the kind that hides this.)"""
        commit(self.api, "api-only divergence")
        sha = git(self.api, "rev-parse", "HEAD").strip()
        self.assertFalse(pt.git_ok(str(self.web), "rev-parse", "--verify",
                                   f"{sha}^{{commit}}"))  # really absent in web
        cfg = self.write_config()
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "sha-everywhere", base=sha)
        self.assertIn("does not exist in web", str(e.exception))
        self.assertIn("nothing was created", str(e.exception))
        self.assertNotIn("sha-everywhere", pt.worktrees_of(str(self.api)))


class TestDefaultBaseResolution(HardeningBase):
    """default_base is the fallback when nobody configured a base. Its order
    (origin/HEAD, then local HEAD, then die) decides what a feature branches off."""

    def test_origin_head_wins_over_whatever_is_checked_out(self):
        """Standing on a stale local branch must not make it the base: the
        remote's default branch is authoritative when it is known."""
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        git(self.api, "remote", "add", "origin", str(origin))
        git(self.api, "push", "-q", "origin", "main")
        git(self.api, "remote", "set-head", "origin", "-a")
        git(self.api, "checkout", "-q", "-b", "some-stale-Local_branch")
        self.assertEqual(pt.default_base({"path": str(self.api), "name": "api"}),
                         "origin/main")

    def test_detached_head_without_remote_dies_with_a_way_out(self):
        """No origin/HEAD and a detached HEAD: there is no sane base to guess.
        The error must say so and name the fix, not pick something arbitrary."""
        git(self.api, "checkout", "-q", "--detach")
        with self.assertRaises(pt.Fail) as e:
            pt.default_base({"path": str(self.api), "name": "api"})
        self.assertIn("cannot determine a base ref", str(e.exception))
        self.assertIn("remote set-head origin -a", str(e.exception))


class TestCmdPaths(HardeningBase):
    """cmd_paths had zero coverage; scripts consume its output, so the output
    IS the interface."""

    def test_prints_exactly_the_set_host_first(self):
        cfg = self.write_config()
        self.new(cfg, "Paths/One_two")
        out = self.capture()
        pt.cmd_paths(cfg, argparse.Namespace(branch="Paths/One_two"))
        self.assertEqual(out, [str(self.tmp / "wt" / "Paths/One_two" / "api"),
                               str(self.tmp / "wt" / "Paths/One_two" / "web")])

    def test_skips_a_worktree_whose_directory_is_gone(self):
        """A path that no longer exists must not be printed: a script would
        cd into it or hand it to an agent."""
        cfg = self.write_config()
        self.new(cfg, "half-gone")
        subprocess.run(["rm", "-rf", pt.worktrees_of(str(self.web))["half-gone"]])
        out = self.capture()
        pt.cmd_paths(cfg, argparse.Namespace(branch="half-gone"))
        self.assertEqual(out, [str(self.tmp / "wt" / "half-gone" / "api")])

    def test_unknown_branch_prints_nothing(self):
        cfg = self.write_config()
        out = self.capture()
        pt.cmd_paths(cfg, argparse.Namespace(branch="never-existed"))
        self.assertEqual(out, [])

    def test_branch_defaults_to_the_worktree_you_stand_in(self):
        """`polytree paths` with no argument, run from inside a member of the
        set, must resolve to that set."""
        cfg = self.write_config()
        self.new(cfg, "where-am-i")
        self.chdir(pt.worktrees_of(str(self.api))["where-am-i"])
        out = self.capture()
        pt.cmd_paths(cfg, argparse.Namespace(branch=None))
        self.assertEqual(out, [str(self.tmp / "wt" / "where-am-i" / "api"),
                               str(self.tmp / "wt" / "where-am-i" / "web")])

    def test_outside_any_repo_dies_instead_of_guessing(self):
        cfg = self.write_config()
        self.chdir(self.tmp)  # mkdtemp: not a repo
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_paths(cfg, argparse.Namespace(branch=None))
        self.assertIn("not inside a git repo", str(e.exception))

    def test_detached_head_dies_instead_of_printing_heads_paths(self):
        """current_branch on a detached HEAD would otherwise return the literal
        string 'HEAD' and quietly look up a branch named HEAD."""
        cfg = self.write_config()
        git(self.api, "checkout", "-q", "--detach")
        self.chdir(self.api)
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_paths(cfg, argparse.Namespace(branch=None))
        self.assertIn("detached HEAD", str(e.exception))


class TestCmdLs(HardeningBase):
    """cmd_ls had zero coverage. It exists to answer 'what will polytree do?',
    so it must show the RESOLVED config: backend, agent, root, and who hosts."""

    def test_shows_resolved_backend_agent_root_and_host_marker(self):
        cfg = self.write_config()
        out = self.capture()
        pt.cmd_ls(cfg, argparse.Namespace())
        text = "\n".join(out)
        self.assertIn("backend : git", text)
        self.assertIn("agent   : fake", text)
        self.assertIn(f"root    : {self.tmp}/wt", text)
        api_line = next(l for l in out if str(self.api) in l)
        web_line = next(l for l in out if str(self.web) in l)
        self.assertTrue(api_line.lstrip().startswith("* "),
                        f"host marker missing on the host line: {api_line!r}")
        self.assertNotIn("*", web_line)


class TestHostFlagValidation(HardeningBase):
    def test_new_with_unknown_host_creates_nothing(self):
        """KNOWN-FAILING at cd70bcc (finding). --host is only validated AFTER
        every worktree has been created: `polytree new f --host typo` builds the
        whole set, then dies without launching and without rolling back — and
        the error ('is not a repo with a worktree for this branch') implies
        nothing matched, hiding that the set now exists. The same 'validate
        before touching disk' rule that resolve_agent already follows applies:
        --host is checkable against the config up front."""
        cfg = self.write_config()
        self.forbid_launch()
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_new(cfg, argparse.Namespace(
                name="hosted", host="tyop", no_launch=False, base=None,
                existing=False, agent=None, issue=None, prompt=None, pr=None))
        self.assertIn("tyop", str(e.exception))
        self.assertNotIn("hosted", pt.worktrees_of(str(self.api)),
                         "unknown --host left a fully created set behind")
        self.assertNotIn("hosted", pt.worktrees_of(str(self.web)))
        self.assertFalse(pt.branch_exists(cfg["repos"][0], "hosted"))

    def test_link_with_unknown_host_names_the_alternatives_and_launches_nothing(self):
        cfg = self.write_config()
        self.new(cfg, "linkable")
        self.forbid_launch()
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_link(cfg, argparse.Namespace(branch="linkable", host="tyop",
                                                agent=None, prompt=None))
        self.assertIn("tyop", str(e.exception))
        self.assertIn("api", str(e.exception))  # tells you what IS valid
        self.assertIn("web", str(e.exception))


class TestPrPrereqs(HardeningBase):
    def test_pr_without_gh_dies_before_touching_anything(self):
        """--pr on a machine without the GitHub CLI must say exactly that,
        up front — not fall through to a confusing resolution failure, and
        not create anything."""
        cfg = self.write_config()
        real = shutil.which
        self.addCleanup(lambda: setattr(shutil, "which", real))
        shutil.which = lambda cmd, *a, **k: None if cmd == "gh" else real(cmd, *a, **k)
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, None, pr="7")
        self.assertIn("GitHub CLI", str(e.exception))
        self.assertEqual(list(pt.worktrees_of(str(self.api))), ["main"])
        self.assertEqual(list(pt.worktrees_of(str(self.web))), ["main"])


class TestManuallyDeletedWorktrees(HardeningBase):
    """`rm -rf` on a worktree directory is what users actually do. Git then
    calls the worktree 'prunable' but keeps the registration AND the branch."""

    def test_rm_cleans_the_repo_whose_directory_was_deleted_by_hand(self):
        """KNOWN-FAILING at cd70bcc (finding). After `rm -rf <api worktree>`,
        `polytree rm b --force` removes web's half and silently skips api:
        api keeps the branch and the stale registration, the output never
        mentions api, and `polytree new b` is then wedged with 'already exists
        in api — use `polytree rm b` to clean up', which finds nothing to
        remove. rm --force must dispose of the prunable half too."""
        cfg = self.write_config()
        self.new(cfg, "torn")
        subprocess.run(["rm", "-rf", pt.worktrees_of(str(self.api))["torn"]])
        pt.cmd_rm(cfg, argparse.Namespace(branch="torn", force=True, yes=True))
        self.assertFalse(pt.branch_exists(cfg["repos"][0], "torn"),
                         "rm --force left the branch behind in the repo whose "
                         "worktree directory had been deleted by hand")
        self.assertFalse(pt.branch_exists(cfg["repos"][1], "torn"))
        self.new(cfg, "torn")  # the user must not be wedged
        self.assertIn("torn", pt.worktrees_of(str(self.api)))

    def test_link_refuses_a_set_with_a_missing_directory(self):
        """The agent must never be launched into (or attached to) a directory
        that no longer exists."""
        cfg = self.write_config()
        self.new(cfg, "gone-half")
        subprocess.run(["rm", "-rf", pt.worktrees_of(str(self.web))["gone-half"]])
        self.forbid_launch()
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_link(cfg, argparse.Namespace(branch="gone-half", host=None,
                                                agent=None, prompt=None))
        self.assertIn("need >=2", str(e.exception))

    def test_list_does_not_show_a_set_with_a_missing_directory(self):
        cfg = self.write_config()
        self.new(cfg, "half-listed")
        subprocess.run(["rm", "-rf", pt.worktrees_of(str(self.web))["half-listed"]])
        out = self.capture()
        pt.cmd_list(cfg, argparse.Namespace())
        self.assertNotIn("half-listed", "\n".join(out))

    def test_existing_over_a_stale_registration_leaves_no_half_set(self):
        """A repo still holds a stale registration for the branch's old path:
        git refuses the new worktree there ('missing but already registered').
        That failure hits the SECOND repo, after the first was created — the
        rollback must take the first one back and keep both branches."""
        cfg = self.write_config()
        self.new(cfg, "stale")
        subprocess.run(["rm", "-rf", pt.worktrees_of(str(self.web))["stale"]])
        git(self.api, "worktree", "remove", "--force",
            pt.worktrees_of(str(self.api))["stale"])
        # both repos still have the branch; web has the stale registration
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "stale", existing=True)
        self.assertIn("Rolled back the 1", str(e.exception))
        self.assertNotIn("stale", pt.worktrees_of(str(self.api)))
        self.assertTrue(pt.branch_exists(cfg["repos"][0], "stale"))  # not ours to delete
        self.assertTrue(pt.branch_exists(cfg["repos"][1], "stale"))


class TestPruneEmptyDirsSafety(HardeningBase):
    """prune_empty_dirs walks UPWARD deleting directories. The interesting
    property is what it must NOT delete."""

    def test_shell_dir_with_a_stray_file_survives(self):
        """<root>/<branch>/ holding the user's own NOTES.txt is not empty and
        must survive rm, file intact."""
        cfg = self.write_config()
        self.new(cfg, "noted")
        stray = self.tmp / "wt" / "noted" / "NOTES.txt"
        stray.write_text("do not lose me")
        pt.cmd_rm(cfg, argparse.Namespace(branch="noted", force=True, yes=True))
        self.assertNotIn("noted", pt.worktrees_of(str(self.api)))  # worktrees gone
        self.assertEqual(stray.read_text(), "do not lose me")

    def test_the_root_itself_is_never_removed(self):
        cfg = self.write_config()
        self.new(cfg, "only-set")
        pt.cmd_rm(cfg, argparse.Namespace(branch="only-set", force=True, yes=True))
        self.assertTrue((self.tmp / "wt").is_dir())

    def test_never_climbs_out_of_the_root(self):
        """A path outside root (mis-set root, symlinked layout) must not have
        its empty parents deleted: those directories belong to the user."""
        cfg = self.write_config()
        outside = self.tmp / "elsewhere" / "deep" / "empty"
        outside.mkdir(parents=True)
        pt.prune_empty_dirs(cfg, str(outside / "wt"))
        self.assertTrue(outside.is_dir())
        self.assertTrue((self.tmp / "elsewhere").is_dir())


class TestSymlinkedRepoPaths(HardeningBase):
    """Config paths through symlinks (~/code -> /mnt/big/code is common)."""

    def test_symlink_and_its_target_are_the_same_repo(self):
        """Listing a repo twice — once real, once via symlink — must be caught
        as a duplicate, or every operation runs twice on one repo."""
        link = self.tmp / "api-ln"
        link.symlink_to(self.api)
        with self.assertRaises(pt.Fail) as e:
            self.load_raw(
                f'backend = "git"\nroot = "{self.tmp}/wt"\n\n'
                f'[[repos]]\npath = "{self.api}"\n\n[[repos]]\npath = "{link}"\n'
            )
        self.assertIn("duplicate repo path", str(e.exception))

    def test_a_repo_reached_through_a_symlink_works_end_to_end(self):
        link = self.tmp / "api-ln"
        link.symlink_to(self.api)
        cfg = self.load_raw(
            f'backend = "git"\nagent = "fake"\nroot = "{self.tmp}/wt"\n\n'
            f'[[repos]]\npath = "{link}"\nbase = "main"\nhost = true\n\n'
            f'[[repos]]\npath = "{self.web}"\nbase = "main"\n\n'
            f'[agents.fake]\ncmd = "true"\nattach = "--add-dir {{dir}}"\n'
        )
        self.assertEqual(cfg["repos"][0]["path"], str(self.api))  # resolved
        self.assertEqual(cfg["repos"][0]["name"], "api-ln")  # named as written
        self.new(cfg, "via-link")
        self.assertIn("via-link", pt.worktrees_of(str(self.api)))
        self.assertEqual(pt.worktrees_of(str(self.api))["via-link"],
                         str(self.tmp / "wt" / "via-link" / "api-ln"))


class TestConfigRejections(HardeningBase):
    """Broken configs must die with a message, not half-work."""

    def test_broken_toml(self):
        with self.assertRaises(pt.Fail) as e:
            self.load_raw('backend = [unclosed\n')
        self.assertIn("invalid TOML", str(e.exception))

    def test_empty_and_single_repo_lists(self):
        for body in ("repos = []\n",
                     f'[[repos]]\npath = "{self.api}"\n'):
            with self.subTest(body=body), self.assertRaises(pt.Fail) as e:
                self.load_raw(body)
            self.assertIn("at least 2", str(e.exception))

    def test_invalid_backend_value(self):
        with self.assertRaises(pt.Fail) as e:
            self.load_raw(self.two_repo_toml('backend = "docker"\n'))
        self.assertIn("invalid backend", str(e.exception))

    def test_backend_orca_without_the_cli(self):
        real = pt.orca_bin
        self.addCleanup(lambda: setattr(pt, "orca_bin", real))
        pt.orca_bin = lambda: None
        with self.assertRaises(pt.Fail) as e:
            self.load_raw(self.two_repo_toml('backend = "orca"\n'))
        self.assertIn("not installed", str(e.exception))

    def test_backend_auto_resolves_by_orca_presence(self):
        """auto must mean 'orca if installed, else git' — on THIS machine either
        may be true, so pin both directions explicitly."""
        real = pt.orca_bin
        self.addCleanup(lambda: setattr(pt, "orca_bin", real))
        pt.orca_bin = lambda: "/usr/bin/fake-orca"
        self.assertEqual(self.load_raw(self.two_repo_toml('backend = "auto"\n'))["backend"], "orca")
        pt.orca_bin = lambda: None
        self.assertEqual(self.load_raw(self.two_repo_toml('backend = "auto"\n'))["backend"], "git")

    def test_agent_without_attach(self):
        cfg = self.write_config(extra='\n[agents.noattach]\ncmd = "true"\n')
        cfg["agent"] = "noattach"
        with self.assertRaises(pt.Fail) as e:
            pt.resolve_agent(cfg)
        self.assertIn("attach", str(e.exception))

    def test_agent_with_empty_cmd(self):
        cfg = self.write_config(extra='\n[agents.hollow]\ncmd = ""\nattach = "-d {dir}"\n')
        cfg["agent"] = "hollow"
        with self.assertRaises(pt.Fail) as e:
            pt.resolve_agent(cfg)
        self.assertIn("empty", str(e.exception))

    def test_invalid_env_var_names_rejected(self):
        """'BAD-NAME=x cmd' would be parsed by a shell as a command, not an
        assignment; the orca launch path builds exactly that string."""
        for key in ("BAD-NAME", "1LEADING", "WITH SPACE"):
            with self.subTest(key=key):
                cfg = self.write_config(extra=f'env = {{ "{key}" = "1" }}\n')
                with self.assertRaises(pt.Fail) as e:
                    pt.resolve_agent(cfg)
                self.assertIn("invalid env var name", str(e.exception))

    def test_repo_name_must_be_a_single_path_component(self):
        for name in ("a/b", "..", "."):
            with self.subTest(name=name), self.assertRaises(pt.Fail):
                self.load_raw(
                    f'backend = "git"\n[[repos]]\npath = "{self.api}"\nname = "{name}"\n\n'
                    f'[[repos]]\npath = "{self.web}"\n'
                )

    def test_missing_path_and_non_git_dir(self):
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        cases = [(f'[[repos]]\npath = "{self.tmp}/nowhere"\n\n[[repos]]\npath = "{self.web}"\n',
                  "does not exist"),
                 (f'[[repos]]\npath = "{plain}"\n\n[[repos]]\npath = "{self.web}"\n',
                  "not a git repo")]
        for body, msg in cases:
            with self.subTest(msg=msg), self.assertRaises(pt.Fail) as e:
                self.load_raw('backend = "git"\n' + body)
            self.assertIn(msg, str(e.exception))

    def test_non_string_env_values_are_stringified_for_the_process(self):
        """TOML happily yields ints/bools; execvpe and shell strings need str."""
        cfg = self.write_config(extra='env = { LEVEL = 3 }\n')
        _, env = pt.build_argv(cfg, cfg["agents"]["fake"], ["/a"])
        self.assertEqual(env, {"LEVEL": "3"})


class TestQuotingAndPrompts(HardeningBase):
    def test_prompt_is_one_argument_and_never_a_template(self):
        """A prompt is user prose: quotes must not split it and a literal
        '{dir}' inside it must NOT be expanded like the attach template."""
        cfg = self.write_config()
        prompt = 'fix the "login" bug in {dir}; don\'t touch tests'
        argv, _ = pt.build_argv(cfg, cfg["agents"]["fake"], ["/a"], prompt)
        self.assertEqual(argv[-1], prompt)
        self.assertEqual(argv.count(prompt), 1)

    def test_attach_dir_with_spaces_stays_one_token(self):
        """expand() splits the TEMPLATE first, then substitutes: a directory
        containing spaces must come out as a single argv element."""
        self.assertEqual(pt.expand("--add-dir {dir}", "/a b/c d"),
                         ["--add-dir", "/a b/c d"])

    def test_orca_terminal_command_survives_hostile_paths_and_prompts(self):
        """The orca backend flattens argv+env into ONE shell string. Paths with
        spaces and quotes, env values with spaces, prompts with newlines: the
        string must shlex-split back into exactly what was meant."""
        cfg = self.write_config()
        cfg["backend"] = "orca"
        real_run, real_bin = pt.run, pt.orca_bin
        self.addCleanup(lambda: (setattr(pt, "run", real_run),
                                 setattr(pt, "orca_bin", real_bin)))
        pt.orca_bin = lambda: "fake-orca"
        calls = []
        pt.run = lambda args, cwd=None, check=True: (calls.append(args), "{}")[1]

        spec = {"cmd": "true", "attach": "--add-dir {dir}", "env": {"FOO": "a b"}}
        host = str(self.tmp / "host with space")
        other = str(self.tmp / "o'ther \"dir\"")
        pt.launch(cfg, spec, host, [other], 'line one\nline "two"')

        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual(args[:3], ["fake-orca", "terminal", "create"])
        self.assertEqual(args[args.index("--worktree") + 1], f"path:{host}")
        cmd = args[args.index("--command") + 1]
        self.assertEqual(shlex.split(cmd),
                         ["FOO=a b", "true", "--add-dir", other, 'line one\nline "two"'])


class TestMiscHardening(HardeningBase):
    def test_remove_worktree_keeping_branch_tolerates_a_missing_branch(self):
        """Called with a branch that never existed (Orca made none, or it is
        already gone): the worktree must still be removed, without crashing and
        without conjuring a branch out of an empty SHA."""
        cfg = self.write_config()
        dest = str(self.tmp / "loose")
        git(self.api, "worktree", "add", "-q", dest, "-b", "carrier")
        pt.remove_worktree_keeping_branch(cfg, cfg["repos"][0], dest, "never-was")
        self.assertNotIn("carrier", pt.worktrees_of(str(self.api)))  # worktree gone
        self.assertFalse(pt.branch_exists(cfg["repos"][0], "never-was"))
        self.assertTrue(pt.branch_exists(cfg["repos"][0], "carrier"))  # kept, as named

    def test_no_launch_does_not_require_the_agent_binary(self):
        """--no-launch exists to create sets where the agent will NOT run (CI,
        a headless box). Requiring the agent binary anyway would break that;
        the agent is validated only when it is about to be used."""
        cfg = self.write_config()
        cfg["agents"]["fake"]["cmd"] = "definitely-not-a-real-binary-xyz"
        self.new(cfg, "headless")  # Base.new passes no_launch=True
        self.assertIn("headless", pt.worktrees_of(str(self.api)))
        self.assertIn("headless", pt.worktrees_of(str(self.web)))

    def test_agent_flag_typo_with_launch_creates_nothing(self):
        """The --agent OVERRIDE must be applied before the agent is validated:
        a typo'd flag has to fail up front, with zero worktrees made."""
        cfg = self.write_config()
        self.forbid_launch()
        with self.assertRaises(pt.Fail) as e:
            pt.cmd_new(cfg, argparse.Namespace(
                name="flagged", host=None, no_launch=False, base=None,
                existing=False, agent="tyop-agent", issue=None, prompt=None, pr=None))
        self.assertIn("tyop-agent", str(e.exception))
        self.assertNotIn("flagged", pt.worktrees_of(str(self.api)))
        self.assertNotIn("flagged", pt.worktrees_of(str(self.web)))

    def test_new_refuses_when_the_destination_directory_already_exists(self):
        """A leftover unregistered directory at <root>/<branch>/<repo> (from a
        crash, a manual copy) must stop the preflight — git would otherwise
        refuse mid-creation or, worse, adopt the directory."""
        cfg = self.write_config()
        (self.tmp / "wt" / "occupied" / "api").mkdir(parents=True)
        with self.assertRaises(pt.Fail) as e:
            self.new(cfg, "occupied")
        self.assertIn("already exists", str(e.exception))
        self.assertIn("nothing was created", str(e.exception))
        self.assertNotIn("occupied", pt.worktrees_of(str(self.web)))


if __name__ == "__main__":
    import unittest
    unittest.main()
