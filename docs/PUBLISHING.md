# Publishing checklist

The extension is the `source/` folder. Everything else in the repository is documentation and tooling and is
never packaged. Reference: [How to Create Extensions](https://docs.blender.org/manual/en/latest/advanced/extensions/getting_started.html).

## 1. Before you build
- [ ] `version` in `source/blender_manifest.toml` matches `LITE_VERSION` in `source/__init__.py`.
- [ ] `website` in the manifest points at your real repository.
- [ ] `DEBUG_PIN_GAP` is `False`.
- [ ] `CHANGELOG.md` has an entry for the version.
- [ ] Tested in Blender on every version you claim to support (at least 4.2 LTS and the newest release).
- [ ] `source/LICENSE` is identical to the root `LICENSE`.
- [ ] `source/README.md` is updated whenever the root `README.md` changes. It is the same text without the
      repository-only sections (project structure, development, contributing) and without repo-relative links.

## 2. Validate and build with Blender

From the repository root:

```
blender --command extension validate source
blender --command extension build --source-dir source --output-dir .
```

The result is `pullsnpins_clothlite-<version>.zip` with `__init__.py`, `blender_manifest.toml`, `LICENSE` and
`README.md` at the zip root. Test it with **Edit > Preferences > Get Extensions > Install from Disk** before uploading.

Manifest rules to keep in mind:
- Required: `schema_version`, `id`, `version` (semantic versioning), `name`, `tagline`, `maintainer`, `type`,
  `blender_version_min` (4.2.0 or newer) and `license`.
- The `tagline` is at most 64 characters and does not end with punctuation.
- No value may be empty. Leave out optional keys you do not use.
- If the add-on ever reads or writes files, uses the network or the clipboard, add a `[permissions]` entry with a
  short reason (up to 64 characters, no end punctuation). ClothLite needs none today.

## 3. GitHub release
```
git tag v1.0.15
git push origin v1.0.15
```
The workflow in `.github/workflows/release.yml` checks that the tag matches the manifest version, zips the
`source/` folder and attaches the zip to the release.

## 4. extensions.blender.org
1. Sign in at https://extensions.blender.org (Blender ID) and choose **Submit your first Extension**.
2. Upload the built zip. The manifest is validated on upload.
3. Fill in the listing and add a featured image or screenshots (a short GIF of pulling a cloth works well).
4. The extension is held for review and published once the moderators approve it. Later versions are uploaded
   the same way with a higher `version`.

To automate updates, see the CI/CD documentation linked from the manual page above.

Notes for the review: the add-on has no permissions, no network access and no bundled third-party code. The
panel footer text mentions the full EverCloth edition; reviewers may ask you to keep promotion minimal.
