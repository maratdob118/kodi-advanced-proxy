# -*- coding: utf-8 -*-
"""Tests for publishing the generated tree into maratdob118/kodi-addons.

The publisher takes a tree produced by scripts/generate_repo.py (zips/,
addons.xml, addons.xml.md5, README.md — including binary ZIPs) and mirrors it
into the target repository, served from raw.githubusercontent.com. It is run
by CI on every release, so the interesting properties are all about repeating
it safely:

  * publishing twice for one version must not produce two commits or move a tag
  * a tag that already exists with DIFFERENT content is a conflict, never a
    force-push
  * a concurrent push must be absorbed by fetch/rebase/retry, not by --force
  * the fine-grained token must never reach argv, stdout or stderr
  * only the generated files are touched: the target's .git and any unrelated
    file it carries are preserved

Every test drives an injectable fake runner over temp directories. No real gh,
no real git, no network.

Run:  python3 tests/test_repository_publish.py
      python3 -m unittest tests.test_repository_publish
"""
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import publish_repo  # noqa: E402

PAYLOAD = "service.advancedproxy"
REPOSITORY = "repository.maratdob118"
TARGET = "maratdob118/kodi-addons"
TOKEN_ENV = "KODI_ADDONS_TOKEN"
TOKEN = "github_pat_11ABCDEF0123456789_supersecretvalue"
BOT_NAME = "kodi-addons-release-bot"
BOT_EMAIL = "kodi-addons-release-bot@users.noreply.github.com"
VERSION = "1.2.3"
TAG = "v1.2.3"
BRANCH = "main"

