# Osiris Repository

A personal Kodi addon repository that **mirrors third-party addons from
multiple external sources into one place**, so any Kodi install only needs
one repository source added to get everything you use.

- Hosted directly via `raw.githubusercontent.com` from the `main` branch —
  no GitHub Pages, no `gh-pages` branch. The `zips/` folder is committed to
  git directly and *is* the hosted content.
- Kept up to date automatically by GitHub Actions
  (`.github/workflows/build.yml`), which rebuilds the repo on every push to
  `sources.yaml`/`tools/`/`repository.osiris/`, on a daily schedule (to
  catch upstream addon updates independent of local edits), and on demand.

> Every raw URL in this repo targets GitHub repo `enkhee-Osiris/kodi` on
> branch `main`. The exact substring `enkhee-Osiris/kodi/main` appears in
> exactly two files: `repository.osiris/addon.xml` and this README.

## How it's built

The repo is **tiered by Kodi major version** (`sources.yaml` has a top-level
`tiers:` list, e.g. `omega` for Kodi 20.9.1–21.9.0 and `piers` for
21.9.1+). Each tier gets its own `<dir minversion=... maxversion=...>`
block in `repository.osiris/addon.xml` — Kodi picks whichever block covers
its own running version. This matters because an addon requiring a newer
Kodi API shouldn't get force-installed on an older client just because it's
listed in the same repo; it's the same approach third-party multi-version
Kodi repos like jurialmunkey's use, since (unlike Kodi's own built-in
official repo, which ships a version-matched addon.xml baked into each Kodi
release) a manually-installed repo zip has to work across whatever Kodi
version it ends up installed on.

`tools/build_repo.py` reads `sources.yaml` and (re)builds:

