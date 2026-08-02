# -*- coding: utf-8 -*-
"""Tests for publishing the generated tree into maratdob118/kodi-addons.

The publisher takes a tree produced by scripts/generate_repo.py and mirrors it
into the target repository, whose Pages workflow reacts to the push. It is run
by CI on every release, so the interesting properties are all about repeating
it safely:

  * publishing twice for one version must not produce two commits or move a tag
  * a tag that already exists with DIFFERENT content is a conflict, never a
    force-push
  * a concurrent push must be absorbed by fetch/rebase/retry, not by --force
  * the fine-grained token must never reach argv, stdout or stderr
  * only the managed generated files are touched: the target's .git and any
    unrelated file it carries are preserved

Every test drives an injectable fake runner over temp directories. No real gh,
no real git, no network.

Run:  python3 tests/test_repository_publish.py
      python3 -m unittest tests.test_repository_publish
"""
import hashlib
import io
import json
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
REPOSITORY = "repository.bigping"
TARGET = "maratdob118/kodi-addons"
TOKEN_ENV = "KODI_ADDONS_TOKEN"
TOKEN = "github_pat_11ABCDEF0123456789_supersecretvalue"
BOT_NAME = "kodi-addons-release-bot"
BOT_EMAIL = "kodi-addons-release-bot@users.noreply.github.com"
VERSION = "1.2.3"
TAG = "v1.2.3"
BRANCH = "main"
MANAGED = ("addons.xml", "addons.xml.md5", "manifest.json", "README.md",
           REPOSITORY + "/addon.xml")

ADDONS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addons>
<addon id="repository.bigping" name="BigPing" version="1.0.0" provider-name="bigping" />
<addon id="service.advancedproxy" name="Advanced Proxy" version="%s" provider-name="advancedproxy" />
</addons>
"""

REPOSITORY_ADDON_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="repository.bigping" name="BigPing" version="1.0.0" provider-name="bigping" />
"""


