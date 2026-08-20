# -*- coding: utf-8 -*-
"""Contract tests for the release workflow and the repository update template.

`.github/workflows/release.yml` cannot be run locally, and the kodi-addons
update workflow runs in another repository. These tests pin the properties a
reviewer would otherwise have to re-derive from YAML every time: which job may
write, in which order the release steps run, that every script flag the
workflow passes exists, and that the update template polls the source releases
and regenerates the classic zips/ tree without any cross-repository token.

Run:  python3 tests/test_workflow_contracts.py
      python3 -m unittest tests.test_workflow_contracts
"""
import os
import re
import sys
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import publish_repo  # noqa: E402

SOURCE_WORKFLOW = os.path.join(REPO, ".github", "workflows", "release.yml")
UPDATE_WORKFLOW = os.path.join(REPO, ".github", "update-repo.yml")

PAYLOAD = "service.advancedproxy"
REPOSITORY = "repository.maratdob118"
TARGET_REPO = "maratdob118/kodi-addons"
SOURCE_REPO = "maratdob118/kodi-advanced-proxy"
MAIN = "refs/heads/main"
PLATFORMS = ("linux_x64", "linux_x86", "linux_armv7", "linux_arm64",
             "android_arm64", "windows_x64", "darwin_x64", "darwin_arm64")
REPO_PLATFORMS = ("linux_arm64", "linux_armv7")

# A stable major tag (actions/checkout@v5) or an immutable 40-hex commit.
PINNED_RE = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w.-]+)*@(?:v[0-9]+|[0-9a-f]{40})$")


def read(path):
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def load(path):
    return yaml.safe_load(read(path))


def triggers(document):
    """`on:` is the YAML 1.1 boolean True once parsed; accept either key."""
    for key in ("on", True):
        if key in document:
            return document[key]
    raise AssertionError("workflow declares no triggers")


def steps(job):
    return job.get("steps") or []


def step_name(step):
    return step.get("name") or step.get("uses") or ""


def step_text(step):
    return step.get("run") or step.get("uses") or ""


def index_of(job, needle):
    """Index of the first step mentioning NEEDLE, or -1."""
    for position, step in enumerate(steps(job)):
        if needle in step_text(step):
            return position
    return -1


def uses_of(document):
    """Every `uses:` reference in the workflow, with its job and step name."""
    found = []
    for job_id, job in (document.get("jobs") or {}).items():
        for step in steps(job):
            if "uses" in step:
                found.append((job_id, step_name(step), step["uses"]))
    return found


