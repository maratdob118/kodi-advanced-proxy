#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Release planner for Advanced Proxy platform ZIPs.

Creates exactly one GitHub release per addon version:
  * skips cleanly when the release is already published
  * resumes an existing draft, replacing all assets before publishing
  * otherwise creates a draft, uploads every platform ZIP plus a generated
    SHA256SUMS file, and only then publishes it
  * aborts WITHOUT publishing when any create/upload step fails

Usage:
    python3 scripts/release.py --version X.Y.Z --assets-dir DIR --sha SHA
    python3 scripts/release.py --version X.Y.Z --assets-dir DIR --dry-run

Subprocess calls are isolated behind an injectable `runner` so tests can
record/fake gh invocations without touching the real GitHub CLI.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CHECKSUM_FILE = "SHA256SUMS"


def _default_runner(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


class ReleasePlanner:
    """Stateful planner; run() orchestrates skip / draft / upload / publish."""

    def __init__(self, version, assets_dir, sha=None, dry_run=False, runner=None):
        self.version = version
        self.tag = f"v{version}"
        self.assets_dir = assets_dir
        self.sha = sha
        self.dry_run = dry_run
        self.runner = runner or _default_runner
        self.assets = []
        self.checksums = None

    # -- asset discovery ----------------------------------------------------
    def discover_assets(self):
        if not os.path.isdir(self.assets_dir):
            return []
        names = sorted(
            n for n in os.listdir(self.assets_dir)
            if n.endswith(".zip")
            and os.path.isfile(os.path.join(self.assets_dir, n))
        )
        return [os.path.join(self.assets_dir, n) for n in names]

    # -- checksums ----------------------------------------------------------
    def write_checksums(self):
        if not self.assets:
            self.assets = self.discover_assets()
        path = os.path.join(self.assets_dir, CHECKSUM_FILE)
        with open(path, "w", encoding="utf-8") as fh:
            for asset in self.assets:
                digest = hashlib.sha256()
                with open(asset, "rb") as src:
                    for chunk in iter(lambda: src.read(65536), b""):
                        digest.update(chunk)
                fh.write(f"{digest.hexdigest()}  {os.path.basename(asset)}\n")
        self.checksums = path
        return path

    # -- gh operations ------------------------------------------------------
    def release_state(self):
        proc = self._gh(
            ["gh", "release", "view", self.tag, "--json", "isDraft"])
        if proc.returncode != 0:
            return "missing"
        data = json.loads(proc.stdout)
        return "draft" if data["isDraft"] else "published"

    def create_draft(self):
        cmd = ["gh", "release", "create", self.tag, "--draft"]
        if self.sha:
            cmd += ["--target", self.sha]
        cmd += ["--title", f"Advanced Proxy {self.version}",
                "--notes",
                f"Advanced Proxy {self.version}: {len(self.assets)} platform "
                f"assets + {CHECKSUM_FILE}."]
        proc = self._gh(cmd)
        return proc.returncode == 0

    def upload(self, path, clobber=False):
        cmd = ["gh", "release", "upload", self.tag, path]
        if clobber:
            cmd.append("--clobber")
        proc = self._gh(cmd)
        return proc.returncode == 0

    def publish(self):
        proc = self._gh(["gh", "release", "edit", self.tag, "--draft=false"])
        return proc.returncode == 0

    def _gh(self, cmd):
        print("+ " + " ".join(cmd))
        return self.runner(cmd)

    # -- orchestration ------------------------------------------------------
    def run(self):
        if not VERSION_RE.match(self.version or ""):
            print(f"ERROR: version {self.version!r} is not X.Y.Z")
            return 2
        if not os.path.isdir(self.assets_dir):
            print(f"ERROR: assets dir not found: {self.assets_dir}")
            return 2
        self.assets = self.discover_assets()
        if not self.assets:
            print(f"ERROR: no *.zip assets found in {self.assets_dir}")
            return 2
        sums = self.write_checksums()
        print(f"assets: {len(self.assets)} zip(s) + {os.path.basename(sums)} "
              f"in {self.assets_dir}")

        if self.dry_run:
            self._print_dry_run()
            return 0

        state = self.release_state()
        if state == "published":
            print(f"SKIP: release {self.tag} already exists")
            return 0
        if state == "missing" and not self.create_draft():
            print(f"ERROR: failed to create draft {self.tag}")
            return 1
        for asset in self.assets + [sums]:
            if not self.upload(asset, clobber=state == "draft"):
                print(f"ABORT: upload failed for {os.path.basename(asset)}; "
                      f"draft {self.tag} NOT published")
                return 1
        if not self.publish():
            print(f"ERROR: failed to publish {self.tag}")
            return 1
        print(f"PUBLISHED: {self.tag}")
        return 0

    def _print_dry_run(self):
        print(f"[dry-run] would inspect {self.tag}: skip if published, "
              "resume if draft, create if missing")
        draft = f"gh release create {self.tag} --draft"
        if self.sha:
            draft += f" --target {self.sha}"
        print(f"[dry-run] would create draft: {draft} --title ... --notes ...")
        for asset in self.assets + [self.checksums]:
            print(f"[dry-run] would upload: gh release upload {self.tag} {asset}")
        print(f"[dry-run] would publish: gh release edit {self.tag} --draft=false")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create/publish one GitHub release per addon version.")
    parser.add_argument("--version", required=True,
                        help="addon version X.Y.Z (source of truth: addon.xml)")
    parser.add_argument("--assets-dir", required=True,
                        help="directory containing the platform ZIPs")
    parser.add_argument("--sha", default=None,
                        help="target commit SHA for the release")
    parser.add_argument("--dry-run", action="store_true",
                        help="rehearse: write checksums, run no gh commands")
    args = parser.parse_args(argv)
    planner = ReleasePlanner(version=args.version, assets_dir=args.assets_dir,
                             sha=args.sha, dry_run=args.dry_run)
    return planner.run()


if __name__ == "__main__":
    sys.exit(main())