def generated_files(version=VERSION):
    """A coherent stand-in for one generate_repo.py run, keyed by relative path."""
    addons_xml = (ADDONS_XML % version).encode("utf-8")
    md5 = hashlib.md5(addons_xml).hexdigest()
    manifest = {
        "schema": 1,
        "generator": "scripts/generate_repo.py",
        "datadir": "https://maratdob118.github.io/kodi-addons/",
        "index": {"addons_xml": "addons.xml",
                  "addons_xml_md5": "addons.xml.md5", "md5": md5},
        "addons": [
            {"id": REPOSITORY, "version": "1.0.0", "origin": "build"},
            {"id": PAYLOAD, "version": version, "origin": "release-asset",
             "release": {"repo": "maratdob118/kodi-advanced-proxy",
                         "tag": "v" + version}},
        ],
    }
    return {
        "addons.xml": addons_xml,
        "addons.xml.md5": (md5 + "\n").encode("utf-8"),
        "manifest.json": (json.dumps(manifest, indent=2, sort_keys=True,
                                     ensure_ascii=False) + "\n").encode("utf-8"),
        "README.md": b"# BigPing Kodi repository\n\nGenerated tree.\n",
        REPOSITORY + "/addon.xml": REPOSITORY_ADDON_XML.encode("utf-8"),
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
        paths = [a for a in args if not a.startswith("-") and a != "--"]
        for relative in paths:
            if self.disk(relative) != self.head_files.get(relative):
                return self.err()  # 1: staged content differs from HEAD
        return self.ok()

    def _git_show(self, args):
        ref, _, path = args[0].partition(":")
        content = self.refs.get(ref, {}).get(path)
        if content is None:
            return self.err("fatal: path '%s' does not exist in '%s'"
                            % (path, ref), returncode=128)
        return SimpleNamespace(returncode=0, stdout=content.decode("utf-8"),
                               stderr="")

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
            # git refuses to update an existing tag ref: even an identical tree
            # arrives as a different commit, so existence alone rejects.
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
        os.remove(os.path.join(self.generated, "manifest.json"))
        self.assertRefusedBeforeGit(*self.publish())

    def test_missing_generated_directory_is_refused(self):
        code, output, runner = self.publish(
            generated_dir=os.path.join(self.tmp, "nope"))
        self.assertRefusedBeforeGit(code, output, runner)

    def test_manifest_version_must_match_the_requested_version(self):
        write_tree(self.generated, generated_files("9.9.9"))
        code, output, runner = self.publish()
        self.assertRefusedBeforeGit(code, output, runner)
        self.assertIn("9.9.9", output)

    def test_malformed_manifest_json_is_refused(self):
        write_tree(self.generated, {"manifest.json": b"{not json"})
        self.assertRefusedBeforeGit(*self.publish())

    def test_manifest_without_the_payload_addon_is_refused(self):
        files = generated_files()
        data = json.loads(files["manifest.json"])
        data["addons"] = [entry for entry in data["addons"]
                          if entry["id"] != PAYLOAD]
        write_tree(self.generated, {"manifest.json": json.dumps(data).encode()})
        self.assertRefusedBeforeGit(*self.publish())

    def test_index_md5_must_describe_the_generated_addons_xml(self):
        """A stale md5 would make Kodi skip the update; never publish it."""
        files = generated_files()
        data = json.loads(files["manifest.json"])
        data["index"]["md5"] = "0" * 32
        write_tree(self.generated, {"manifest.json": json.dumps(data).encode()})
        self.assertRefusedBeforeGit(*self.publish())

    def test_unexpected_extra_file_is_refused(self):
        write_tree(self.generated, {"service.advancedproxy-1.2.3.zip": b"PK\x03\x04"})
        self.assertRefusedBeforeGit(*self.publish())

    def test_binary_content_is_refused(self):
        """The target repository stays text-only; a blob here means a bad tree."""
        write_tree(self.generated, {"README.md": b"\x00\xff\xfe binary"})
        self.assertRefusedBeforeGit(*self.publish())

    def test_symlink_in_the_generated_tree_is_refused(self):
        path = os.path.join(self.generated, "README.md")
        os.remove(path)
        os.symlink("/etc/passwd", path)
        self.assertRefusedBeforeGit(*self.publish())

    def test_malformed_version_is_a_usage_error(self):
        code, output, runner = self.publish(version="1.2")
        self.assertEqual(code, 2, output)
        self.assertEqual(runner.calls, [])

    def test_missing_token_is_refused_before_any_command(self):
        code, output, runner = self.publish(environ={"PATH": "/usr/bin"})
        self.assertEqual(code, 2, output)
        self.assertEqual(runner.calls, [])
        self.assertIn(TOKEN_ENV, output)


class TestFreshPublish(PublishTestCase):
    """A new version: commit the tree, tag it, push both."""

    def setUp(self):
        super().setUp()
        stale = generated_files("1.2.2")
        self.runner = FakeGit(head_files=stale,
                              untracked={"CNAME": b"bigping.example\n",
                                         ".nojekyll": b""})
        self.code, self.output = self.run_publisher(self.publisher(self.runner))

    def test_succeeds(self):
        self.assertEqual(self.code, 0, self.output)

    def test_commits_with_the_agreed_message(self):
        commits = self.runner.ran("commit")
        self.assertEqual(len(commits), 1, commits)
        self.assertIn("Publish %s %s" % (PAYLOAD, VERSION), commits[0])

    def test_tags_the_published_version(self):
        self.assertEqual([c[4:] for c in self.runner.ran("tag")], [[TAG]])

    def test_pushes_branch_and_tag_together(self):
        self.assertEqual(len(self.runner.pushed), 1, self.runner.pushed)
        pushed = self.runner.pushed[0]
        self.assertIn(BRANCH, pushed)
        self.assertIn("refs/tags/" + TAG, pushed)

    def test_never_force_pushes(self):
        for call in self.runner.calls:
            self.assertNotIn("--force", call)
            self.assertNotIn("-f", call)
            self.assertNotIn("--force-with-lease", call)

    def test_target_receives_exactly_the_generated_files(self):
        for relative, content in generated_files().items():
            self.assertEqual(self.runner.published.get(relative), content,
                             relative)

    def test_preserves_unmanaged_files_and_the_git_directory(self):
        """Pages config lives in the target repo; publishing must not eat it."""
        self.assertEqual(self.runner.published.get("CNAME"),
                         b"bigping.example\n")
        self.assertEqual(self.runner.published.get(".nojekyll"), b"")
        self.assertTrue(self.runner.kept_git_dir)

    def test_stages_only_the_managed_paths(self):
        self.assertEqual(sorted(self.runner.staged), sorted(MANAGED))

    def test_configures_an_explicit_bot_identity_locally(self):
        configured = [c for c in self.runner.ran("config")]
        flat = [" ".join(c) for c in configured]
        self.assertTrue(any("user.name" in f for f in flat), flat)
        self.assertTrue(any("user.email" in f for f in flat), flat)
        for call in configured:
            self.assertIn("--local", call, "identity must not be global")

    def test_reports_what_it_published(self):
        self.assertIn("PUBLISHED", self.output)
        self.assertIn(TAG, self.output)


class TestIdempotency(PublishTestCase):
    def test_no_diff_and_matching_tag_skips_without_writing(self):
        runner = FakeGit(head_files=generated_files(),
                         tags={TAG: generated_files()})
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP", output)
        self.assertEqual(runner.ran("commit"), [])
        self.assertEqual(runner.ran("tag"), [])
        self.assertEqual(runner.pushed, [])

    def test_no_diff_and_missing_tag_pushes_the_tag_only(self):
        """A crash between commit and tag must be recoverable by re-running."""
        runner = FakeGit(head_files=generated_files())
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertEqual(runner.ran("commit"), [])
        self.assertEqual([c[4:] for c in runner.ran("tag")], [[TAG]])
        self.assertEqual(len(runner.pushed), 1)
        self.assertEqual(runner.pushed[0], ["origin", "refs/tags/" + TAG])
        self.assertNotIn(BRANCH, runner.pushed[0])

    def test_second_run_after_a_publish_is_a_skip(self):
        first = FakeGit(head_files=generated_files("1.2.2"))
        code, output, _ = self.publish(first)
        self.assertEqual(code, 0, output)
        second = FakeGit(head_files=first.head_files, tags=first.remote_tags)
        code, output, _ = self.publish(second)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP", output)
        self.assertEqual(second.pushed, [])


class TestTagConflict(PublishTestCase):
    """A published version is immutable: same tag, different bytes, is fatal."""

    def setUp(self):
        super().setUp()
        published = dict(generated_files())
        published["addons.xml"] = b"<addons><!-- different --></addons>\n"
        self.runner = FakeGit(head_files=generated_files("1.2.2"),
                              tags={TAG: published})
        self.code, self.output = self.run_publisher(self.publisher(self.runner))

    def test_aborts(self):
        self.assertNotEqual(self.code, 0)
        self.assertIn(TAG, self.output)

    def test_pushes_nothing(self):
        self.assertEqual(self.runner.pushed, [])

    def test_never_moves_or_deletes_the_published_tag(self):
        self.assertEqual([c for c in self.runner.ran("tag") if "-d" in c], [])
        for call in self.runner.calls:
            self.assertNotIn("--force", call)

    def test_tag_missing_a_managed_file_is_a_conflict(self):
        partial = dict(generated_files())
        partial.pop("README.md")
        runner = FakeGit(head_files=generated_files("1.2.2"),
                         tags={TAG: partial})
        code, output, _ = self.publish(runner)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(runner.pushed, [])


class TestRetry(PublishTestCase):
    def test_non_fast_forward_is_absorbed_by_fetch_rebase_retry(self):
        runner = FakeGit(head_files=generated_files("1.2.2"), reject_pushes=1)
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(runner.ran("fetch")), 1)
        self.assertEqual(len(runner.ran("rebase")), 1)
        self.assertEqual(len(runner.pushed), 1)

    def test_retries_at_most_three_pushes(self):
        runner = FakeGit(head_files=generated_files("1.2.2"), reject_pushes=99)
        code, output, _ = self.publish(runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(runner.ran("push")), 3, runner.ran("push"))

    def test_backs_off_between_attempts(self):
        runner = FakeGit(head_files=generated_files("1.2.2"), reject_pushes=2)
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(all(delay > 0 for delay in self.sleeps), self.sleeps)
        self.assertGreater(self.sleeps[1], self.sleeps[0], "no backoff growth")

    def test_retag_after_rebase_so_the_tag_follows_the_replayed_commit(self):
        """Rebase leaves the tag on the pre-rebase commit; it must be remade."""
        runner = FakeGit(head_files=generated_files("1.2.2"), reject_pushes=1)
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        tags = runner.ran("tag")
        self.assertIn(["git", "-C", runner.dest, "tag", "-d", TAG], tags)
        self.assertEqual(tags[-1][4:], [TAG], "tag not recreated after rebase")

    def test_a_tag_published_concurrently_with_other_content_aborts(self):
        """Another run wins the race with different bytes: refuse, never force."""
        stolen = dict(generated_files())
        stolen["addons.xml"] = b"<addons><!-- someone else --></addons>\n"
        runner = FakeGit(head_files=generated_files("1.2.2"), race_tag=stolen)
        code, output, _ = self.publish(runner)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(runner.pushed, [])
        self.assertNotIn(TAG, runner.local_tags, "orphan local tag survived")

    def test_a_tag_published_concurrently_with_identical_content_skips(self):
        """Losing the race to an identical tree is a success, not a failure."""
        runner = FakeGit(head_files=generated_files("1.2.2"),
                         race_tag=generated_files())
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP", output)
        self.assertNotIn("PUBLISHED", output,
                         "claimed a push that was rejected")
        self.assertEqual(runner.pushed, [])
        self.assertNotIn(TAG, runner.local_tags, "orphan local tag survived")


