#!/usr/bin/env python3
"""Publish the generated tree into the target repository.

`scripts/generate_repo.py` builds a small text-only tree; this script mirrors
that tree into maratdob118/bigping.repository, where a Pages workflow reacts to
the push and deploys the actual add-on ZIPs. Only the generated files are
managed here: the target's .git, its Pages configuration and anything else it
carries are left untouched.

The operation is designed to be repeated. CI may re-run a release, a job may
die between the commit and the tag, and two runs may race:

    no diff + tag vVERSION already present   -> SKIP, nothing is written
    no diff + tag missing                    -> tag and push the tag only
    diff                                     -> commit, tag, push both

A tag that already exists but carries DIFFERENT content is a conflict: a
published version is immutable, so the run aborts instead of moving it. Nothing
is ever force-pushed. A push rejected as non-fast-forward is retried up to three
times behind fetch/rebase with a growing backoff, and a run that ultimately
fails deletes the local tag it created so the next run starts clean.

The target is written with a fine-grained token that is scoped to that one
repository with Contents:write. It reaches git only through the environment
(GH_TOKEN, consumed by gh acting as a credential helper), never through argv,
and it is scrubbed from anything this script prints.

Usage:
    python3 scripts/publish_repo.py --generated-dir DIR --version X.Y.Z
    python3 scripts/publish_repo.py --generated-dir DIR --version X.Y.Z --dry-run

Subprocess calls and sleeping are isolated behind an injectable runner/sleep so
tests can drive the whole flow without touching real git, gh or the network.

Exit 0 on success or skip, 1 when publishing is refused or fails, 2 on usage
and configuration errors.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

PAYLOAD = "service.advancedproxy"
REPOSITORY = "repository.bigping"
DEFAULT_REPOSITORY = "maratdob118/bigping.repository"
DEFAULT_TOKEN_ENV = "BIGPING_REPOSITORY_TOKEN"
DEFAULT_BRANCH = "main"
MANAGED = ("addons.xml", "addons.xml.md5", "manifest.json", "README.md",
           REPOSITORY + "/addon.xml")
MANIFEST = "manifest.json"
ADDONS_XML = "addons.xml"
SCHEMA = 1
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
MAX_FILE = 1 << 20
BOT_NAME = "bigping-release-bot"
BOT_EMAIL = "bigping-release-bot@users.noreply.github.com"
CREDENTIAL_KEY = "credential.https://github.com.helper"
CREDENTIAL_HELPER = "!gh auth git-credential"
COMMIT_MESSAGE = "chore: publish %s %s"
PUSH_ATTEMPTS = 3
BACKOFF = 2.0
REDACTED = "***"
REMOTE_TAG_REF = "refs/publish/remote-tag"
NON_FAST_FORWARD = ("fetch first", "non-fast-forward")
TAG_EXISTS = "already exists"


class PublishError(Exception):
    """A refusal to publish: the inputs, the target or a command said no."""


class UsageError(Exception):
    """A misconfiguration: bad arguments or a missing token."""


def _default_runner(cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


class RepositoryPublisher:
    """Mirrors one generated tree into the target repository, idempotently."""

    def __init__(self, generated_dir, version, repository=DEFAULT_REPOSITORY,
                 token_env=DEFAULT_TOKEN_ENV, branch=DEFAULT_BRANCH,
                 dry_run=False, runner=None, sleep=None, environ=None):
        self.generated_dir = generated_dir
        self.version = version
        self.tag = "v%s" % version
        self.repository = repository
        self.token_env = token_env
        self.branch = branch
        self.dry_run = dry_run
        self.runner = runner or _default_runner
        self.sleep = sleep or time.sleep
        self.environ = dict(os.environ if environ is None else environ)
        self.token = None
        self.files = {}
        self.dest = None
        self.tagged = False

    # -- inputs -------------------------------------------------------------

    def load_generated(self):
        """Read the generated tree, refusing anything that is not ours to push."""
        if not os.path.isdir(self.generated_dir):
            raise PublishError("generated dir not found: %s" % self.generated_dir)
        found = []
        for directory, _, names in os.walk(self.generated_dir):
            for name in names:
                path = os.path.join(directory, name)
                relative = os.path.relpath(path, self.generated_dir)
                found.append(relative.replace(os.sep, "/"))
        unexpected = sorted(set(found) - set(MANAGED))
        if unexpected:
            raise PublishError("refusing to publish unmanaged file(s): %s"
                               % ", ".join(unexpected))
        missing = sorted(set(MANAGED) - set(found))
        if missing:
            raise PublishError("generated tree is incomplete, missing: %s"
                               % ", ".join(missing))
        files = {}
        for relative in MANAGED:
            path = os.path.join(self.generated_dir, relative.replace("/", os.sep))
            if os.path.islink(path):
                raise PublishError("refusing to publish a symlink: %s" % relative)
            size = os.path.getsize(path)
            if size > MAX_FILE:
                raise PublishError("%s is %d bytes; the target stays text-only"
                                   % (relative, size))
            with open(path, "rb") as stream:
                raw = stream.read()
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                raise PublishError("%s is not UTF-8 text; the target stays "
                                   "text-only" % relative)
            files[relative] = raw
        self.check_manifest(files)
        return files

    def check_manifest(self, files):
        """The manifest must describe this version and match the index beside it."""
        try:
            manifest = json.loads(files[MANIFEST].decode("utf-8"))
        except ValueError as error:
            raise PublishError("%s is not valid JSON: %s" % (MANIFEST, error))
        if not isinstance(manifest, dict):
            raise PublishError("%s must hold a JSON object" % MANIFEST)
        if manifest.get("schema") != SCHEMA:
            raise PublishError("%s declares schema %r, expected %d"
                               % (MANIFEST, manifest.get("schema"), SCHEMA))
        addons = manifest.get("addons")
        if not isinstance(addons, list):
            raise PublishError("%s declares no addons list" % MANIFEST)
        payload = next((entry for entry in addons
                        if isinstance(entry, dict) and entry.get("id") == PAYLOAD),
                       None)
        if payload is None:
            raise PublishError("%s declares no %s entry" % (MANIFEST, PAYLOAD))
        if payload.get("version") != self.version:
            raise PublishError("%s declares %s %s, expected %s"
                               % (MANIFEST, PAYLOAD, payload.get("version"),
                                  self.version))
        recorded = (manifest.get("index") or {}).get("md5")
        digest = hashlib.md5(files[ADDONS_XML]).hexdigest()
        if recorded != digest:
            raise PublishError(
                "%s records md5 %r but %s hashes to %s; the tree is stale"
                % (MANIFEST, recorded, ADDONS_XML, digest))

    def resolve_token(self):
        token = (self.environ.get(self.token_env) or "").strip()
        if not token:
            raise UsageError("%s is unset or empty; a fine-grained token with "
                             "Contents:write on %s is required"
                             % (self.token_env, self.repository))
        return token

    # -- commands -----------------------------------------------------------

    def command_env(self):
        """gh reads the token from here; it must never reach a command line."""
        env = dict(self.environ)
        env.pop("GITHUB_TOKEN", None)  # the source repo's token is the wrong one
        env["GH_TOKEN"] = self.token
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        return env

    def redact(self, text):
        if self.token and text:
            return text.replace(self.token, REDACTED)
        return text or ""

    def execute(self, cmd):
        if self.token and any(self.token in argument for argument in cmd):
            raise PublishError("refusing to run a command carrying the token")
        print("+ " + " ".join(cmd))
        result = self.runner(cmd, env=self.command_env())
        return result

    def git(self, *args):
        return self.execute(["git", "-C", self.dest] + list(args))

    def output_of(self, result):
        return self.redact((result.stdout or "") + (result.stderr or "")).strip()

    def checked(self, result, message):
        if result.returncode != 0:
            raise PublishError("%s: %s" % (message, self.output_of(result)))
        return result

    # -- target repository --------------------------------------------------

    def clone(self, dest):
        """gh clones over https with the token taken from GH_TOKEN."""
        self.dest = dest
        self.checked(
            self.execute(["gh", "repo", "clone", self.repository, dest]),
            "cannot clone %s" % self.repository)

    def configure(self):
        """Pin an explicit identity and let gh answer git's credential prompt."""
        for key, value in ((("user.name", BOT_NAME)),
                           (("user.email", BOT_EMAIL)),
                           ((CREDENTIAL_KEY, CREDENTIAL_HELPER))):
            self.checked(self.git("config", "--local", "--replace-all",
                                  key, value),
                         "cannot configure %s" % key)

    def checkout(self):
        """Track the remote branch, or start it when the target is empty."""
        remote = "refs/remotes/origin/%s" % self.branch
        if self.git("rev-parse", "--verify", "--quiet", remote).returncode == 0:
            self.checked(self.git("checkout", "-B", self.branch,
                                  "origin/%s" % self.branch),
                         "cannot check out %s" % self.branch)
        else:
            self.checked(self.git("checkout", "-B", self.branch),
                         "cannot start branch %s" % self.branch)

    def sync(self):
        """Write the managed files; everything else in the target is left alone."""
        for relative, content in self.files.items():
            path = os.path.join(self.dest, relative.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as stream:
                stream.write(content)

    def stage(self):
        """Stage the managed paths; True when they differ from the target's HEAD."""
        self.checked(self.git("add", "--", *MANAGED), "cannot stage the tree")
        result = self.git("diff", "--cached", "--quiet", "--", *MANAGED)
        if result.returncode not in (0, 1):
            raise PublishError("cannot diff the staged tree: %s"
                               % self.output_of(result))
        return result.returncode == 1

    def tag_exists(self):
        return self.git("rev-parse", "--verify", "--quiet",
                        "refs/tags/%s" % self.tag).returncode == 0

    def tag_matches(self, ref):
        """Does the tree recorded at REF equal the tree we were asked to publish?"""
        for relative, content in self.files.items():
            result = self.git("show", "%s:%s" % (ref, relative))
            if result.returncode != 0:
                return False
            if result.stdout != content.decode("utf-8"):
                return False
        return True

    def check_published_tag(self, ref):
        """A published version is immutable: same tag, other bytes, is fatal."""
        if not self.tag_matches(ref):
            raise PublishError(
                "%s already exists in %s with different content; a published "
                "version is immutable, refusing to move it. Bump the addon "
                "version and re-run."
                % (self.tag, self.repository))

    def commit(self):
        self.checked(self.git("commit", "-m",
                              COMMIT_MESSAGE % (PAYLOAD, self.version)),
                     "cannot commit the tree")

    def create_tag(self):
        self.checked(self.git("tag", self.tag), "cannot tag %s" % self.tag)
        self.tagged = True

    def delete_tag(self):
        self.git("tag", "-d", self.tag)
        self.tagged = False

    # -- pushing ------------------------------------------------------------

    def push(self, refs, rebase):
        """Push REFS, absorbing a concurrent push with fetch/rebase/backoff.

        Returns True when our refs landed, False when an identical tree was
        published by a concurrent run and ours became redundant.
        """
        for attempt in range(1, PUSH_ATTEMPTS + 1):
            result = self.git("push", *refs)
            if result.returncode == 0:
                return True
            output = self.output_of(result)
            lowered = output.lower()
            if TAG_EXISTS in lowered:
                self.absorb_remote_tag()
                return False
            retryable = any(needle in lowered for needle in NON_FAST_FORWARD)
            if not retryable or attempt == PUSH_ATTEMPTS:
                raise PublishError("push rejected after %d attempt(s): %s"
                                   % (attempt, output))
            self.sleep(BACKOFF * attempt)
            self.checked(self.git("fetch", "origin"), "cannot fetch origin")
            if rebase:
                self.checked(self.git("rebase", "origin/%s" % self.branch),
                             "cannot rebase onto origin/%s" % self.branch)
                # Rebase replays the commit, leaving the tag on the old one.
                self.delete_tag()
                self.create_tag()

    def absorb_remote_tag(self):
        """The tag landed while we were pushing: accept it only if it is ours."""
        fetched = self.git("fetch", "origin", "+refs/tags/%s:%s"
                           % (self.tag, REMOTE_TAG_REF))
        if fetched.returncode != 0:
            raise PublishError(
                "%s was rejected as already existing in %s, but it cannot be "
                "fetched back for comparison: %s"
                % (self.tag, self.repository, self.output_of(fetched)))
        self.check_published_tag(REMOTE_TAG_REF)
        if self.tagged:
            self.delete_tag()  # theirs is the published one; ours never landed
        print("SKIP: %s was published concurrently with identical content"
              % self.tag)

    # -- orchestration ------------------------------------------------------

    def publish(self, dest):
        self.clone(dest)
        self.configure()
        self.checkout()
        self.sync()
        changed = self.stage()
        if self.tag_exists():
            self.check_published_tag("refs/tags/%s" % self.tag)
            print("SKIP: %s already published in %s" % (self.tag, self.repository))
            return 0
        try:
            if changed:
                self.commit()
                self.create_tag()
                if self.push(["--atomic", "origin", self.branch,
                              "refs/tags/%s" % self.tag], rebase=True):
                    print("PUBLISHED: %s %s -> %s (%s, %s)"
                          % (PAYLOAD, self.version, self.repository,
                             self.branch, self.tag))
            else:
                self.create_tag()
                if self.push(["origin", "refs/tags/%s" % self.tag],
                             rebase=False):
                    print("TAGGED: %s already committed in %s, pushed %s only"
                          % (self.version, self.repository, self.tag))
        except PublishError:
            if self.tagged:
                self.delete_tag()  # never leave an orphan tag behind
            raise
        return 0

    def run(self):
        try:
            if not VERSION_RE.match(self.version or ""):
                raise UsageError("version %r is not X.Y.Z" % self.version)
            self.files = self.load_generated()
            if self.dry_run:
                self.print_plan()
                return 0
            self.token = self.resolve_token()
            workdir = tempfile.mkdtemp(prefix="publish-repo-")
            try:
                return self.publish(os.path.join(workdir, "target"))
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
        except UsageError as error:
            print("publish_repo: %s" % error)
            return 2
        except PublishError as error:
            print("publish_repo: %s" % self.redact(str(error)))
            return 1

    def print_plan(self):
        print("[dry-run] validated %d generated file(s) in %s"
              % (len(self.files), self.generated_dir))
        print("[dry-run] would clone %s (branch %s) using $%s"
              % (self.repository, self.branch, self.token_env))
        print("[dry-run] would replace only: %s" % ", ".join(MANAGED))
        print("[dry-run] would skip if %s exists with identical content, "
              "abort if it exists with different content" % self.tag)
        print("[dry-run] would commit %r, tag %s and push both to %s"
              % (COMMIT_MESSAGE % (PAYLOAD, self.version), self.tag,
                 self.branch))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="publish_repo.py",
        description="Publish the generated tree into the target repository.")
    parser.add_argument("--generated-dir", required=True,
                        help="tree produced by scripts/generate_repo.py")
    parser.add_argument("--version", required=True,
                        help="addon version X.Y.Z (source of truth: addon.xml)")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY,
                        help="target repository (default: %(default)s)")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV,
                        help="env var holding the fine-grained token scoped to "
                             "the target (default: %(default)s)")
    parser.add_argument("--branch", default=DEFAULT_BRANCH,
                        help="target branch (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="rehearse: validate and print the plan, run no "
                             "git or gh commands")
    args = parser.parse_args(argv)
    if not VERSION_RE.match(args.version):
        parser.error("version must be X.Y.Z: %s" % args.version)

    publisher = RepositoryPublisher(
        generated_dir=args.generated_dir, version=args.version,
        repository=args.repository, token_env=args.token_env,
        branch=args.branch, dry_run=args.dry_run)
    return publisher.run()


if __name__ == "__main__":
    sys.exit(main())