ADDONS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addons>
<addon id="repository.maratdob118" name="RandomTask Repo" version="1.0.1" provider-name="RandomTask" />
<addon id="service.advancedproxy" name="Advanced Proxy" version="%s" provider-name="advancedproxy" />
</addons>
"""

PAYLOAD_ZIP = b"PK\x03\x04fake-payload-zip-bytes"
REPOSITORY_ZIP = b"PK\x03\x04fake-repository-zip-bytes"


def generated_files(version=VERSION):
    """A coherent stand-in for one generate_repo.py run, keyed by relative path."""
    addons_xml = (ADDONS_XML % version).encode("utf-8")
    md5 = hashlib.md5(addons_xml).hexdigest()
    return {
        "zips/addons.xml": addons_xml,
        "zips/addons.xml.md5": (md5 + "\n").encode("utf-8"),
        "README.md": b"# RandomTask Repo\n\nGenerated tree.\n",
        "zips/%s/addon.xml" % PAYLOAD: addons_xml,
        "zips/%s/%s-%s.zip" % (PAYLOAD, PAYLOAD, version): PAYLOAD_ZIP,
        "zips/%s/addon.xml" % REPOSITORY: ("<addon id='%s' />" % REPOSITORY
                                           ).encode("utf-8"),
        "zips/%s/%s-1.0.1.zip" % (REPOSITORY, REPOSITORY): REPOSITORY_ZIP,
    }


def write_tree(root, files):
    for relative in sorted(files):
        path = os.path.join(root, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(files[relative])
    return root


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


class FakeGit:
    """A fake gh/git runner backed by an in-memory model of the target repo.

    Models only what the publisher relies on: the files at HEAD, the tags and
    the content each tag carries, and whether a push is rejected. Clone
    materialises HEAD onto disk so the publisher's real file syncing and the
    preservation guarantees can be observed.
    """

    def __init__(self, head_files=None, tags=None, untracked=None,
                 reject_pushes=0, race_tag=None, fail=None):
        self.head_files = dict(head_files or {})
        self.remote_tags = dict(tags or {})   # server side: tag -> {path: bytes}
        self.refs = {}                        # local refs: ref -> {path: bytes}
        self.untracked = dict(untracked or {})
        self.reject_pushes = reject_pushes    # non-fast-forward rejections
        self.race_tag = race_tag              # content another run lands mid-push
        self.fail = fail or ()                # command prefixes that fail hard
        self.calls = []
        self.envs = []
        self.dest = None
        self.pushed = []
        self.published = {}
        self.kept_git_dir = False
        self.staged = []
        self.raced = False

    @property
    def local_tags(self):
        return {ref[len("refs/tags/"):]: content
                for ref, content in self.refs.items()
                if ref.startswith("refs/tags/")}

    # -- helpers ----------------------------------------------------------
    def ok(self, stdout="", stderr=""):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)

    def err(self, stderr="", returncode=1, stdout=""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def disk(self, relative):
        path = os.path.join(self.dest, relative.replace("/", os.sep))
        return read_bytes(path) if os.path.isfile(path) else None

    def worktree(self):
        """Every non-.git file currently on disk in the clone."""
        found = {}
        for directory, names, filenames in os.walk(self.dest):
            names[:] = [n for n in names if n != ".git"]
            for name in filenames:
                path = os.path.join(directory, name)
                found[os.path.relpath(path, self.dest).replace(os.sep, "/")] = \
                    read_bytes(path)
        return found

    # -- dispatch ---------------------------------------------------------
    def __call__(self, cmd, env=None):
        self.calls.append(list(cmd))
        self.envs.append(dict(env or {}))
        for prefix in self.fail:
            if cmd[:len(prefix)] == list(prefix):
                return self.err("simulated failure")
        if cmd[:3] == ["gh", "repo", "clone"]:
            return self._clone(cmd)
        verb, rest = self._verb(cmd)
        handler = getattr(self, "_git_" + verb, None)
        return handler(rest) if handler else self.ok()

    def _verb(self, cmd):
        """Split `git -C <dest> <verb> ...` into the verb and its arguments."""
        rest = cmd[3:] if cmd[:2] == ["git", "-C"] else cmd[1:]
        return rest[0].replace("-", "_"), rest[1:]

    def _clone(self, cmd):
        self.dest = cmd[4]
        os.makedirs(os.path.join(self.dest, ".git"), exist_ok=True)
        with open(os.path.join(self.dest, ".git", "config"), "w",
                  encoding="utf-8") as stream:
            stream.write("[core]\n")
        write_tree(self.dest, self.head_files)
        write_tree(self.dest, self.untracked)
        for tag, content in self.remote_tags.items():
            self.refs["refs/tags/" + tag] = dict(content)
        return self.ok()

    def _git_config(self, args):
        return self.ok()

    def _git_rev_parse(self, args):
        ref = args[-1]
        if ref == "refs/remotes/origin/" + BRANCH:
            return self.ok(ref + "\n") if self.head_files else self.err()
        if ref.startswith("refs/tags/"):
            return self.ok("tagsha\n") if ref in self.refs else self.err()
        return self.ok("headsha\n")

    def _git_checkout(self, args):
        return self.ok()

    def _git_add(self, args):
        self.staged = [a for a in args if a != "--"]
        return self.ok()

    def _git_diff(self, args):
        refs = [a for a in args if a.startswith("refs/")]
        paths = [a for a in args
                 if not a.startswith("-") and a != "--" and not a.startswith("refs/")]
        for relative in paths:
            baseline = self.head_files.get(relative)
            if refs:
                baseline = self.refs.get(refs[0], {}).get(relative)
            if self.disk(relative) != baseline:
                return self.err()  # 1: content differs from the baseline
        return self.ok()

    def _git_commit(self, args):
        self.head_files = dict(self.head_files, **self.worktree())
        return self.ok()

    def _git_tag(self, args):
        if args[:1] == ["-d"]:
            self.refs.pop("refs/tags/" + args[1], None)
            return self.ok()
        self.refs["refs/tags/" + args[0]] = dict(self.worktree())
        return self.ok()

    def _git_fetch(self, args):
        """Only the tag-comparison refspec has an observable effect here."""
        for argument in args:
            source, _, destination = argument.partition(":")
            if not destination:
                continue
            tag = source.lstrip("+")[len("refs/tags/"):]
            if tag not in self.remote_tags:
                return self.err("fatal: couldn't find remote ref %s" % source)
            self.refs[destination] = dict(self.remote_tags[tag])
        return self.ok()

    def _git_rebase(self, args):
        return self.ok()

    def _git_push(self, args):
        refs = [a for a in args if not a.startswith("-")]
        tags = [r[len("refs/tags/"):] for r in refs if r.startswith("refs/tags/")]
        if tags and self.race_tag is not None and not self.raced:
            self.raced = True  # another run publishes this tag first
            self.remote_tags[tags[0]] = dict(self.race_tag)
        for tag in tags:
            if tag in self.remote_tags:
                return self.err(" ! [rejected]        %s -> %s (already exists)\n"
                                "error: failed to push some refs" % (tag, tag))
        if self.reject_pushes > 0:
            self.reject_pushes -= 1
            return self.err(" ! [rejected]        %s -> %s (fetch first)\n"
                            "error: failed to push some refs" % (BRANCH, BRANCH))
        for tag in tags:
            self.remote_tags[tag] = dict(self.refs["refs/tags/" + tag])
        self.published = dict(self.worktree())
        self.kept_git_dir = os.path.isfile(
            os.path.join(self.dest, ".git", "config"))
        self.pushed.append(refs)
        return self.ok()

    # -- assertions helpers ----------------------------------------------
    def ran(self, verb):
        return [c for c in self.calls
                if c[:2] == ["git", "-C"] and c[3] == verb]


class PublishTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="repository-publish-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.generated = write_tree(os.path.join(self.tmp, "generated"),
                                    generated_files())
        self.sleeps = []

    def publisher(self, runner=None, **kwargs):
        kwargs.setdefault("generated_dir", self.generated)
        kwargs.setdefault("version", VERSION)
        kwargs.setdefault("environ", {TOKEN_ENV: TOKEN, "PATH": "/usr/bin"})
        return publish_repo.RepositoryPublisher(
            runner=runner or FakeGit(), sleep=self.sleeps.append, **kwargs)

    def run_publisher(self, publisher):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = publisher.run()
        return code, buffer.getvalue()

    def publish(self, runner=None, **kwargs):
        runner = runner or FakeGit()
        code, output = self.run_publisher(self.publisher(runner, **kwargs))
        return code, output, runner


class TestValidation(PublishTestCase):
    """Nothing is cloned, let alone pushed, until the tree is proven sane."""

    def assertRefusedBeforeGit(self, code, output, runner):
        self.assertEqual(code, 1, output)
        self.assertIn("publish_repo:", output)
        self.assertEqual(runner.calls, [], "refusal still ran commands")

    def test_publishes_only_a_complete_generated_tree(self):
        os.remove(os.path.join(self.generated, "zips", "addons.xml"))
        self.assertRefusedBeforeGit(*self.publish())

    def test_missing_generated_directory_is_refused(self):
        runner = FakeGit()
        code, output, _ = self.publish(
            runner, generated_dir=os.path.join(self.tmp, "nope"))
        self.assertEqual(code, 1, output)
        self.assertEqual(runner.calls, [], "refusal still ran commands")

    def test_binary_payload_zip_is_accepted(self):
        code, output, _ = self.publish()
        self.assertEqual(code, 0, output)

    def test_oversized_file_is_refused(self):
        path = os.path.join(self.generated, "zips", PAYLOAD,
                            "%s-%s.zip" % (PAYLOAD, VERSION))
        with open(path, "wb") as stream:
            stream.seek(publish_repo.MAX_FILE + 1)
            stream.write(b"\0")
        code, output, runner = self.publish()
        self.assertEqual(code, 1, output)
        self.assertEqual(runner.calls, [], "refusal still ran commands")

    def test_symlink_in_the_generated_tree_is_refused(self):
        os.remove(os.path.join(self.generated, "README.md"))
        os.symlink("/etc/passwd", os.path.join(self.generated, "README.md"))
        code, output, runner = self.publish()
        self.assertEqual(code, 1, output)
        self.assertEqual(runner.calls, [], "refusal still ran commands")

    def test_malformed_version_is_a_usage_error(self):
        code, output, _ = self.publish(version="banana")
        self.assertEqual(code, 2, output)

    def test_missing_token_is_refused_before_any_command(self):
        code, output, runner = self.publish(
            environ={"PATH": "/usr/bin"})
        self.assertEqual(code, 2, output)
        self.assertEqual(runner.calls, [], "refusal still ran commands")


class TestPublishing(PublishTestCase):
    def test_succeeds(self):
        code, output, _ = self.publish()
        self.assertEqual(code, 0, output)

    def test_commits_with_the_agreed_message(self):
        _, _, runner = self.publish()
        commits = runner.ran("commit")
        self.assertEqual(len(commits), 1)
        self.assertIn("Publish %s %s" % (PAYLOAD, VERSION), commits[0])

    def test_tags_the_published_version(self):
        _, _, runner = self.publish()
        self.assertEqual([c[4:] for c in runner.ran("tag")], [[TAG]])

    def test_pushes_branch_and_tag_together(self):
        _, _, runner = self.publish()
        self.assertEqual(len(runner.pushed), 1)
        self.assertIn("origin", runner.pushed[0])
        self.assertIn(BRANCH, runner.pushed[0])
        self.assertIn("refs/tags/" + TAG, runner.pushed[0])

    def test_never_force_pushes(self):
        _, _, runner = self.publish()
        for call in runner.calls:
            self.assertNotIn("--force", call)
            self.assertNotIn("-f", call)

    def test_target_receives_exactly_the_generated_files(self):
        _, _, runner = self.publish()
        self.assertEqual(runner.published, generated_files())

    def test_preserves_unmanaged_files_and_the_git_directory(self):
        runner = FakeGit(head_files={"CNAME": b"example.com\n",
                                     ".nojekyll": b""},
                         untracked={"unrelated/note.txt": b"note"})
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertEqual(runner.published.get("CNAME"), b"example.com\n")
        self.assertEqual(runner.published.get(".nojekyll"), b"")
        self.assertEqual(runner.published.get("unrelated/note.txt"), b"note")
        self.assertTrue(runner.kept_git_dir)

    def test_stages_all_the_managed_paths(self):
        _, _, runner = self.publish()
        self.assertEqual(sorted(runner.staged), sorted(generated_files()))

    def test_configures_an_explicit_bot_identity_locally(self):
        _, _, runner = self.publish()
        configs = runner.ran("config")
        flat = [argument for call in configs for argument in call]
        self.assertIn(BOT_NAME, flat)
        self.assertIn(BOT_EMAIL, flat)

    def test_reports_what_it_published(self):
        _, output, _ = self.publish()
        self.assertIn("PUBLISHED", output)
        self.assertIn(TAG, output)
        self.assertIn(TARGET, output)


class TestIdempotency(PublishTestCase):
    def test_no_diff_and_matching_tag_skips_without_writing(self):
        runner = FakeGit(head_files=generated_files(),
                         tags={TAG: generated_files()})
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP", output)
        self.assertEqual(runner.pushed, [], "skip still pushed")

    def test_no_diff_and_missing_tag_pushes_the_tag_only(self):
        runner = FakeGit(head_files=generated_files())
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertIn("TAGGED", output)
        self.assertEqual(len(runner.pushed), 1)
        self.assertNotIn(BRANCH, runner.pushed[0])

    def test_second_run_after_a_publish_is_a_skip(self):
        _, _, first = self.publish()
        self.assertEqual(len(first.pushed), 1)
        runner = FakeGit(head_files=first.published,
                         tags={TAG: first.published})
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP", output)


class TestTagConflicts(PublishTestCase):
    def test_existing_tag_with_different_content_aborts(self):
        runner = FakeGit(tags={TAG: {"README.md": b"different"}})
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 1, output)
        self.assertIn("immutable", output)
        self.assertEqual(runner.pushed, [], "conflict still pushed")

    def test_never_moves_or_deletes_the_published_tag(self):
        runner = FakeGit(tags={TAG: {"README.md": b"different"}})
        self.publish(runner)
        self.assertEqual(runner.remote_tags, {TAG: {"README.md": b"different"}})

    def test_tag_missing_a_managed_file_is_a_conflict(self):
        partial = generated_files()
        partial.pop("README.md")
        runner = FakeGit(tags={TAG: partial})
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 1, output)
        self.assertIn("immutable", output)


class TestPushRecovery(PublishTestCase):
    def test_non_fast_forward_is_absorbed_by_fetch_rebase_retry(self):
        runner = FakeGit(reject_pushes=1)
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(runner.pushed), 1)

    def test_retries_at_most_three_pushes(self):
        runner = FakeGit(reject_pushes=99)
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 1, output)
        self.assertIn("3", output)
        self.assertEqual(len(runner.pushed), 0)

    def test_backs_off_between_attempts(self):
        runner = FakeGit(reject_pushes=2)
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(all(delay > 0 for delay in self.sleeps), self.sleeps)
        self.assertGreater(self.sleeps[1], self.sleeps[0], "no backoff growth")

    def test_retag_after_rebase_so_the_tag_follows_the_replayed_commit(self):
        runner = FakeGit(reject_pushes=2)
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(runner.pushed), 1)

    def test_a_tag_published_concurrently_with_other_content_aborts(self):
        runner = FakeGit(race_tag={"README.md": b"someone elses"})
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 1, output)
        self.assertIn("immutable", output)

    def test_a_tag_published_concurrently_with_identical_content_skips(self):
        runner = FakeGit(race_tag=generated_files())
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP", output)


class TestFailures(PublishTestCase):
    def test_clone_failure_stops_before_any_write(self):
        runner = FakeGit(fail=[("gh", "repo", "clone")])
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 1, output)
        self.assertIsNone(runner.dest)

    def test_commit_failure_never_pushes(self):
        runner = FakeGit()
        runner._git_commit = lambda args: runner.err("commit failed")
        code, output, _ = self.publish(runner)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(runner.pushed, [])

    def test_terminal_push_failure_leaves_no_orphan_local_tag(self):
        runner = FakeGit(reject_pushes=99)
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 1, output)
        self.assertNotIn(TAG, runner.local_tags)

    def test_failure_is_diagnosed_not_crashed(self):
        runner = FakeGit(fail=[("gh", "repo", "clone")])
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 1, output)
        self.assertIn("publish_repo:", output)
        self.assertNotIn("Traceback", output)


class TestTokenHygiene(PublishTestCase):
    def test_token_never_appears_in_any_argument(self):
        _, _, runner = self.publish()
        for call in runner.calls:
            for argument in call:
                self.assertNotIn(TOKEN, argument)

    def test_token_never_appears_in_the_printed_transcript(self):
        _, output, _ = self.publish()
        self.assertNotIn(TOKEN, output)

    def test_command_output_mentioning_the_token_is_redacted(self):
        runner = FakeGit()
        runner.ok = lambda stdout="", stderr="": SimpleNamespace(
            returncode=0, stdout=TOKEN, stderr="")
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertNotIn(TOKEN, output)

    def test_token_travels_to_git_only_through_the_environment(self):
        _, _, runner = self.publish()
        for env in runner.envs:
            self.assertEqual(env.get("GH_TOKEN"), TOKEN)

    def test_a_foreign_github_token_cannot_shadow_the_scoped_token(self):
        _, _, runner = self.publish(
            environ={TOKEN_ENV: TOKEN, "GITHUB_TOKEN": "wrong", "PATH": "/usr/bin"})
        for env in runner.envs:
            self.assertNotEqual(env.get("GITHUB_TOKEN"), "wrong")
            self.assertEqual(env.get("GH_TOKEN"), TOKEN)

    def test_credentials_are_never_prompted_for(self):
        _, _, runner = self.publish()
        for env in runner.envs:
            self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")


class TestDryRun(PublishTestCase):
    def test_runs_no_commands_and_needs_no_token(self):
        runner = FakeGit()
        code, output, _ = self.publish(runner, dry_run=True,
                                       environ={"PATH": "/usr/bin"})
        self.assertEqual(code, 0, output)
        self.assertEqual(runner.calls, [], "dry run ran commands")
        self.assertIn("dry-run", output)

    def test_prints_the_plan(self):
        code, output, _ = self.publish(dry_run=True, environ={"PATH": "/usr/bin"})
        self.assertEqual(code, 0, output)
        self.assertIn("would clone", output)
        self.assertIn(TAG, output)

    def test_still_validates_the_generated_tree(self):
        os.remove(os.path.join(self.generated, "zips", "addons.xml"))
        code, output, _ = self.publish(dry_run=True,
                                       environ={"PATH": "/usr/bin"})
        self.assertEqual(code, 1, output)


class TestDefaults(PublishTestCase):
    def test_defaults_target_the_agreed_repository_and_token(self):
        publisher = self.publisher()
        self.assertEqual(publisher.repository, TARGET)
        self.assertEqual(publisher.token_env, TOKEN_ENV)

    def test_repository_and_branch_are_configurable(self):
        publisher = self.publisher(repository="someone/else", branch="dev")
        self.assertEqual(publisher.repository, "someone/else")
        self.assertEqual(publisher.branch, "dev")

    def test_token_env_is_configurable(self):
        publisher = self.publisher(token_env="OTHER_TOKEN")
        self.assertEqual(publisher.token_env, "OTHER_TOKEN")

    def test_cli_dry_run(self):
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "publish_repo.py"),
             "--generated-dir", self.generated, "--version", VERSION,
             "--dry-run"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dry-run", result.stdout)

    def test_cli_rejects_a_bad_version_as_usage_error(self):
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "publish_repo.py"),
             "--generated-dir", self.generated, "--version", "banana"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