- `zips/<addon-id>/<addon-id>-<version>.zip` — a **shared** store, not
  duplicated per tier. If two tiers resolve an addon to the identical
  version (the common case when it isn't split per Kodi version upstream),
  only one copy is kept on disk and it's only fetched once per run.
- `zips/tiers/<tier-name>/addons.xml` — always regenerated per tier by
  reading the `addon.xml` back out of whichever version that tier actually
  resolved, so it can never drift from what's actually being served.
- `zips/tiers/<tier-name>/addons.xml.md5` — MD5 hex digest of that tier's
  `addons.xml` bytes.

`repository.osiris` itself is untiered (one build serves every Kodi
version) and always rebuilt from `repository.osiris/` regardless of
`sources.yaml`, so the repo can never accidentally stop shipping its own
installable, self-updating zip.

A single dead upstream link only fails *that* addon in *that* tier (logged
as a `::warning::` + a row in the Actions job summary, falling back to
whatever version that tier last successfully published) — it never blocks
publishing everything else that resolved fine.

## Adding a mirrored addon

1. Edit `sources.yaml` — add an entry under each tier that should carry it,
   picking one of the three documented shapes (`kodi_repo`, `github`, or
   `zip_url`). Most addons aren't split per Kodi version upstream, so the
   same source config is usually just copied into every tier.
2. Test locally first (recommended):
   ```bash
   cd /Users/enkherdene/Work/personal/kodi
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r tools/requirements.txt
   python tools/build_repo.py
   ```
   Inspect the printed summary table and `zips/<id>/` before committing.
3. Commit and push `sources.yaml` — CI builds it automatically.
4. Check the Actions run's job summary to confirm the addon shows `added`.
5. **Removing an addon:** delete its entry from every tier that lists it.
   The next build automatically prunes it from the shared `zips/<id>/`
   store — but only once *no* tier references it anymore; if one tier
   still needs a version, it's kept.
6. **Adding a new Kodi-version tier** (rare, roughly once per Kodi major
   release): add a new tier block to `sources.yaml`, cap the previous
   tier's `maxversion` if needed, AND add a matching `<dir>` block to
   `repository.osiris/addon.xml` — bump its `version` too, so
   already-installed clients notice and refresh to see the new tier.

## First-time setup (do this once)

1. Local scaffold (already done if you're reading this from the repo):
   ```bash
   cd /Users/enkherdene/Work/personal/kodi
   git init
   git branch -M main
   ```

2. Create the GitHub repo. The `gh` CLI is not currently installed in this
   environment, so use the web UI:
   - Go to https://github.com/new
   - Owner: `enkhee-Osiris`, Repository name: `kodi`
   - **Public** (required — see below)
   - Do **NOT** check "Add a README", ".gitignore", or "license" (this repo
     already has its own files; auto-init would create a conflicting
     initial commit)
   - Click **Create repository**

   (If you'd rather use the CLI: `brew install gh && gh auth login`, then
   `gh repo create enkhee-Osiris/kodi --public --source=. --remote=origin`.)

3. Connect the remote and push:
   ```bash
   git remote add origin git@github.com:enkhee-Osiris/kodi.git
   git add -A
   git commit -m "Initial commit: repository scaffold"
   git push -u origin main
   ```

4. The push already triggers the first build (paths match). Watch the
   **Actions** tab — within about a minute you should see a follow-up
   commit from `github-actions[bot]` adding `zips/`. If it doesn't fire,
   trigger it manually: Actions → Build repository → Run workflow.

## Repo visibility

This repo **must stay public**. `raw.githubusercontent.com` can only serve
private-repo content with an auth token, and Kodi has no way to supply
one — a private repo would silently break every install.

## Installing on a Kodi device (bootstrap)

`raw.githubusercontent.com` does **not** support directory listing, so Kodi
can't browse it as a folder-based file source the way it browses SMB/FTP.
Two approaches:

- **Most reliable (any Kodi version/platform):** open this URL in a
  browser on any device and download the zip —
  `https://raw.githubusercontent.com/enkhee-Osiris/kodi/main/zips/repository.osiris/repository.osiris-1.1.0.zip`
  (check `zips/repository.osiris/` for the current version if this repo has
  moved on since this was written) — copy it somewhere Kodi can browse (its
  own Downloads folder, a USB
  stick, an SMB share). In Kodi: **Settings → System → Add-ons → enable
  "Unknown sources"** (if not already) **→ Add-ons → Install from zip
  file →** browse to that file **→** select it.

- **Often works, worth trying first:** **Settings → Media → File
  manager → Add source**, paste the *exact zip file URL itself* (not the
  folder) as the path, name it e.g. "Osiris". Then **Install from zip
  file** — since it points at a file rather than a folder, it may appear
  as a directly-selectable item. Falls back to the method above if Kodi
  tries to browse it as a folder.

Once `repository.osiris` is installed: **Add-ons → Install from
repository → Osiris Repository →** install any mirrored addon. From then
on, Kodi handles all future updates — both mirrored addons and
`repository.osiris` itself — automatically.

## Verification

```bash
# Well-formed XML + md5 sanity check, per tier
for tier in omega piers; do
  python3 -c "import xml.dom.minidom as m; m.parse('zips/tiers/$tier/addons.xml')"
  python3 -c "import hashlib; print(hashlib.md5(open('zips/tiers/$tier/addons.xml','rb').read()).hexdigest())"
  cat zips/tiers/$tier/addons.xml.md5   # must match exactly
done

# Zip shape check (single top-level folder == the addon id, containing addon.xml)
unzip -l zips/repository.osiris/repository.osiris-*.zip
```

After pushing, confirm the hosted files actually resolve (repeat per tier):
```bash
curl -I https://raw.githubusercontent.com/enkhee-Osiris/kodi/main/zips/tiers/piers/addons.xml
curl -I https://raw.githubusercontent.com/enkhee-Osiris/kodi/main/zips/tiers/piers/addons.xml.md5
curl -I https://raw.githubusercontent.com/enkhee-Osiris/kodi/main/zips/repository.osiris/repository.osiris-1.1.0.zip
```
All three should return `200 OK`.