class TestFailure(PublishTestCase):
    def test_clone_failure_stops_before_any_write(self):
        runner = FakeGit(fail=[("gh", "repo", "clone")])
        code, output, _ = self.publish(runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(runner.ran("commit"), [])
        self.assertEqual(runner.pushed, [])

    def test_commit_failure_never_pushes(self):
        runner = FakeGit(head_files=generated_files("1.2.2"))
        runner._git_commit = lambda args: runner.err("commit failed")
        code, output, _ = self.publish(runner)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(runner.pushed, [])

    def test_terminal_push_failure_leaves_no_orphan_local_tag(self):
        """A leftover local tag would poison the next run's conflict check."""
        runner = FakeGit(head_files=generated_files("1.2.2"), reject_pushes=99)
        code, output, _ = self.publish(runner)
        self.assertNotEqual(code, 0)
        self.assertNotIn(TAG, runner.local_tags, "orphan local tag survived")
        self.assertIn(["git", "-C", runner.dest, "tag", "-d", TAG],
                      runner.ran("tag"))

    def test_failure_is_diagnosed_not_crashed(self):
        runner = FakeGit(fail=[("gh", "repo", "clone")])
        code, output, _ = self.publish(runner)
        self.assertIn("publish_repo:", output)
        self.assertNotIn("Traceback", output)


class TestSecrecy(PublishTestCase):
    def test_token_never_appears_in_any_argument(self):
        runner = FakeGit(head_files=generated_files("1.2.2"))
        code, output, _ = self.publish(runner)
        self.assertEqual(code, 0, output)
        self.assertTrue(runner.calls)
        for call in runner.calls:
            for argument in call:
                self.assertNotIn(TOKEN, argument, call)

    def test_token_never_appears_in_the_printed_transcript(self):
        runner = FakeGit(head_files=generated_files("1.2.2"))
        code, output, _ = self.publish(runner)
        self.assertNotIn(TOKEN, output)

    def test_command_output_mentioning_the_token_is_redacted(self):
        """Git echoes remote URLs; a leaked token must be scrubbed from logs."""
        runner = FakeGit(head_files=generated_files("1.2.2"))
        runner._git_fetch = lambda args: runner.err(
            "fatal: https://x-access-token:%s@github.com/%s denied"
            % (TOKEN, TARGET))
        runner.reject_pushes = 99
        code, output, _ = self.publish(runner)
        self.assertNotEqual(code, 0)
        self.assertNotIn(TOKEN, output)
        self.assertIn("***", output)

    def test_token_travels_to_git_only_through_the_environment(self):
        runner = FakeGit(head_files=generated_files("1.2.2"))
        self.publish(runner)
        clone_env = runner.envs[0]
        self.assertEqual(clone_env.get("GH_TOKEN"), TOKEN)

    def test_a_foreign_github_token_cannot_shadow_the_scoped_token(self):
        """Source-repo GITHUB_TOKEN in CI must not be used for the target."""
        runner = FakeGit(head_files=generated_files("1.2.2"))
        self.publish(runner, environ={TOKEN_ENV: TOKEN, "PATH": "/usr/bin",
                                      "GITHUB_TOKEN": "ghs_wrongrepotoken"})
        for env in runner.envs:
            self.assertNotEqual(env.get("GITHUB_TOKEN"), "ghs_wrongrepotoken")
            self.assertEqual(env.get("GH_TOKEN"), TOKEN)

    def test_credentials_are_never_prompted_for(self):
        runner = FakeGit(head_files=generated_files("1.2.2"))
        self.publish(runner)
        self.assertEqual(runner.envs[0].get("GIT_TERMINAL_PROMPT"), "0")


class TestDryRun(PublishTestCase):
    def test_runs_no_commands_and_needs_no_token(self):
        runner = FakeGit()
        code, output, _ = self.publish(runner, dry_run=True,
                                       environ={"PATH": "/usr/bin"})
        self.assertEqual(code, 0, output)
        self.assertEqual(runner.calls, [])

    def test_prints_the_plan(self):
        code, output, _ = self.publish(dry_run=True)
        self.assertEqual(code, 0, output)
        self.assertIn("dry-run", output)
        self.assertIn(TAG, output)
        self.assertIn(TARGET, output)

    def test_still_validates_the_generated_tree(self):
        os.remove(os.path.join(self.generated, "addons.xml"))
        code, output, runner = self.publish(dry_run=True)
        self.assertEqual(code, 1, output)
        self.assertEqual(runner.calls, [])


class TestPublisherContract(PublishTestCase):
    """The publisher's defaults must equal the approved publication targets."""

    def test_defaults_target_the_agreed_repository_and_token(self):
        import publish_repo
        self.assertEqual(publish_repo.DEFAULT_REPOSITORY, TARGET)
        self.assertEqual(publish_repo.DEFAULT_TOKEN_ENV, TOKEN_ENV)
        self.assertEqual(publish_repo.DEFAULT_BRANCH, BRANCH)
        self.assertEqual(publish_repo.BOT_NAME, BOT_NAME)
        self.assertEqual(publish_repo.BOT_EMAIL, BOT_EMAIL)

    def test_publisher_manages_only_the_five_generated_paths(self):
        import publish_repo
        self.assertEqual(publish_repo.MANAGED, MANAGED)
        for managed in publish_repo.MANAGED:
            self.assertFalse(managed.startswith(".github/"), managed)
            self.assertFalse(managed.startswith("scripts/"), managed)

    def test_publisher_never_force_pushes(self):
        import publish_repo
        source = read_bytes(os.path.join(SCRIPTS, "publish_repo.py")).decode("utf-8")
        for forbidden in ("--force", "--force-with-lease"):
            self.assertNotIn(forbidden, source, forbidden)
        # Match `-f` only as a standalone flag, not inside "non-fast-forward".
        self.assertNotRegex(source, r'(?<![-\w])-f(?![-\w])')


class TestDefaultsAndCli(PublishTestCase):
    def test_defaults_target_the_agreed_repository_and_token(self):
        self.assertEqual(publish_repo.DEFAULT_REPOSITORY, TARGET)
        self.assertEqual(publish_repo.DEFAULT_TOKEN_ENV, TOKEN_ENV)
        self.assertEqual(publish_repo.DEFAULT_BRANCH, BRANCH)

    def test_repository_and_branch_are_configurable(self):
        runner = FakeGit(head_files=generated_files("1.2.2"))
        self.publish(runner, repository="someone/else", branch="trunk")
        clone = next(c for c in runner.calls if c[:3] == ["gh", "repo", "clone"])
        self.assertEqual(clone[3], "someone/else")
        self.assertTrue(any("trunk" in " ".join(c) for c in runner.ran("checkout")))

    def test_token_env_is_configurable(self):
        runner = FakeGit(head_files=generated_files("1.2.2"))
        code, output, _ = self.publish(runner, token_env="OTHER_TOKEN",
                                       environ={"OTHER_TOKEN": TOKEN})
        self.assertEqual(code, 0, output)
        self.assertEqual(runner.envs[0].get("GH_TOKEN"), TOKEN)

    def test_cli_dry_run(self):
        process = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "publish_repo.py"),
             "--generated-dir", self.generated, "--version", VERSION,
             "--dry-run"], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(process.returncode, 0,
                         process.stdout + process.stderr)
        self.assertIn("dry-run", process.stdout)
        self.assertIn(TARGET, process.stdout)

    def test_cli_rejects_a_bad_version_as_usage_error(self):
        process = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "publish_repo.py"),
             "--generated-dir", self.generated, "--version", "banana",
             "--dry-run"], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(process.returncode, 2,
                         process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
