# Bootstrapping `maratdob118/kodi-addons`

`kodi-addons/` mirrors the files the Kodi repository needs but the
release publisher must never write. Copy them into the target repository once,
by hand, before the first release publishes.

```
kodi-addons/
├── .github/workflows/pages.yml   # downloads, verifies and deploys the payload
└── scripts/build_site.py         # builds the served tree from manifest.json
```

## Why by hand

`scripts/publish_repo.py` writes exactly five generated files into the target
(`addons.xml`, `addons.xml.md5`, `manifest.json`, `README.md`,
`repository.bigping/addon.xml`) and touches nothing else. That is a deliberate
limit, not an omission: a token that could create or edit a file under
`.github/workflows/` would need GitHub's `workflows` permission on top of
`Contents: write`, which is a far larger grant for a credential that lives in
another repository's CI. Keeping the workflow out of the published set is what
holds `KODI_ADDONS_TOKEN` down to `Contents: write` on one repository.

The cost is that these two files are updated by a human. The benefit is that a
compromised source-repo secret cannot change what the target repo executes.

## Steps

1. Create the target repo and enable Pages with **GitHub Actions** as the
   source (Settings → Pages → Build and deployment).
2. Clone it, copy this tree in, and push to `main`:

   ```bash
   git clone https://github.com/maratdob118/kodi-addons
   cp -r bootstrap/bigping.repository/. kodi-addons/
   cd kodi-addons && git add .github scripts && git commit -m "ci: bootstrap Pages deployment" && git push
   ```

   The first push has no `manifest.json` yet, so the workflow fails until the
   source repo publishes. That is expected.
3. Store the fine-grained PAT (`Contents: write`, scoped to this repository
   only) as `KODI_ADDONS_TOKEN` in `maratdob118/kodi-advanced-proxy`, and record
   its expiry date somewhere you will see it.

## Keeping it in sync

`tests/test_workflow_contracts.py` and `tests/test_site_builder.py` test this
template as it sits here. When either file changes, re-run the suite and copy
the change into the target repo the same way. Nothing detects drift for you:
the target is a separate repository, and by design this repo cannot write to it.
