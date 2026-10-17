# Anki template runtime

This package runs Anki card JavaScript in a restricted QuickJS environment and renders its browser preview. It owns typed-answer comparison, speech/audio bridges, state isolation, and the patched engine build. It does not read a Rails checkout.

From the shirabe-anki repository root:

```sh
npm ci
npm run build --workspace @shirabe-org/anki-runtime
npm run test:runtime
npx playwright install chromium firefox webkit
npm run test:runtime:browser
npm run package:runtime
```

The package exports the runtime, browser client, and snapshot renderer. Its `dist` directory also contains the worker, preview script, raw browser bridges, frame source, CSP hash manifest, and engine licenses. Rails installs the versioned package and copies these artifacts during asset preparation.

`STUDY_BROWSER` selects chromium, firefox, or webkit. Browser checks are headless and mute audio. `STUDY_RUNTIME_BUILD_DIR` selects a temporary output directory for verification. Rebuild the patched engine with `npm run build:engine --workspace @shirabe-org/anki-runtime`; this uses the pinned source checksum and Docker compiler image.

Runtime releases use the tag `anki-runtime-vVERSION` and a packed npm archive attached to the GitHub release. The Python add-on has its own archive and does not bundle this runtime.

To prepare a release, dispatch `runtime-release.yml` with the package version. The workflow builds, tests, installs the archive in a fresh consumer, and uploads the archive with `runtime-release.json`. Download that workflow artifact and publish the archive with `gh release create anki-runtime-vVERSION --target SOURCE_REVISION ARCHIVE`. Use the source revision recorded in the metadata and verify its SHA-256 before uploading. Release publishing uses the operator account because the repository's Actions token cannot create releases.
