# Slicer Extension Checklist Status

This file tracks compliance for the Slicer extension checklist.

## Tier 1

- [x] Reasonable extension name (`SlicerMotionScoreHRpQCT` repository, module `MotionScoreHRpQCT`)
- [x] Repository naming follows `Slicer+ExtensionName`
- [ ] GitHub topic `3d-slicer-extension` is set on repository (GitHub web setting)
- [x] Extension description is 1-2 sentences and non-expert friendly
- [x] Known patents statement included (`No known related patents`)
- [x] `LICENSE.txt` present at repository root
- [x] License name mentioned on homepage (`README.md`)
- [x] `scmurl` / `scmrevision` set in catalog JSON (`main` branch tracked)
- [x] Icon URL points to raw icon file
- [x] Screenshot URL points to raw image file and includes at least one screenshot
- [x] Catalog JSON is consistent with top-level `CMakeLists.txt` (`build_dependencies` and `EXTENSION_DEPENDS` empty)
- [x] Homepage contains extension name
- [x] Homepage contains short description
- [x] Homepage contains informative image
- [x] Homepage describes contained module(s)
- [x] Homepage contains publication reference
- [ ] Unused GitHub features hidden (Wiki/Projects/Discussions/Releases/Packages) if unused (GitHub web setting)
- [x] No automatic data transmission without explicit user action

## Tier 3 (Reachability)

- [x] Core documentation and usage tutorial provided in `README.md`
- [ ] Sample tutorial data registration in Sample Data module (future improvement)
- [x] GUI/logic separation maintained in scripted module structure
- [x] Automated CI packaging checks exist in `.github/workflows/ci.yml`
- [ ] Validate package build artifacts on all platforms in extension index CI environment
- [ ] Maintainer responsiveness expectations are process commitments (issues/PRs/forum mentions)
- [x] Permissive license used (MIT)

## Manual GitHub Actions Required

1. Add repository topic `3d-slicer-extension`.
2. Hide unused repository features in GitHub settings.
3. Keep issue/PR/forum response expectations documented in CONTRIBUTING or issue templates if desired.
