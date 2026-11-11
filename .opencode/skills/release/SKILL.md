---
name: release
description: Prepare and publish a new release. Use when the user asks to release, cut a release, or publish a new version.
---

## Purpose

Release a new version of battery-optimizer with the release script and the CI pipeline.

## When to use

- The user asks to release, cut a release, publish, or tag a new version.

## How a release works

1. `just release` (`.ci/release.sh`) bumps `[project] version` in `pyproject.toml` with
   `uv version`, refreshes `uv.lock`, prepends the new section to `CHANGELOG.md` with git-cliff,
   commits `release: Version X.Y.Z` (signed, with `Signed-off-by`), and pushes `release/vX.Y.Z`.
2. Open a PR to `main` with the `gh pr create` command the script prints, and merge it.
3. On merge, CI does the rest automatically. **Never create tags or releases by hand.**
   - `auto-tag.yml` reads the newest version heading in `CHANGELOG.md` and pushes the signed tag
     `vX.Y.Z` if it does not exist yet.
   - `images.yml` runs on the tag: pushes `ghcr.io/pando85/battery-optimizer:X.Y.Z` (and `:X.Y`),
     attests provenance, scans the image, runs `uv build` and creates the GitHub release with the
     changelog section and the `dist/*` files attached.

## Prerequisites (the script enforces them)

- Clean working tree, on `main`, identical to `origin/main` (not ahead, not behind).
- Full clone with tags (`git fetch --unshallow` and `git fetch --tags` are done for you).
- `uv` and `just` installed (`UV="uvx uv@<version>" just release` to use another one) and signed commits
  configured in git.

## Version decision guide

Semantic Versioning. Review the commits since the last tag and take the highest bump:

```bash
git log "$(git describe --tags --abbrev=0)..HEAD" --oneline
```

| Bump | When |
|------|------|
| MAJOR | `!` after the type or a `BREAKING CHANGE:` footer; incompatible change to the CLI, the config format, the dataset/manifest schema or an environment variable |
| MINOR | `feat:` commits; new commands, options or datasets, backward compatible |
| PATCH | `fix:`, `perf:`, `docs:`, `refactor:`, `build(deps):` only |

While the project is `0.x`, breaking changes may bump MINOR; say so in the PR description.

## Config keys and deployed overrides

The homelab deploys a sparse overrides file (`BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES`, in
`apps/battery-optimizer/files/` of the homelab repo) over the `config/service.yaml` shipped in the image, and the
load fails on a key path the base no longer has. When a release renames or removes a `config/service.yaml` key
(`git diff "$(git describe --tags --abbrev=0)..HEAD" -- config/service.yaml`), check it against the deployed
overrides and say so in the release notes and the PR description, so the overrides are updated in the same
homelab change that bumps the image tag. Verify before the rollout with
`docker run --rm -v <overrides>:/o.yaml:ro ghcr.io/pando85/battery-optimizer:<tag> service-config --overrides /o.yaml`.

## Steps

```bash
git checkout main && git pull --ff-only
just release                 # prompts for the version; or: just release 0.2.0
# run the gh pr create command it prints, get the PR merged
gh run list --limit 5        # watch auto-tag, then the Images run on the new tag
```

The first release (`0.1.0`) uses the version already in `pyproject.toml`: pass `0.1.0` and the
script only regenerates the changelog.

## What NOT to do

| Mistake | Why it is wrong | Fix |
|---------|-----------------|-----|
| Editing `CHANGELOG.md` by hand | git-cliff generates it from conventional commits | Fix the commit message convention, run `just update-changelog` |
| Editing the version in `pyproject.toml` by hand | `uv.lock` and the changelog would drift | Use `just release` |
| Creating tags manually | `auto-tag.yml` creates the signed tag | Merge the release PR |
| Releasing from a feature branch | The changelog needs the merged `main` commit ids | `git checkout main` first |
| Passing `--no-gpg-sign` | Release commits and tags must be signed | Fix the local signing setup |

## Troubleshooting

- **"commits ahead of origin/main"**: push or merge them first.
- **Shallow clone**: `git fetch --unshallow origin && git fetch --tags origin`.
- **Auto-tag did not run**: `CHANGELOG.md` must have a `## [X.Y.Z]` heading as its first version
  entry, the tag must not exist yet (`git ls-remote --tags origin`), and the `PAT` and
  `GPG_PRIVATE_KEY` repository secrets must be set. Without a version heading the job is a green
  no-op by design.
- **Tag exists but no images**: check the `Images` run for that tag; re-run it from the Actions tab.

## Key files

| File | Role |
|------|------|
| `pyproject.toml` | `[project] version`: single source of truth |
| `cliff.toml` | git-cliff changelog configuration |
| `CHANGELOG.md` | Generated changelog; auto-tag reads the version from it |
| `.ci/release.sh` | Release branch/commit script behind `just release` |
| `.github/workflows/auto-tag.yml` | Creates the signed tag on push to `main` |
| `.github/workflows/images.yml` | Builds/pushes the image and creates the GitHub release on tags |

## Checklist

- [ ] On `main`, clean, up to date with `origin/main`
- [ ] Version bump decided from the commits since the last tag
- [ ] `just release` run, `release/vX.Y.Z` pushed
- [ ] PR opened and merged
- [ ] `auto-tag` and `Images` runs green, GitHub release present