class TestSourceWorkflow(unittest.TestCase):
    """The workflow that tests, builds and releases."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(SOURCE_WORKFLOW)
        cls.document = load(SOURCE_WORKFLOW)
        cls.jobs = cls.document["jobs"]

    def job(self, name):
        self.assertIn(name, self.jobs, "no %s job" % name)
        return self.jobs[name]

    # -- triggers ----------------------------------------------------------

    def test_runs_on_main_not_master(self):
        on = triggers(self.document)
        self.assertEqual(on["push"]["branches"], ["main"])
        self.assertEqual(on["pull_request"]["branches"], ["main"])
        self.assertIn("workflow_dispatch", on)
        self.assertNotIn("master", self.text)

    def test_path_filters_scope_the_monorepo(self):
        on = triggers(self.document)
        for event in ("push", "pull_request"):
            paths = on[event].get("paths")
            self.assertTrue(paths, "%s declares no path filter" % event)
            for required in ("build.sh", "scripts/**", "tests/**",
                             "%s/**" % PAYLOAD, "%s/**" % REPOSITORY):
                self.assertIn(required, paths,
                              "%s filter misses %s" % (event, required))

    def test_documentation_only_pushes_do_not_release(self):
        on = triggers(self.document)
        for event in ("push", "pull_request"):
            for path in on[event]["paths"]:
                self.assertNotIn("docs/", path)
                self.assertNotEqual(path, "README.md")

    # -- permissions -------------------------------------------------------

    def test_default_permissions_are_read_only(self):
        self.assertEqual(self.document["permissions"], {"contents": "read"})

    def test_only_the_release_job_may_write_contents(self):
        writers = [name for name, job in self.jobs.items()
                   if (job.get("permissions") or {}).get("contents") == "write"]
        self.assertEqual(writers, ["release"])

    def test_no_job_uses_a_cross_repository_token(self):
        for name, job in self.jobs.items():
            self.assertNotIn("KODI_ADDONS_TOKEN", yaml.safe_dump(job))
            self.assertNotIn("secrets.", yaml.safe_dump(job))

    def test_release_is_gated_on_a_push_to_main(self):
        condition = self.job("release").get("if", "")
        self.assertIn("github.event_name == 'push'", condition)
        self.assertIn(MAIN, condition)

    # -- build matrix ------------------------------------------------------

    def test_matrix_builds_every_platform(self):
        matrix = self.job("build")["strategy"]["matrix"]["platform"]
        self.assertEqual(sorted(matrix), sorted(PLATFORMS))

    def test_test_job_runs_the_whole_suite(self):
        runs = " ".join(step.get("run") or "" for step in steps(self.job("test")))
        self.assertIn("unittest discover", runs)
        self.assertIn("check_versions.sh", runs)
        self.assertIn("validate_addon.py", runs)

    # -- release job ordering ---------------------------------------------

    def test_release_aggregates_every_matrix_artifact(self):
        download = [step for step in steps(self.job("release"))
                    if "download-artifact" in step.get("uses", "")]
        self.assertEqual(len(download), 1)
        options = download[0]["with"]
        self.assertEqual(options["pattern"], "addon-*")
        self.assertTrue(options["merge-multiple"])

    def test_universal_is_assembled_and_verified_before_the_release(self):
        release = self.job("release")
        download = index_of(release, "artifacts")
        assemble = index_of(release, "make_universal.py")
        verify = index_of(release, "verify_zip.sh --universal")
        publish = index_of(release, "release.py")
        for label, position in (("download", download), ("make_universal", assemble),
                                ("verify_zip --universal", verify),
                                ("release.py", publish)):
            self.assertGreaterEqual(position, 0, "no %s step" % label)
        self.assertLess(download, assemble)
        self.assertLess(assemble, verify)
        self.assertLess(verify, publish)

    def test_no_generated_tree_is_committed(self):
        self.assertNotIn("git add", self.text)
        self.assertNotIn("git commit", self.text)

    # -- flags actually exist ---------------------------------------------

    def test_every_script_flag_the_workflow_passes_exists(self):
        """A renamed option must fail here, not halfway through a release."""
        scripts = ("make_universal.py", "release.py")
        checked = set()
        for job in self.jobs.values():
            for step in steps(job):
                run = step.get("run") or ""
                for script in scripts:
                    if "scripts/%s" % script not in run:
                        continue
                    source = read(os.path.join(SCRIPTS, script))
                    for option in set(re.findall(r"--[a-z][a-z-]+", run)):
                        self.assertIn('"%s"' % option, source,
                                      "%s does not accept %s" % (script, option))
                        checked.add((script, option))
        self.assertEqual({script for script, _ in checked}, set(scripts))
        self.assertGreaterEqual(len(checked), 2 * len(scripts),
                                "each script is invoked with real options")

    def test_actions_are_pinned(self):
        for job_id, name, uses in uses_of(self.document):
            self.assertRegex(uses, PINNED_RE, "%s/%s is unpinned" % (job_id, name))


class TestUpdateWorkflowTemplate(unittest.TestCase):
    """The template kodi-addons copies into .github/workflows/update-repo.yml.

    It lives at .github/update-repo.yml here (outside .github/workflows/ so it
    never runs in this repository) and is copied into kodi-addons once by hand.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = read(UPDATE_WORKFLOW)
        cls.document = load(UPDATE_WORKFLOW)
        cls.job = cls.document["jobs"]["update"]

    def test_polls_on_a_schedule_and_by_hand(self):
        on = triggers(self.document)
        self.assertIn("schedule", on)
        self.assertIn("workflow_dispatch", on)

    def test_runs_in_kodi_addons_with_contents_write(self):
        self.assertEqual(self.document["permissions"], {"contents": "write"})

    def test_never_cancels_midway(self):
        self.assertIs(self.document["concurrency"]["cancel-in-progress"], False)

    def test_skips_when_the_version_is_already_published(self):
        skip_steps = [step for step in steps(self.job)
                      if "already published" in step_name(step)]
        self.assertEqual(len(skip_steps), 1, "no skip step")
        checkout = index_of(self.job, "actions/checkout")
        self.assertLess(index_of(self.job, "resolve"),
                        checkout, "skip must come before the source clone")

    def test_polls_the_source_repository_releases(self):
        self.assertIn(SOURCE_REPO, self.text)
        self.assertIn("gh release view", self.text)
        self.assertNotIn("secrets.", self.text,
                         "no cross-repository token may be needed")

    def test_downloads_only_the_two_arm_platform_payloads(self):
        run = next(step.get("run") or "" for step in steps(self.job)
                   if "gh release download" in (step.get("run") or ""))
        for platform in REPO_PLATFORMS:
            self.assertIn(platform, run)
        self.assertNotIn("linux_x64", run, "the repo payload is ARM-only")

    def test_regenerates_with_the_source_scripts(self):
        run = "\n".join(step.get("run") or "" for step in steps(self.job))
        self.assertIn("make_universal.py", run)
        self.assertIn("--platforms", run)
        self.assertIn("generate_repo.py", run)
        self.assertIn("--payload", run)
        self.assertIn("--version", run)

    def test_commits_the_generated_tree_back(self):
        run = "\n".join(step.get("run") or "" for step in steps(self.job))
        self.assertIn("git commit", run)
        self.assertIn("git push", run)
        self.assertIn("kodi-addons-release-bot", run)

    def test_actions_are_pinned(self):
        for job_id, name, uses in uses_of(self.document):
            self.assertRegex(uses, PINNED_RE, "%s/%s is unpinned" % (job_id, name))

    def test_uses_only_the_repositorys_own_token(self):
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("GH_TOKEN: ${{ secrets.", self.text)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.text)


