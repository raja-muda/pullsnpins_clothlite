# Changelog

All notable changes to PULLS'NPiNS ClothLite. Versions follow `blender_manifest.toml`.

## 1.0.15
- Release preparation for the Blender Extensions platform and GitHub.
- Debug console output (`DEBUG_PIN_GAP`) is now off by default.
- Removed an unused helper function.
- Repository now follows the usual extension layout: the add-on lives in `source/`, everything else
  (README, docs, issue templates, release workflow) sits around it.
- Manifest: added `website` and `copyright`, removed the upper Blender version limit so the add-on stays
  installable on newer Blender releases.
- Manifest tagline no longer says "Free lite version".
- Added a README inside the add-on package (`source/README.md`) as well as the repository README.
- Added README, LICENSE, changelog, issue templates and a release workflow.

## 1.0.14
- Pins are carried by a Pin Shape Key plus a pin vertex group (no hooks or Empties).
  Setups made by 1.0.13 with `PNP_LiteHook_*` hooks are converted automatically.

_Earlier history was not recorded._
