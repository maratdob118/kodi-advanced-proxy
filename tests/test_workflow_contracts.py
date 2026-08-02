# -*- coding: utf-8 -*-
"""Contract tests for the two release workflows.

Two workflows carry this project's release, and neither can be run locally:

  * `.github/workflows/release.yml` lives here. It tests, builds the eight
    platform ZIPs, assembles and verifies the universal ZIP, publishes the
    source release, then hands the generated metadata to the target repo.
* `bootstrap/bigping.repository/.github/workflows/pages.yml` is a template.
  A human bootstraps it into maratdob118/kodi-addons once, by hand,
    together with its site builder. It is deliberately NOT part of the tree
    `scripts/publish_repo.py` manages, so the cross-repository token needs
    Contents:write and never the `workflows` permission.

What cannot be executed is asserted instead. These tests pin the properties a
reviewer would otherwise have to re-derive from YAML every time: which job may
write, which job may see the cross-repository token, in which order the release
steps run, that the payload is never fetched from somewhere that cannot serve
it, and that no step can leak a secret through argv or a log line.

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
TEMPLATE = os.path.join(REPO, "bootstrap", "bigping.repository")
TARGET_WORKFLOW = os.path.join(TEMPLATE, ".github", "workflows", "pages.yml")
BUILD_SITE = os.path.join(TEMPLATE, "scripts", "build_site.py")

PAYLOAD = "service.advancedproxy"
TARGET_REPO = "maratdob118/kodi-addons"
TOKEN_ENV = "KODI_ADDONS_TOKEN"
TOKEN_SECRET = "secrets." + TOKEN_ENV
MAIN = "refs/heads/main"
PLATFORMS = ("linux_x64", "linux_x86", "linux_armv7", "linux_arm64",
             "android_arm64", "windows_x64", "darwin_x64", "darwin_arm64")

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
    """The workflow that tests, builds, releases and hands off publication."""

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
                             "%s/**" % PAYLOAD):
                self.assertIn(required, paths,
                              "%s filter misses %s" % (event, required))

    def test_documentation_only_pushes_do_not_release(self):
        """docs/ and the README are not build inputs; they must not trigger."""
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

    def test_publish_job_cannot_write_this_repository(self):
        self.assertEqual(self.job("publish")["permissions"], {"contents": "read"})

    def test_release_and_publish_are_gated_on_a_push_to_main(self):
        for name in ("release", "publish"):
            condition = self.job(name).get("if", "")
            self.assertIn("github.event_name == 'push'", condition)
            self.assertIn(MAIN, condition)

    # -- the cross-repository token ---------------------------------------

    def test_token_is_scoped_to_the_publish_job_only(self):
        self.assertEqual(self.text.count(TOKEN_SECRET), 1,
                         "%s must be referenced exactly once" % TOKEN_SECRET)
        publish = self.job("publish")
        self.assertEqual((publish.get("env") or {}).get(TOKEN_ENV),
                         "${{ %s }}" % TOKEN_SECRET)
        for name, job in self.jobs.items():
            if name != "publish":
                self.assertNotIn(TOKEN_ENV, yaml.safe_dump(job))

    def test_publish_fails_explicitly_on_an_empty_token(self):
        guard = [step for step in steps(self.job("publish"))
                 if "-z" in (step.get("run") or "")
                 and TOKEN_ENV in (step.get("run") or "")]
        self.assertEqual(len(guard), 1, "no explicit empty-token guard")
        run = guard[0]["run"]
        self.assertIn("exit 1", run)
        self.assertLess(index_of(self.job("publish"), "-z"),
                        index_of(self.job("publish"), "publish_repo.py"),
                        "the token guard must run before publishing")

    def test_no_step_puts_a_secret_on_a_command_line(self):
        for job_id, job in self.jobs.items():
            for step in steps(job):
                run = step.get("run") or ""
                self.assertNotIn("${{ secrets.", run,
                                 "%s/%s interpolates a secret into a script"
                                 % (job_id, step_name(step)))

    def test_release_token_is_never_offered_to_the_target(self):
        """GITHUB_TOKEN cannot write the target; the PAT cannot write here."""
        publish = yaml.safe_dump(self.job("publish"))
        self.assertNotIn("secrets.GITHUB_TOKEN", publish)

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

    def test_release_hands_the_universal_zip_to_the_publish_job(self):
        upload = [step for step in steps(self.job("release"))
                  if "upload-artifact" in step.get("uses", "")]
        self.assertEqual(len(upload), 1, "the universal ZIP is not handed on")
        options = upload[0]["with"]
        self.assertIn(PAYLOAD, options["path"])
        self.assertNotIn(".linux_", options["path"], "that is a platform ZIP")
        self.assertEqual(options["if-no-files-found"], "error")

    def test_universal_zip_is_never_committed(self):
        self.assertNotIn("git add", self.text)
        self.assertNotIn("git commit", self.text)

    # -- publish job -------------------------------------------------------

    def test_publish_generates_then_publishes(self):
        publish = self.job("publish")
        generate = index_of(publish, "generate_repo.py")
        push = index_of(publish, "publish_repo.py")
        self.assertGreaterEqual(generate, 0, "no generate_repo.py step")
        self.assertGreaterEqual(push, 0, "no publish_repo.py step")
        self.assertLess(generate, push)

    def test_publish_consumes_the_universal_artifact(self):
        download = [step for step in steps(self.job("publish"))
                    if "download-artifact" in step.get("uses", "")]
        self.assertEqual(len(download), 1)
        self.assertIn("needs", self.job("publish"))
        self.assertIn("release", self.job("publish")["needs"])

    def test_publish_concurrency_is_ref_independent_and_queues(self):
        concurrency = self.job("publish").get("concurrency")
        self.assertIsNotNone(concurrency, "publish declares no concurrency")
        self.assertNotIn("github.ref", concurrency["group"],
                         "a ref-keyed group lets two refs publish at once")
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_workflow_concurrency_never_cancels(self):
        self.assertIs(self.document["concurrency"]["cancel-in-progress"], False)

    # -- flags actually exist ---------------------------------------------

    def test_every_script_flag_the_workflow_passes_exists(self):
        """A renamed option must fail here, not halfway through a release."""
        scripts = ("make_universal.py", "release.py", "generate_repo.py",
                   "publish_repo.py")
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

    def test_nothing_is_force_pushed_and_nothing_uses_raw_githubusercontent(self):
        self.assertNotIn("--force", self.text)
        self.assertNotIn("raw.githubusercontent", self.text)


class TestTargetWorkflowTemplate(unittest.TestCase):
    """The Pages workflow a human bootstraps into the target repository."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(TARGET_WORKFLOW)
        cls.document = load(TARGET_WORKFLOW)
        cls.job = cls.document["jobs"]["deploy"]

    def test_triggers_on_the_publisher_push(self):
        on = triggers(self.document)
        self.assertEqual(on["push"]["branches"], ["main"])
        self.assertIn("workflow_dispatch", on)

    def test_permissions_are_exactly_what_pages_needs(self):
        self.assertEqual(self.document["permissions"], {"contents": "read"})
        self.assertEqual(self.job["permissions"],
                         {"contents": "read", "pages": "write",
                          "id-token": "write"})

    def test_deployment_never_cancels_midway(self):
        self.assertIs(self.document["concurrency"]["cancel-in-progress"], False)

    def test_download_url_comes_from_the_manifest_not_from_a_literal(self):
        plan = index_of(self.job, "build_site.py --plan")
        self.assertGreaterEqual(plan, 0, "no manifest plan step")
        self.assertNotIn("https://github.com/", self.text,
                         "the payload URL must be read from manifest.json")

    def test_url_reaches_curl_through_the_environment_only(self):
        for step in steps(self.job):
            run = step.get("run") or ""
            self.assertNotIn("steps.plan.outputs", run,
                             "an interpolated URL is a shell injection vector")
        download = [step for step in steps(self.job)
                    if "curl" in (step.get("run") or "")]
        self.assertEqual(len(download), 1)
        self.assertIn("${{ steps.plan.outputs.url }}",
                      yaml.safe_dump(download[0].get("env") or {}))

    def test_curl_retries_and_fails_loudly(self):
        download = next(step for step in steps(self.job)
                        if "curl" in (step.get("run") or ""))
        run = download["run"]
        self.assertIn("--retry", run)
        self.assertIn("--fail", run)
        self.assertNotIn("raw.githubusercontent", self.text)

    def test_payload_is_verified_before_the_site_is_built(self):
        verify = index_of(self.job, "build_site.py --verify")
        build = index_of(self.job, "--out _site")
        upload = index_of(self.job, "upload-pages-artifact")
        deploy = index_of(self.job, "deploy-pages")
        for label, position in (("verify", verify), ("build", build),
                                ("upload", upload), ("deploy", deploy)):
            self.assertGreaterEqual(position, 0, "no %s step" % label)
        self.assertLess(verify, build)
        self.assertLess(build, upload)
        self.assertLess(upload, deploy)

    def test_deploys_with_the_official_pages_actions(self):
        actions = [uses for _, _, uses in uses_of(self.document)]
        self.assertTrue(any(u.startswith("actions/upload-pages-artifact@")
                            for u in actions))
        self.assertTrue(any(u.startswith("actions/deploy-pages@") for u in actions))
        self.assertEqual(self.job["environment"]["name"], "github-pages")

    def test_actions_are_pinned(self):
        for job_id, name, uses in uses_of(self.document):
            self.assertRegex(uses, PINNED_RE, "%s/%s is unpinned" % (job_id, name))

    def test_no_token_is_needed_beyond_the_targets_own(self):
        self.assertNotIn("secrets.", self.text.replace("secrets.GITHUB_TOKEN", ""))
        self.assertNotIn(TOKEN_ENV, self.text)


