# Publishing a release

This page describes how an OpenSpecLib release is built, checked, and published to GitHub Releases, Zenodo and Hugging Face. It is written for maintainers.

A release has one version number, used for the `openspeclib` Python package, the git tag and the data (for example `0.2.0`, tag `v0.2.0`). The [specification](spec/format-v1.md) has its own version and is not bumped by a release unless an RFC changed it.

## What needs credentials

Building and checking a release needs no credentials and can run anywhere. Publishing the data does, and **every data-publishing step is manual**: a maintainer starts it and supplies the credentials. The one automatic deployment is the viewer: `.github/workflows/deploy-viewer.yml` runs on every push to `main` that changes `viewer/**` (or the workflow itself), and downloads the release named by `OPENSPECLIB_VERSION` in `viewer/src/constants/urls.ts`.

| Step | Credential | Who |
|---|---|---|
| Build, validate, checksum | none | anyone |
| Tag and GitHub Release | write access to `null-jones/openspeclib` (to run the release workflow) | maintainer |
| Zenodo archive and DOI | a Zenodo account linked to the GitHub repository (one-time setup) | maintainer |
| Hugging Face mirror | a Hugging Face write token for the dataset repo, in `HF_TOKEN` | maintainer |

Never commit a token, and never put one in a workflow file or a script argument. `scripts/publish_huggingface.py` reads its token only from the `HF_TOKEN` environment variable.

## 1. Prepare

1. Make sure `main` passes CI (tests, black, ruff, mypy).
2. Set the version in `pyproject.toml` and `CITATION.cff` (`version`, and `date-released` on the day of release).
3. In `CHANGELOG.md`, replace `Unreleased` in the version's heading with the date (for example `## [0.2.0] - 2026-11-02`), change its compare link at the bottom from `...HEAD` to `...v0.2.0`, and add a fresh `## [Unreleased]` section with a `[Unreleased]: …/compare/v0.2.0...HEAD` link.
4. Update the counts table in `README.md` (marked `<!-- counts: updated at release build -->`).
5. Merge these changes to `main`. Do not change `OPENSPECLIB_VERSION` in `viewer/src/constants/urls.ts` to the new version yet: a merge that touches `viewer/**` triggers the viewer deployment, which fails while the release does not exist. Change it after the GitHub Release is published (step 6).

## 2. Build

The **Build and Release Spectral Library** workflow (`.github/workflows/build-release.yml`) is started by hand: **Actions → Build and Release Spectral Library → Run workflow**, with the version (for example `0.2.0`). It:

1. runs the tests;
2. downloads and ingests each source;
3. runs `openspeclib combine`, which writes one Format v1 collection file per collection, the index file `openspeclib-index.parquet`, `id-migration.csv` and `checksums.txt`;
4. runs `openspeclib validate --profile openspeclib`, which checks the conformance rules and the OpenSpecLib quality profile. Any error stops the build. A spectrum is exempted only by an allowlist entry with a written reason. The report is saved as `validation-report.json`;
5. adds `validation-report.json` to `checksums.txt` (the SHA-256 of every release file, in `sha256sum` format) and verifies every checksum.

To build locally instead, run the same commands (see the README) and put the release files flat in one directory, for example `build/release/`. Check the checksums before publishing anything:

```bash
cd build/release && sha256sum --check checksums.txt
```

## 3. GitHub Release

The workflow creates the git tag `v<version>` and a GitHub Release with the release files as assets and generated release notes. Check the release page:

- every collection file, the index file, `id-migration.csv`, `validation-report.json` and `checksums.txt` are attached;
- the notes give the right counts and link to the specification at the tag.

Asset URLs have the form `https://github.com/null-jones/openspeclib/releases/download/v<version>/<file>`. Do not replace assets on a published release; publish a new version instead, so a URL always returns the same bytes.

## 4. Zenodo (DOI)

Zenodo archives each GitHub Release and gives it a DOI through Zenodo's GitHub integration.

**One-time setup (manual, maintainer):** sign in to Zenodo with GitHub, open *GitHub* in the Zenodo account settings, and switch on `null-jones/openspeclib`. From then on, every published GitHub Release triggers an archive. Zenodo reads the record's metadata (title, creators, licence, keywords) from `.zenodo.json` in the repository root.

