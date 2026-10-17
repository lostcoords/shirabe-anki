# Shirabe Anki

This repository owns the Anki add-on under `addon/`, the portable template runtime under `packages/template-runtime/`, client tests, and packaging. Rails lives in `lostcoords/shirabe`; shared API and popup packages live in `lostcoords/shirabe-sdk`.

Use jj when `.jj/` exists. Preserve other sessions' edits.

Run `npm ci`, `npm run build`, `npm run check`, and `npm test`. Run `npm run package` to create `artifacts/shirabe-anki.ankiaddon`. The shared popup comes from the pinned npm SDK release.

Run native tests with `QT_QPA_PLATFORM=offscreen uv run --no-project --python 3.13 --with-requirements requirements-test.txt python tests/run.py`. Cross-client probes require a Rails checkout selected by `SHIRABE_PATH`, defaulting to `../shirabe`. Their scratch files use that checkout's `tmp` when the variable is explicit.

Use headless browser and offscreen Qt checks. Do not activate native apps, control a real study profile, change the clipboard, or play test audio without permission.

Run `npm run test:runtime` and `npm run test:runtime:browser` for runtime changes. Releases of `@shirabe-org/anki-runtime` include all built artifacts and do not read Rails source.
