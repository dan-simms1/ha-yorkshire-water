# HACS submission runbook

This file is the maintainer's checklist for getting this integration
into the HACS default store. Most users never need to read this; it
is a runbook for the repo owner.

## Prerequisite: pyyorkshirewater must be on PyPI

HACS requires hassfest to pass, and hassfest rejects
`package @ git+https://...` requirement strings. The integration
therefore depends on the published library, not the git URL.

### One-time PyPI Trusted Publisher setup

1. Sign in to PyPI.
2. Open <https://pypi.org/manage/account/publishing/> →
   *Add a new pending publisher*.
3. Fill the form:
   - **PyPI Project Name:** `pyyorkshirewater`
   - **Owner:** `dan-simms1`
   - **Repository name:** `pyyorkshirewater`
   - **Workflow filename:** `publish.yml`
   - **Environment name:** `pypi`
4. Save.

That is the only credentials step. From then on, every tag pushed to
`pyyorkshirewater` matching `v*.*.*` will publish to PyPI via OIDC,
no API token needed.

### Publishing pyyorkshirewater

```bash
cd ../pyyorkshirewater
git tag -a v0.4.0 -m "v0.4.0: first PyPI release"
git push origin v0.4.0
```

Watch the publish workflow on the
[Actions tab](https://github.com/dan-simms1/pyyorkshirewater/actions)
and verify the package lands at
<https://pypi.org/p/pyyorkshirewater/0.4.0>.

## HACS submission

Once pyyorkshirewater is on PyPI:

1. **Verify the integration's CI passes.** Push a fresh commit (or
   manually re-run the validate workflow). Both `hassfest` and
   `HACS validation` jobs must be green on the latest tagged
   release commit.
2. **Confirm the GitHub release exists for the current `manifest.json`
   version.** A tag without a release is not enough - HACS reads from
   GitHub Releases.
3. **Fork [`hacs/default`](https://github.com/hacs/default)** to your
   account.
4. **Edit `integration` (the JSON file in the root):** add
   `dan-simms1/ha-yorkshire-water` to the sorted list. Maintain
   alphabetical order.
5. **Open a PR** with the HACS-supplied checklist template. Fill the
   checklist carefully - the bot auto-closes PRs that do not.
6. **Required links** in the PR body:
   - Latest GitHub release.
   - A successful HACS action run on that release.
   - A successful hassfest action run on that release.
7. **Wait.** Do not comment, do not request reviewers, do not open a
   second PR. The HACS team works through the queue manually.

## Lessons from previous submissions

- HACS validates against GitHub Releases, not just tags. Make sure
  the release object exists and is tagged at the correct commit.
- Hassfest must pass too. HACS validation alone is not enough.
- Fix any validation issues, push them, and **release them** before
  submitting - reviewers inspect the released package, not main.
- Once queued, do not open duplicate PRs.