**Each release:**

1. After the GitHub Release is published, check on Zenodo that a new version appeared under the OpenSpecLib record, with the right version and metadata.
2. Note the DOIs. Zenodo issues a *concept DOI* (always resolves to the latest version, the same for every release) and a *version DOI* (this release only).
3. After the first release, add the concept DOI to `CITATION.cff` (`identifiers`, or `doi`) and to the README, in a follow-up pull request.

What Zenodo archives: the integration stores a snapshot of the **repository source** at the tag (code, specification, schemas, documentation), under the MIT licence. It does not copy the data files attached to the GitHub Release. The data stays under each source's own licence and is distributed through GitHub Releases and Hugging Face. Uploading the data to Zenodo as well would be a separate, manual deposit; it would have to state that each collection keeps its source's licence (`license` per row and in each file's footer), and should only be done for collections whose licences allow it.

## 5. Hugging Face mirror

`scripts/publish_huggingface.py` mirrors a release directory to a Hugging Face dataset repository. Each release goes into its own folder, `v<version>/`, in one commit, and that commit is tagged `v<version>`. The script refuses to upload if the folder or the tag already exists, so a published URL never changes. It also writes the repo's `README.md` (the dataset card) from the collection footers or the index file: collections, counts, licences, citations, file checksums and URLs.

**One-time setup (manual, maintainer):** create the dataset repository on Hugging Face. The script does not create repositories.

**Each release:**

1. Download the release assets from GitHub into an empty directory (or use your local build directory), and verify them with `sha256sum --check checksums.txt`.
2. Dry run. Nothing is uploaded and no token is needed. The script verifies `checksums.txt` and prints every file it would upload, with size and SHA-256, and the full dataset card:

   ```bash
   python scripts/publish_huggingface.py build/release --repo-id OWNER/NAME
   ```

   The version comes from the index file's footer (`release`); pass `--version 0.2.0` if the directory has no index file. Read the output: the file list and card are exactly what will be uploaded.
3. Upload (manual, needs a write token):

   ```bash
   read -rs HF_TOKEN && export HF_TOKEN   # paste a write token; it is not echoed or saved
   python scripts/publish_huggingface.py build/release --repo-id OWNER/NAME --yes
   ```

4. Check the dataset page, and that a file opens at its immutable URL:

   ```
   https://huggingface.co/datasets/OWNER/NAME/resolve/v<version>/v<version>/<file>
   ```

   The first `v<version>` is the tag (the revision); the second is the folder. Links using `resolve/main/v<version>/<file>` also keep working, because releases never overwrite each other's folders, but only the tagged form is pinned to one commit.

Run `python scripts/publish_huggingface.py --help` for all options.

The hand-written per-source cards that described the older standalone re-uploads (`huggingface_cards/`) have been removed; the script's generated card replaces them.

## 6. After the release

- Point the viewer at the new release. Publishing a release does not redeploy the viewer. Set `OPENSPECLIB_VERSION` in `viewer/src/constants/urls.ts` to the new version and merge it (the push to `main` deploys the viewer), or, if it is already set, run **Actions → Deploy Viewer to GitHub Pages → Run workflow** by hand. Then check that the viewer loads the new release.
- Announce the release with links to the GitHub Release, the Zenodo DOI and the Hugging Face dataset.

## If something goes wrong

- **A data error in a published release:** do not replace assets or overwrite the Hugging Face folder. Fix it, publish a new patch release, and say in the changelog what was wrong and which versions are affected.
- **The Hugging Face upload failed:** the script uploads everything in one commit, so a failed upload leaves nothing behind. Run it again. If the commit succeeded but creating the tag failed, the script will refuse a re-run (the folder exists); create the tag `v<version>` on that commit by hand in the Hugging Face web interface or with `huggingface_hub`.
- **Zenodo did not archive the release:** check that the integration is still switched on for the repository, and look at the webhook deliveries in the GitHub repository settings. A missed release can be archived by a manual upload to the same Zenodo record as a new version.