class TestRepositoryPublisherContract(unittest.TestCase):
    """publish_repo.py still exists for manual pushes of a generated tree."""

    def test_targets_the_agreed_repository_and_branch(self):
        self.assertEqual(publish_repo.DEFAULT_REPOSITORY, TARGET_REPO)
        self.assertEqual(publish_repo.DEFAULT_BRANCH, "main")

    def test_payload_and_repository_ids_are_agreed(self):
        self.assertEqual(publish_repo.PAYLOAD, PAYLOAD)
        self.assertEqual(publish_repo.REPOSITORY, REPOSITORY)

    def test_the_committed_zips_index_markers_are_managed(self):
        self.assertTrue(publish_repo.ADDONS_XML.startswith("zips/"))
        self.assertTrue(publish_repo.ADDONS_XML_MD5.startswith("zips/"))

    def test_publisher_manages_no_workflow_and_no_script(self):
        source = read(os.path.join(SCRIPTS, "publish_repo.py"))
        self.assertNotIn(".github/workflows", source)
        self.assertNotIn("workflow", source.replace("workflow_dispatch", ""))

    def test_publisher_preserves_every_unknown_target_file(self):
        source = read(os.path.join(SCRIPTS, "publish_repo.py"))
        self.assertIn("for relative, content in self.files.items()", source)
        self.assertNotIn("rmtree(self.dest", source)
        self.assertNotIn('"clean"', source)
        self.assertNotIn("git add --all", source)

    def test_nothing_is_force_pushed(self):
        source = read(os.path.join(SCRIPTS, "publish_repo.py"))
        self.assertNotIn("--force", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