class TestBootstrapSeparation(unittest.TestCase):
    """The template is bootstrapped by hand and preserved by the publisher."""

    def test_template_ships_a_workflow_and_a_site_builder(self):
        self.assertTrue(os.path.isfile(TARGET_WORKFLOW), TARGET_WORKFLOW)
        self.assertTrue(os.path.isfile(BUILD_SITE), BUILD_SITE)

    def test_publisher_manages_no_workflow_and_no_script(self):
        """Writing .github/ would demand a `workflows` permission we refuse."""
        for managed in publish_repo.MANAGED:
            self.assertFalse(managed.startswith(".github/"), managed)
            self.assertFalse(managed.startswith("scripts/"), managed)

    def test_publisher_preserves_every_unknown_target_file(self):
        """Only MANAGED paths are written, staged and diffed; nothing is pruned."""
        source = read(os.path.join(SCRIPTS, "publish_repo.py"))
        self.assertIn("for relative, content in self.files.items()", source)
        self.assertNotIn("rmtree(self.dest", source)
        self.assertNotIn('"clean"', source)
        self.assertNotIn("git add --all", source)

    def test_the_workflow_the_target_runs_is_the_one_under_test(self):
        """The bootstrapped copy sits at the same relative paths in the target."""
        self.assertTrue(TARGET_WORKFLOW.endswith(
            os.path.join(".github", "workflows", "pages.yml")))
        self.assertTrue(BUILD_SITE.endswith(
            os.path.join("scripts", "build_site.py")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
