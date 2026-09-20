# Real-client harness reference

These scripts use installed Anki, not a collection mock. Each desktop run creates
a new directory under ignored `tmp/anki-phase0-*`. They never open the default
Anki profile. Keep the directory printed by a run to retain its evidence.

## Entry points

### Study sync verification

Study sync targets Anki 26.08.1. These background checks cover package parity,
native API compatibility, browser rendering and interrupted synchronization:

```sh
QT_QPA_PLATFORM=offscreen uv run --no-project --with aqt==26.8.1 --python 3.13 python anki-addon/tests/platform_check.py
SHIRABE_RICH_BUNDLE="$PWD/anki-addon/src/web/shirabe-card.js" QT_QPA_PLATFORM=offscreen uv run --no-project --with aqt==26.8.1 --python 3.13 python anki-addon/tests/run.py
ANKI_ADDON_ROUNDTRIP=1 ANKI_SYNC_CRASH=1 bin/test integration test/integration/anki_addon_roundtrip_test.rb
env -u SYSTEM_TESTS_BROWSER bin/test system test/system/study_review_layout_test.rb test/system/study_review_buffer_test.rb test/system/card_browser_layout_test.rb test/system/study_script_worker_test.rb
bin/package-check
bin/lint
bin/brief check
```

The Rails round-trip suite skips live AnkiWeb, personal-profile and performance
probes unless their explicit opt-ins are set. A passing default run does not
claim those checks. Live account probes require separate authorization and keep
preservation reports beside their disposable collections. Never use a full
upload or download to bypass a failed preservation or sync-state guard.

### Dictionary and native reviewer checks

```sh
python3 anki-addon/tests/platform_check.py --portable --prepared
python3 anki-addon/tests/desktop_check.py --allow-foreground
python3 anki-addon/tests/desktop_check.py --allow-foreground --keep-open
python3 anki-addon/tests/desktop_check.py --allow-foreground --prepare-offline --keep-open
SHIRABE_SAVED_AUDIO_MANIFEST=tmp/anki-complete-current-v10/prepared-mp3/ankiweb-prepared-audio.json \
  python3 anki-addon/tests/desktop_saved_audio_check.py --allow-foreground
python3 anki-addon/tests/refresh_fixture.py tmp/anki-phase0-EXISTING
python3 anki-addon/tests/oauth_fixture.py
python3 anki-addon/tests/isolate_apkg.py tmp/anki-phase0-EXISTING/native-prepared.apkg
```

The scripts re-execute with installed Anki's Python when needed. Alternatively,
invoke them with the Python environment containing the Anki version under test.
Build the SDK before running; packaging uses the current `src/web` files.

The current installable handoff is indexed in
`tmp/anki-complete-current-v10/README.md`. The older relations checkpoint
described below remains as historical evidence.

The older relations checkpoint is
`tmp/anki-current-ready.8lrRFU/shirabe-phase0.ankiaddon`, with its source manifest
and evidence index beside it. Package SHA-256 starts `9e505295`, Python
`offline.py` is `b6ab804d`, and the SDK is `864b02b2`. All 24 package members
match their source and manifest. Relative to frozen `5d467a3e`, only the SDK
changed: ten kanji numerals were added to its publisher-group label regex.
The compiled-artifact comparison renders two grammar groups instead of three
for `一（一）`, `一（二）`, `二（一）`, without changing labels or glosses.
The 43 popup source tests and TypeScript check pass. The exact new artifact
passes the 19-test rich-content/runtime suite on both Anki versions. The minimum
environment uses a current-installation fallback for other Python dependencies;
the index distinguishes direct native tests from older subprocess tests.
The older `5d467a3e` package passed 431 tests without skips in each environment,
plus 189 SDK unit and 123 integration tests. Those full runs were not repeated
or relabeled as new-bundle executions. Relations survive online lookup and
offline preparation, and nested links preserve the target reading. Partial
refreshes retain saved selected definitions within an unchanged scope; the
native guard tests cover front/back availability and unchanged schedules.
Actual-client and performance gates remain open. The previous
checkpoint's 39 backend checks and consent/template layout reports remain in
`tmp/anki-background-build.RfJiUS/`. Preparation needs the matching server
`/parse.stackFingerprint` response and `include: relations` support. The Desktop
add-on was not installed or deployed. The current APKG has since been imported
into the private Android emulator described below.

The `864b02b2` bundle's
`tmp/anki-current-ready.8lrRFU/ankiweb-current-864b/` APKG retains the original
four test notes and adds two real public examples to the synthetic cat definition. Anki 25.09.4 and
24.04 each export and reimport the prepared examples/audio; the frozen SDK reads
the reimported examples, source references and license without network access.
The exports have SHA-256 prefixes `a65b9bd3` and `09eb207f`, respectively;
their ten media files total 224,056 bytes. Native reimport verifies note fields,
IDs/GUIDs, card IDs, scheduling, the recording and prepared payload. Independent
audits verify every media hash and zero review history. Cross-version snapshots
differ only by a 13 ms timestamp and its hash-derived reference; native deck-ID
remapping is checked separately. Eight focused test executions pass, and all
15 tracked source/input hashes stay unchanged.
This single-token fixture stores 1,426 compressed bytes from 1,934 JSON bytes,
including 603 decoded bytes for examples and notices, plus 11,387 audio bytes
and a 347-byte field reference. It is not a
representative-deck size or throughput measurement. Nothing has been imported or
synced to the user's account. Current-bundle Android emulator playback events
and source gestures now pass; Desktop/AnkiWeb, audible playback and native
timings remain open. The new evidence does not replace physical-device checks.

The frozen package is not a claim about future source edits. See the fixture's
`README.md` and `source-after.json` for its boundary. Older `8a7e8e13` artifacts
remain in `tmp/anki-relations-ready.ReJIYo/` as historical evidence.

To rebuild this example fixture in a new output directory:

```sh
python3 anki-addon/tests/ankiweb_prepared_audio_probe.py \
  tmp/anki-background-verified.I6F2Og/ankiweb/ankiweb-prepared-audio.json \
  --bundle-sha256 864b02b29a98d7f5a295a6567275215f66ad7f944e6d71c84f0d27730792ed62 \
  --recording-sha256 429847003510fe7eac3c569ea31138a2dbc61cdbe473b3061113e4cf8083d201 \
  --examples anki-addon/tests/fixtures/tatoeba_cat_examples.json
```

The example fixture records the public Tatoeba API URLs, unchanged text, direct
translation IDs, owner credits and license. It does not use a Shirabe credential
or claim to be a captured production Shirabe response. The native serializer's
bounded-page regression is exercised by `test_offline_examples.py` and the real
Rails-to-Python-to-SDK check in
`test/controllers/api/v1/offline_examples_contract_test.rb`.

Coordinate foreground access before running a native GUI check. A separate
AeroSpace workspace does not isolate keyboard focus. The Desktop runners refuse
to start without `--allow-foreground`; their refusal tests run without windows.
AnkiWeb checkers do not call `bringToFront()`. Select the reserved Brave window
on workspace 10 before a trusted-input run: the attached browser drops synthetic
mouse and keyboard events while its tab is in the background.

The smaller Qt probes accept explicit `QT_QPA_PLATFORM=offscreen`; their reports
identify that platform and do not establish native-client rendering. Regular Qt
widget tests skip before constructing an application unless offscreen mode is
explicitly selected. `test_foreground_gate.py` verifies refusal before runtime,
collection, server or window setup, including subprocess probes. The safeguard
audit passed nine system-Python tests and 32 windowless checks on each supported
Anki version; commands and hashes are in
`tmp/anki-mp3-handoff.u7rec2/gui-entrypoint-audit.json`.

On the development Mac, workspace 10 (`Cmd+0`) holds the isolated Phase0 Anki
window and a dedicated Brave test window. Regular Anki and Brave windows stay on
their own workspaces. Read-only AnkiWeb inspection works while its tab is hidden,
but trusted mouse and keyboard input does not reach a background tab. Foregrounding
the reserved window made the 20-check audio harness pass. Restore the
user's previous workspace after a coordinated foreground run.
`tmp/anki-agent-workspace-20260913.json` records the original window IDs,
ownership and observations.

| Artifact                   | Contents                                                                                                         |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `manifest.json`            | Note/card IDs, fields, asset filenames, installed Anki version                                                   |
| `shirabe-phase0.apkg`      | Basic, reverse, two clozes, ruby/inline, custom layout, long text, image/audio, optional prepared snapshot       |
| `source/collection.anki2`  | Isolated source collection                                                                                       |
| `rendered/`                | Actual Anki question/answer HTML                                                                                 |
| `platform-report.json`     | Real import/media roundtrip, literal search, stale writes, full card rows, undo                                  |
| `desktop-report.json`      | Actual reviewer/native-dialog checks and loopback API request bodies; failure details when a check stops the run |
| `desktop-*.png`            | Qt-rendered reviewer, field preview, Add window, Browser                                                         |
| `shirabe-phase0.ankiaddon` | Current production source and local assets, excluding runtime `meta.json` and bytecode                           |
| `package-manifest.json`    | Package and per-file SHA-256 hashes                                                                              |
| `profile/`                 | Disposable full Anki profile and installed test add-on                                                           |
| `native-prepared.apkg`     | Notes prepared through the real Browser consent and text-capture workflow, without a fixture-generated snapshot  |
| `native-roundtrip/`        | Real reimport of that native-prepared APKG                                                                       |

Desktop startup uses a unique `AnkiApp.KEY`, explicit `-b`/`-p`, no sync account,
disabled sync/update checks/analytics, and a fake loopback-only credential.
The dictionary HTTP responses are deterministic fixture data. The Qt reviewer,
JavaScript loader, Python bridge, field previews, Add window, collection writes,
Browser and undo are real. No OAuth account flow or dictionary-service quality
is established by this runner. `--keep-open` leaves this isolated app and its
fixture API running for inspection.

The prepared example deliberately renders `😀猫は猫` and `犬` in explicit block
elements. Exact collector output, including whitespace and UTF-16 offsets, is
part of the snapshot contract. The popup checks require three wrapped tokens
and visible `cat` content inside the real shadow DOM, not just an HTTP 200.
The reusable prepared fixture uses production `offline.snapshot_field()` and
`{{text:ShirabeLookup}}`; it has no static snapshot-reference attribute.
`--prepare-offline` additionally starts with Basic, ruby/inline and cloze notes
without a snapshot field and drives the native preparation workflow.
`isolate_apkg.py` imports an APKG into another disposable collection and moves
its cards to a uniquely named test deck for Android review. It leaves note
fields, templates and media unchanged and records the input package hash.

## Android control

The current private emulator is rooted at `tmp/anki-android-current.xMKeTD`;
latest final-bundle evidence and resumable state are in
`tmp/anki-complete-current-v8/README.md`. This remains the latest passing
Android evidence; the current v10 source-link candidate still needs its emulator
rerun. On AnkiDroid 2.24.1 and WebView
124.0.6367.219, exact SDK `1f65a44b` passes native offline tap, source-detail
swipe/tap, saved-MP3 lifecycle, signed-out controls, and cold restart. Its
stopped collection has zero reviews and unchanged card scheduling. The emulator
has no host audio, so this is not an audible-quality test. Earlier `864b02b2`,
`e4ec98be`, and `aa0f226b` evidence remains in older candidate directories.
The real visible WebView completes both 120 ms word transitions.

The current SDK also passes two 390-opening native Previewer runs over 33 novel
passages, using the full decoded-size selection including all multiline cases.
One warmup and two measured sweeps return to the same first card at each memory
checkpoint. Retained V8 growth was 60,108 and 16,604 bytes; all checkpoints kept
three documents, 465 nodes and 329 listeners. Effective garbage collection and
both real 120 ms transitions are asserted. The second run uses the reusable
runner below, whose source hash matches the report. Its visible snapshot
references are also checked against the fixture manifest.

| Measured opening       | First run median / p95 | Repeat run median / p95 |
| ---------------------- | ---------------------: | ----------------------: |
| First after navigation |        17.95 / 32.0 ms |          16.6 / 23.1 ms |
| Immediate repeat       |         19.0 / 31.9 ms |          17.4 / 41.8 ms |

These are shared-Mac/software-emulator timings, not app cold starts or phone
budgets. The runs made 193 and 192 local snapshot requests, respectively, with
one per attachment/navigation and none to a nonlocal origin. The first run
includes initial attachment; both include an explicit SDK-byte verification
request. Native application PSS returned from 153,139 to 145,575 KiB and from
154,301 to 144,889 KiB during the idle checkpoint. That metric covers the main
application process, not every separate WebView renderer.

`android_review_report.py` sets a 128 KiB retained-V8 ceiling for this exact
one-warmup/two-measured-sweep workload, more than twice the larger measured
growth, and refuses any retained DOM/listener increase. Its tests reject
incomplete sequences, wrong headwords, unfinished transitions, excess memory,
and nonlocal requests. This is a regression ceiling, not an assertion that an
arbitrarily long review session has a 128 KiB lifetime bound. Previewer preserves
its JavaScript context; its result does not close the legacy reviewer or
Desktop memory gates.

The fixture originally attached raw-corpus snapshots to rendered Anki fields.
Native capture of all 66 sides found three CRLF/LF mismatches. Re-preparing
exactly those three from rendered text fixed the fixture without changing the
SDK's exact-text guard. A real-WebEngine newline case now guards capture.
The corrected 606,160-byte APKG has 35 media files and 357,838 stored snapshot
bytes. All 33 passages remain in scope; none was removed to make the run pass.

After both runs, the native collection audit verifies all 37 notes, 38 cards,
IDs/GUIDs, fields and schedules, including the original cat fixture unchanged,
and zero reviews. All 46 media files (626,777 bytes) match their source hashes;
three obsolete fixture snapshots were retained, not deleted. Stock SQLite's
integrity check remains unavailable without Anki's `unicase` collation.
Evidence, failed-run boundaries, manifests and commands are indexed in
`tmp/anki-android-perf.7ibvco/README.md`.

With the owned emulator's corpus Previewer on its first card, use new output
paths for a rerun. Do not operate its UI while the runner is live:

```sh
node anki-addon/tests/android_review_performance.mjs \
  --android-root tmp/anki-android-current.xMKeTD --adb-port 5043 --serial emulator-5586 \
  --fixture tmp/anki-android-perf.7ibvco/rendered.hyozi5/fixture-verified.json \
  --output tmp/NEW-native-review.json
python3 anki-addon/tests/android_review_report.py tmp/NEW-native-review.json
```

The current AVD uses private adb 5043, serial `emulator-5586`, no window/audio,
and no real account. `device.py status` checks ownership; the current WebView
port must be rediscovered after app restart. UI-dump failures are refused by
`android_probe.py`, with regressions in `test_android_probe.py`.

The previously provisioned emulator used `tmp/phase0-android.qCQ2rX`. Its AVD is
`shirabe_phase0`, serial `emulator-5580`, with a separate adb server on port 5041.
The helper refuses other AVD names or non-emulator serials. Coordinate control
with whoever is reviewing the app before sending taps or restarting it.
The 2026-09-13 recheck found no running instance and no SDK tools at that path;
the commands below are the historical recipe, not a live environment.

```sh
python3 anki-addon/tests/android_probe.py --android-root tmp/phase0-android.qCQ2rX info
python3 anki-addon/tests/android_probe.py --android-root tmp/phase0-android.qCQ2rX ui
python3 anki-addon/tests/android_probe.py --android-root tmp/phase0-android.qCQ2rX screenshot tmp/phase0-new.png
python3 anki-addon/tests/android_probe.py --android-root tmp/phase0-android.qCQ2rX debug-port
node anki-addon/tests/android_cdp.mjs PORT '({online:navigator.onLine,origin:location.origin})'
```

`debug-port` forwards the current AnkiDroid WebView socket. Repeat after an app
restart; the old process socket is no longer valid. WebView debugging was enabled
through AnkiDroid's own developer settings. No root/device bypass is needed.

Restart this existing AVD with the following environment. The `user/avd` index
points at the AVD disk under `avd/`; these two directories are intentionally
different. Do not start a second copy while it is running.

```sh
phase0_android="$PWD/tmp/phase0-android.qCQ2rX"
ANDROID_USER_HOME="$phase0_android/user" \
ANDROID_AVD_HOME="$phase0_android/user/avd" \
ANDROID_HOME="$phase0_android/sdk" \
ANDROID_ADB_SERVER_PORT=5041 \
"$phase0_android/sdk/emulator/emulator" -avd shirabe_phase0 -port 5580 \
  -no-window -no-audio -no-snapshot -no-boot-anim -gpu swiftshader_indirect \
  -memory 2048 -partition-size 1536
```

Import through the app's Import > Deck package UI and the Android Downloads
picker after pushing the APKG into this emulator's `/sdcard/Download`.
Direct file-URI import failed; a fabricated document-provider URI lacked a
grant. The successful run used the picker. Refreshing a fixture preserves note
IDs so another import updates the same test notes.

## Measured clients, 2026-09-13 JST

| Component               | Executed version                       |
| ----------------------- | -------------------------------------- |
| Desktop Anki            | 25.09.4                                |
| Desktop Qt / PyQt       | 6.9.0 / 6.9.1                          |
| AnkiDroid               | 2.24.1, official arm64-v8a release APK |
| AnkiDroid backend       | 25.09.2 / 3890e12c                     |
| Android                 | 15, API 35, Google APIs arm64 emulator |
| Android WebView         | 124.0.6367.219                         |
| Android emulator binary | 37.1.11.0                              |
| Sibling source only     | Anki 26.05; AnkiDroid 2.25.0alpha4     |

This table describes native-app emulator validation, not physical-device validation.
AnkiWeb sync is covered by the separate real-account checkpoint below. AnkiMobile
and live-account Shirabe OAuth were not exercised. Browser handoff was
exercised with a deterministic local OAuth server and dummy credentials.

## Android evidence and interfaces

The Android directory retains raw `reviewer-*.json`, `native-search*.json/png`,
and `prepared-popup-proof.json` plus `prepared-popup-offline.png`. The latter
pair shows an actual definition popup after a cold app restart with airplane
mode enabled. `prepared-answer-cold.json` is the earlier failed whitespace
fixture and must not be treated as the successful popup result.

| Interface                  | Evidence or constraint                                                                                                                                                                                                                                                                                                                                                                                                       |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Relative JSON/JS media     | Real JSON 200 `application/json`, JS `text/javascript`, missing JSON 404, corrupt JSON parse failure; local loading survived airplane mode and cold restart                                                                                                                                                                                                                                                                  |
| Underscore media in APKG   | Real export omitted an unreferenced file. Snapshot filename as hidden element text alone did not retain it. Production `snapshot_field()` now writes a standard hidden image reference into the field; its real APKG roundtrip passes without the old static fixture attribute                                                                                                                                               |
| `AnkiDroidJS`              | Real lexical class; `typeof AnkiDroidJS` is `function`, while `window.AnkiDroidJS` is undefined                                                                                                                                                                                                                                                                                                                              |
| Native search              | `new AnkiDroidJS({version:"0.0.3",developer:"https://shirabe.org"}).ankiSearchCard('"猫"')` returned success and opened the real Browser                                                                                                                                                                                                                                                                                     |
| Search scope               | Current source uses `allDecks=false`. The installed release opens Browser with current-card context; cross-deck scope was not measured                                                                                                                                                                                                                                                                                       |
| Query with returned fields | `ankiSearchCardWithCallback` requires dangerous `QUERY_COLLECTION` permission, off by default. It calls the global `ankiSearchCard(jsonString)` callback; not enabled in the emulator run                                                                                                                                                                                                                                    |
| Template note authoring    | No create-note or update-note-fields method in the actual installed JavaScript prototype                                                                                                                                                                                                                                                                                                                                     |
| Cold storage               | Default localStorage probe vanished when the loopback origin port changed from 40563 to 43007. Same-reviewer write/read worked                                                                                                                                                                                                                                                                                               |
| Fixed-port setting         | Advanced > localStorage in Study Screen retained port 44067. Immediate force-stop lost the just-written marker (`fixed-port-before/after.json`); a second run waited, left the reviewer, and retained its marker across restart (`fixed-port-settled-before/after.json`, 26 seconds between observations). Stable origin does not guarantee an immediate disk flush. Enabled mode can fall back to a random port if occupied |
| Browser handoff            | Default-off Advanced > Allow cards to launch other apps blocked the actual sign-in link with a security snackbar. After enabling it in the fixture emulator, the link opened Chrome. This is separate from fixed-port storage and dangerous collection-query permission                                                                                                                                                      |
| Dummy OAuth and restart    | Real reviewer JS requested READ_ACCOUNT/READ_DICTIONARY/READ_SRS, Chrome submitted approval, token polling completed, and the popup showed `cat`. After leaving the reviewer and a settled cold restart, the dummy token survived at port 44067. The server recorded one device authorization and one token issuance; the cold click reused that token for `/me`, `/parse`, `/words/identify`                                |

## Dummy OAuth harness

`oauth_fixture.py` prints a new artifact directory and a loopback port. Keep the
process running, reverse that port on the isolated emulator, push its APKG, and
import through the app picker:

```sh
phase0_adb="$PWD/tmp/phase0-android.qCQ2rX/sdk/platform-tools/adb"
"$phase0_adb" -P 5041 -s emulator-5580 reverse tcp:PORT tcp:PORT
"$phase0_adb" -P 5041 -s emulator-5580 push tmp/anki-phase0-NEW/phase0-oauth.apkg /sdcard/Download/phase0-oauth.apkg
```

Enable fixed-port storage and restart AnkiDroid before signing in. Leave
external-app launch and dangerous collection-query permissions off. In the OAuth
deck, show the answer and tap the first character of `猫がいます。`, then expand
the manual sign-in controls. Copy the address with Android's selection controls,
open Chrome separately, paste and approve the dummy flow without adding an
account. Return to AnkiDroid and inspect the popup. Let storage settle and leave
the reviewer before force-stopping the app.
After relaunch, inspect storage before clicking, then click again. CDP reads
must not seed credentials; the production link code owns token writes.

The earlier direct-link run is `tmp/anki-phase0-t7gelt7k`, server port 61959.
That run enabled external-app launch; the final manual-copy run below does not.
The earlier directory retains
`oauth-server-report.json`, `android-oauth-*.json/png`, and
`android-browser-approval.png` / `android-browser-approved.png`. The cold answer
had no API requests before a click. The cold popup used the stored dummy token
without another authorization or token request. Airplane mode stayed on; adb
reverse provided loopback connectivity. This does not test public-network TLS,
real account approval, refresh-token rotation, or Android process death while
approval is pending.

## Latest integration observations

Both supported Desktop versions pass 52 checks with the final bundle. Their
versioned sections below retain the native preparation and lookup measurements.

The final Android run used AnkiDroid 2.24.1 with SDK SHA-256
`e849eb7c375f4984114739e9ce5d08ce7bc8234db14a29f2e0d0bfdfecefa2dc`
and loader SHA-256
`1352e871d2587a0c065b76557421554ce942f8229a479273e70d43a2f8f0d534`.
Evidence is in `tmp/android-final-e849eb7c-pending-final/`, with the artifact
map and rerun command in `FINAL.md`. The earlier manual OAuth and Back checks
remain in `tmp/android-final-01abab33-oauth/`; those used SDK
`01abab33a1b2985b546e7bcb6783c5564f6b30c30a2c74a6de213a3f1449af0c`
and the same loader, and were not repeated:

- External-app launch and dangerous JavaScript permissions were off. Native
  long-press, Select all and Copy selected the sign-in address. A separately
  opened Chrome pasted it, approved the dummy flow, and returned through Recents
  to a visible definition. `chrome-pasted-address.png`, `oauth-complete.json`
  and `oauth-return-popup.png` record the flow; the server recorded one device
  authorization, one browser approval and one token issuance.
- The prepared package came from the actual 25.09.4 native workflow at
  `tmp/anki-popup-release-20260913/anki-250904-e849eb7c-final/native-prepared.apkg`.
  Its SHA-256 is `65a0efc05716cca730997536ead47759873ba34520669b091ded35811b12168c`.
  `tmp/anki-phase0-yafy75yl/repackage-report.json` records changing only the card
  deck. Android imported three notes with their fields, templates and media
  unchanged; no static snapshot-reference workaround was added.
- The new cold restart retained the dummy token at origin port 44067. The Basic
  prepared answer made no API calls before a tap, observed for 10.2 seconds.
  `before-tap.json` records only the local snapshot fetch, taking 41.3 ms.
- A native tap opened the saved `cat` definition in airplane mode while GET
  `http://127.0.0.1:55681/api/v1/me` was held by CDP network interception.
  `popup-while-me-pending.json` records a visible, opacity-1 definition, and
  `popup-while-me-pending.png` captures the actual emulator before release.
  `pending-study-report.json` records the pending GET, zero JS exceptions and
  its later explicit abort. The interceptor supplied only the CORS preflight
  response, not an account response; no runtime function was patched.
- No parse or identify request occurred. The clicked flow is not zero-call;
  the pre-click render is. A pending account request no longer blocks the
  cached definition in this running-app test.
- In the earlier `01abab33` run, System Back left the reviewer for the deck
  list, rather than dismissing only the popup (`prepared-system-back.png`).

The earlier `01abab33` post-click failure displayed AnkiDroid's `me.bin`
media-error snackbar.
In [v2.24.1's error handler](https://raw.githubusercontent.com/ankidroid/Anki-Android/v2.24.1/AnkiDroid/src/main/java/com/ichi2/anki/cardviewer/MediaErrorHandler.kt),
`processFailure()` ignores hosts other than
[127.0.0.1](https://raw.githubusercontent.com/ankidroid/Anki-Android/v2.24.1/AnkiDroid/src/main/java/com/ichi2/anki/pages/AnkiServer.kt).
It does not compare ports or schemes. The generated filename comes from
`URLUtil.guessFileName`, not a requested `me.bin` asset. A failed request to
`https://shirabe.org/api/v1/me` is excluded from this specific snackbar path.
No actual production-HTTPS offline test was performed. The earlier 4.1 ms
failure did not establish pending-request behavior; the new held-GET test
above does. No Android popup latency distribution is claimed.

## Source entry points

Source entry points in the sibling AnkiDroid checkout:

- `AnkiDroid/src/main/assets/scripts/js-api.js`: API 0.0.3 method inventory and constructor.
- `AnkiDroid/src/main/java/com/ichi2/anki/AnkiDroidJsAPI.kt`: native search, contact validation, query permission and callback.
- `AnkiDroid/src/main/java/com/ichi2/anki/ViewerResourceHandler.kt`: GET interception of collection media using safe filename resolution.
- `AnkiDroid/src/main/java/com/ichi2/utils/AssetHelper.kt`: JSON/JS MIME mapping.
- `AnkiDroid/src/main/java/com/ichi2/anki/ui/windows/reviewer/StudyScreenRepository.kt`: dynamic/fixed-port choice.
- `api/src/main/java/com/ichi2/anki/api/AddContentApi.kt`: separate Android-app authoring interface.

The external Android API requires `com.ichi2.anki.permission.READ_WRITE_DATABASE`
and uses the exported provider `com.ichi2.anki.flashcards`.
`addNote(modelId, deckId, fields, tags)` returns a note ID;
`updateNoteFields(noteId, fields)` replaces the complete ordered field array.
`addMediaFromUri(uri, preferredName, mimeType)` handles audio/image imports and
requires access to the source URI. These are native-app APIs, not template JS
capabilities. Normal note editing is not an exported template action. The
ACTION_SEND text/subject route only fills the first two fields and is not a
general mapped-note editor. These interfaces were inspected in source, not
claimed as emulator-tested authoring paths.

Official references: [JavaScript API](https://github.com/ankidroid/Anki-Android/wiki/AnkiDroid-Javascript-API),
[third-party app API](https://github.com/ankidroid/Anki-Android/wiki/Third-Party-Apps),
[tested APK release](https://github.com/ankidroid/Anki-Android/releases/tag/v2.24.1).

## Real AnkiWeb sync and native recording selection, 2026-09-13 JST

The owner authorized the existing account and the temporary deck
`Shirabe Test 2026-09-13 Popup and Audio`. Anki's native Create Backup ran before
the first import. Normal Desktop sync transferred four new notes, five cards,
three new note types and one deck. No one-way sync, existing-note import,
real-profile add-on installation or production deployment was performed.

`tmp/ankiweb-validation-20260913-verified` contains the original and updated APKG,
fixture manifests, media hash reports and browser screenshots. The source cards
were prepared through the native Browser workflow using deterministic dictionary
fixtures. Their synthetic Japanese segmentation is not linguistic-quality evidence.

The original nine media files matched their Desktop SHA-256 hashes. Updating the
two prepared note types to bundle `c712132c` added one asset, for ten matching
files totaling 349,404 bytes. The authenticated browser media check received
HTTP 200 for every file. Card payloads contain no login credentials.

AnkiWeb's protected snapshot media requires its same-origin session cookie.
With cookies omitted, the JSON request redirected to authentication and failed.
With `credentials: "same-origin"`, the snapshot loaded and the actual popup
opened `猫 / ねこ / cat` with no Shirabe API calls. Its field-selection controls
also worked without revealing or grading a card. The screenshots
`ankiweb-cat-popup.png` and `ankiweb-popup-actions.png` show settled opacity 1.
The earlier partial-transition screenshot is not the settled visual result.

The recording reached AnkiWeb unchanged: AAC, mono, 48 kHz, 0.829 seconds,
11,387 bytes. Its `[sound:…m4a]` tag produced no playback control. The comparison
in `tmp/ankiweb-audio-compatibility-20260913/ankiweb-playback.json` then recorded
completed playback through an MP3 sound tag and an HTML M4A audio element.
Both ended at 0.829 seconds, with no media error or card advancement. This is
an AnkiWeb sound-tag limitation, not a general inability to decode M4A.
`ankiweb_audio_probe.py` builds the comparison and `ankiweb_audio_check.mjs`
clicks its actual audio controls while guarding the test note ID.

Only the approved ねこ MP3 was published to the audio CDN. The public file has
10,700 bytes and SHA-256
`429847003510fe7eac3c569ea31138a2dbc61cdbe473b3061113e4cf8083d201`.
The M4A original remains byte-identical. Publication evidence is in
`tmp/anki-mp3-publication.YsPu64`; no application deployment or bulk conversion
was performed.

The M4A saved-popup pronunciation fixture in
`tmp/anki-complete-current-v5.5aBVrs/ankiweb-current-e4` was imported over the
existing prepared test note and synced. `foreground-media-report.json` records
HTTP 200 responses and exact hashes for all ten files, including SDK `e4ec98be`,
the version 2 cat snapshot, and both approved audio encodings.

`foreground-popup-audio-report-v6.json` records the guarded real-client popup
run. After revealing the answer, the saved `猫 / ねこ / cat` entry opened and its
source details expanded without grading the card. Missing and corrupt recording
responses kept the definition open and allowed retry. The real M4A then completed
twice, a third play stopped when the popup closed, all created players used the
verified data URL, and no playback error occurred. The run made exactly three
recording requests and no Shirabe requests. `ankiweb_popup_audio_check.mjs`
reveals the answer before lookup because answer-side-only lookup is the default.
The fixture uses synthetic dictionary data and the approved real recording; it
does not establish production OAuth or parser quality.

After normal sync, the read-only row audit found no original rows
changed or missing in notes, cards, revlog, notetypes or decks, excluding only
USN sync bookkeeping. The audit retained at
`tmp/ankiweb-prepared-audio-20260913-verified/existing-after-audit.json` records
four added notes, five cards, ten test reviews, three note types and one deck.
It stores row hashes, not user note text, and includes the updated fixture
import. No original row or schema changed.

Rerun the audit without opening the collection for writing:

```sh
python3 anki-addon/tests/ankiweb_audit.py compare \
  tmp/ankiweb-validation-20260913-verified/existing-before.json \
  --collection '/Users/davafons/Library/Application Support/Anki2/User 1/collection.anki2'
```

For a new authorized browser session, open this test deck before running
`ankiweb_media_check.mjs` with `ankiweb-fixture-updated.json`. The helper refuses
a current note outside the fixture and reports only filenames, sizes and hashes.
`ankiweb_fixture.py` builds a new test deck from a native-prepared APKG and verifies
an isolated reimport. Its `--refresh` preserves test note identities and writes
a separate package; that package was also imported into the real test note types.

### Live MP3 native authoring

Anki 25.09.4 and 24.04 each passed 48 native authoring checks with the approved
public MP3 and production HTTPS downloader. Reports, screenshots and per-file
package manifests are in `tmp/anki-live-cdn-current.4RVcvh/run` and
`tmp/anki-live-cdn-minimum.T75gB3/run`. Both use Cocoa and SDK `ee75bf74`.
The runs verified explicit selection, native Add Save, update Save and Undo,
exact downloaded bytes, attribution and preserved card history. They used
isolated profiles with no sync account and deterministic dictionary metadata.

Both reports retain importer warnings. Their captured contexts identify the
harness's output directory and Python script path being offered to Anki's file
import handler. No recording validator rejected the downloaded MP3. These
runs are not claimed warning-free.

### Earlier native authoring package

The earlier native authoring package is
`tmp/anki-phase0-_6z_h4ac/shirabe-phase0.ankiaddon`, SHA-256
`eb2a03b96332e8a3ba258d0b2e6295fa6a1aff0bd6495f3c4116df40cb331611`.
Anki 25.09.4 and 24.04 each passed
82 authoring tests and 45 real native authoring checks, including explicit
recording choice, retry, cancel, attribution, field preservation, Save and Undo.
The matching reports are in `tmp/anki-phase0-_6z_h4ac` and
`tmp/anki-phase0-g308ccx_`. Those checks use a synthetic AAC transport; the
real CDN byte transfer is the separate AnkiWeb fixture above.

The installed-Anki Cocoa suite passes 255 tests. Its offscreen run passes 254
and fails the large-text label height assertion; the six real Cocoa layout
checks pass on both versions. Both authoring harness reports also retain an
`Unsupported file type.` message traced to Anki's deck importer, not the selected
recording validator. Its triggering filename remains unproven; the harness is
not claimed warning-free. The SDK passes 257 tests and TypeScript; the c712132c
bundle passes 52 actual Desktop reviewer checks. Physical Android remains
deferred at the owner's request. Older Android results below used different bytes.

## Earlier native M4A importer checkpoint

The installable package is
`tmp/anki-popup-release-20260913/anki-250904-aac-ready/shirabe-phase0.ankiaddon`,
SHA-256 `53bf97d639fc1e825e8ea6940529f37d71c37b472e57773978b1b856d2f670f8`.
Its manifest differs from the timed package below only in `authoring_media.py`.
The SDK and phone loader hashes are unchanged. This package passed all 52 actual
Anki 25.09.4 reviewer checks with no JavaScript errors, including native
preparation and offline lookup. Its matching `desktop-report.json` retains the
checks. No new Android bundle or timing measurement is implied.

The installed-Anki suite passes 240 tests. The 26 media/native-authoring tests
also pass on Anki 24.04. A generated 150 ms tone uses AAC, 48 kHz, mono, in M4A,
matching the current encoder's container. Anki stores it once, retains its bytes,
and returns a sound tag when rendering the card. Mislabeled content and invalid
M4A file-type boxes are refused. The acceptance test failed before the change
with unsupported-format and invalid-media errors. This earlier importer-only
checkpoint used no VOICEPEAK recording. The owner-confirmed permission and
native selection workflow are covered by the checkpoint above.

```sh
anki_python='/Users/davafons/Library/Application Support/AnkiProgramFiles/.venv/bin/python'
"$anki_python" anki-addon/tests/run.py
cd anki-addon/tests
"$anki_python" -m unittest test_authoring_media test_authoring_native
```

Use installed Anki's Python for the full suite; ordinary Python skips native
checks. The generated-tone test additionally needs `ffmpeg`, which was present
in both measured runs.

## Anki 25.09.4 lookup timing, 2026-09-13 JST

The native fixture now enforces p95 ceilings of 300/225 ms for cold/warm online
lookup and 60/85 ms for prepared lookup. These allow 41–53% above the slowest
recorded p95 across the two versions below. The validator requires all four
series, at least 12 finite samples each, and summaries matching the raw values.
These are synthetic native-fixture limits, not browser, Android or WAN targets.

Both saved baseline reports pass the report-only check:

```sh
python3 anki-addon/tests/template_reviewer_check.py --check-performance-report tmp/anki-popup-release-20260913/anki-250904-e849eb7c-final/desktop-report.json
python3 anki-addon/tests/template_reviewer_check.py --check-performance-report tmp/anki-popup-release-20260913/anki-2404-e849eb7c-final/desktop-report.json
```

This validates old measurements without starting a client; it does not validate
the current SDK. `test_template_reviewer_check.py` exercises regressions and
malformed reports while refusing native imports and network access.

The frozen bundle passed all 52 checks in the actual 25.09.4 reviewer, with zero
JavaScript errors. Native Add, Update, Browser search, Undo, parsing consent,
hidden-cloze exclusion, preparation and APKG export/reimport passed. The
signed-out prepared popup made no dictionary API requests.

Evidence is in `tmp/anki-popup-release-20260913/anki-250904-e849eb7c-final/`:

- `desktop-report.json` retains checks, requests, 48 raw lookup samples,
  native-state replies and unchanged notes/cards/revlog fingerprints.
- `shirabe-phase0.ankiaddon` is the tested production package; its SHA-256 is
  `3d85cd64a03969eb998bc2e68fa4f91bf9e6f350c6114d126260c0014797aca9`.
- `native-prepared.apkg` contains the three notes prepared through native consent.
- `package-manifest.json` records each packaged file's hash. The SDK is
  `e849eb7c375f4984114739e9ce5d08ce7bc8234db14a29f2e0d0bfdfecefa2dc`.

Each lookup mode has 12 samples. Timing includes default lookup delays and ends
at the first visible definition frame. Native collection-state replies are
checked separately, without changing that timestamp. The popup no longer waits
for study metadata before displaying its dictionary content.

| Lookup                           |   Median |      p95 |
| -------------------------------- | -------: | -------: |
| Online, fresh document           | 199.0 ms | 212.5 ms |
| Online, reopened                 | 149.4 ms | 159.5 ms |
| Prepared offline, fresh document |  28.6 ms |  39.2 ms |
| Prepared offline, reopened       |  33.9 ms |  53.7 ms |

Three notes produced 31,015 snapshot bytes in 0.222 seconds from accepted consent
through native apply, with 62 dictionary requests. This is a small synthetic
loopback fixture, not production throughput. Metadata loading, user consent wait,
export and sync are excluded. The report retains those boundaries with the values.

Use `template_reviewer_check.py --prepare-offline --bundle-sha256 HASH --output NEW_DIR`
with the target Anki Python and
`QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu`.
The initial offscreen 25.09.4 run without the GPU flag exited 139 at startup; it
is not a passing reviewer result. Disabling GPU acceleration avoids that harness
startup failure. No production source or bundle was changed between those runs.

The first `e849eb7c` run stopped at the harness's former requirement that a native
study reply precede the visible definition. The runner now records the definition
timestamp first, then separately requires the native reply and validates its
payload. The successful `-final` run above uses that corrected measurement.
The SDK regression in `packages/card/test/standalone.test.ts` holds the online study request
unresolved while requiring the prepared `cat` definition to be visible; the
authoring suite checks that a later reply preserves selected fields and focus,
and cannot paint a dismissed popup. Both tests failed before the renderer fix.

## Anki 24.04 minimum-version gate, 2026-09-13 JST

Actual Anki 24.04 passed all 52 desktop checks with zero JavaScript errors.
The isolated environment used Python 3.13.5, PyQt 6.6.1 and Qt runtime 6.6.2,
with offscreen rendering and GPU acceleration disabled. This establishes
reviewer execution at the declared `min_point_version: 240400`, not just imports.
The separate API inventory found all 128 checked entries on 24.04; that check
establishes symbol availability only. Newer Anki and Android have separate gates.

The run installed the production add-on into a disposable profile, without a
sync account or user profile. Dictionary requests used synthetic loopback HTTP
responses with no added latency. Native Add, Update, Browser search, Undo,
consent, hidden-cloze exclusion, preparation, APKG roundtrip and signed-out
offline lookup passed. The three native snapshots retained `jmdict: show`.

Evidence under `tmp/anki-popup-release-20260913/anki-2404-e849eb7c-final/`:

| Artifact                                                                                                                                              | Recorded evidence                                                                                                           |
| ----------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| [desktop-report.json](../../tmp/anki-popup-release-20260913/anki-2404-e849eb7c-final/desktop-report.json)                                             | 52 checks, 48 raw lookup timings, separately validated native-state replies, preparation timing and collection fingerprints |
| [package-manifest.json](../../tmp/anki-popup-release-20260913/anki-2404-e849eb7c-final/package-manifest.json)                                         | Archive and per-file SHA-256 hashes                                                                                         |
| [shirabe-phase0.ankiaddon](../../tmp/anki-popup-release-20260913/anki-2404-e849eb7c-final/shirabe-phase0.ankiaddon)                                   | Tested 23-file production package; no runtime metadata, environment files or populated credential defaults                  |
| [native-prepared.apkg](../../tmp/anki-popup-release-20260913/anki-2404-e849eb7c-final/native-prepared.apkg)                                           | Three notes prepared through native consent and collection operations                                                       |
| [desktop-native-prepared-offline-popup.png](../../tmp/anki-popup-release-20260913/anki-2404-e849eb7c-final/desktop-native-prepared-offline-popup.png) | Actual signed-out reviewer definition after preparation                                                                     |

The archive SHA-256 is
`2d14bce2e0e6402a6b7dcea395d8ec8a3e16c77bc63a74183faee0a2d1499022`.
Its bundled SDK SHA-256 is
`e849eb7c375f4984114739e9ce5d08ce7bc8234db14a29f2e0d0bfdfecefa2dc`.
The profile directory contains synthetic runtime state and is not a distributable.

### Native lookup timing

The production SDK handled QTest mouse gestures in the real reviewer.
Renderer `performance.now()` measured each captured click through the first
animation frame with the expected visible headword and gloss. Loading shells,
hidden hosts and transparent definitions did not satisfy the observation.
The first visible definition does not wait for study metadata. After freezing
that timestamp, the runner separately awaited and validated all 48 native
collection-state replies and required the definition timestamp to remain unchanged.
Python polling is outside the interval; frame sampling delay is included. These
are render-ready timings, not physical display, heap or production-network measurements.

Cold means a fresh reviewer document and SDK host, not a cold app or filesystem
cache. Warm means the same target reopened after dismissal without a host reset.
Each pair used the same Basic note. Percentiles use nearest rank.

| Lookup                | Samples | p50, ms | p95, ms |
| --------------------- | ------: | ------: | ------: |
| Online cold           |      12 |   191.4 |   207.8 |
| Online warm           |      12 |   146.8 |   157.3 |
| Prepared offline cold |      12 |    22.9 |    30.1 |
| Prepared offline warm |      12 |    24.0 |    55.7 |

Warm online and both prepared-offline series made zero dictionary API requests.
The lookup-only batches preserved fingerprints for all 9 notes, 11 cards and
the empty review log. The report retains every sample and screenshot path.

### Native preparation throughput

Accepted consent through completed apply took 0.212505041 seconds for 3 notes,
3 snapshot files and 31,015 bytes, with 62 loopback dictionary requests.
That is 14.12 notes/second and 145,949 snapshot bytes/second in this small fixture.
The interval includes actual Qt DOM capture, media writes and collection apply.
It excludes metadata and initial reads, consent wait, result acknowledgement,
APKG export and offline replay. Snapshot bytes exclude the shared SDK bundle.
This is an actual native-path measurement, not a bulk or WAN throughput estimate.

### Runner and environment reference

The temporary 24.04 interpreter and commands for a new isolated measurement are:

```sh
anki2404_python=/var/folders/b0/ggkwwpj56nx_kntnkyr5w3p80000gn/T/shirabe-anki2404.iJCsPwpObZ/venv/bin/python
QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu \
  "$anki2404_python" anki-addon/tests/template_reviewer_check.py --prepare-offline \
  --bundle-sha256 e849eb7c375f4984114739e9ce5d08ce7bc8234db14a29f2e0d0bfdfecefa2dc
python3 anki-addon/tests/check_against_anki.py --python "$anki2404_python"
```

Omitting `--output` allocates a fresh directory. An explicit output directory
must not exist. The runner rejects a mismatched SDK hash and never substitutes
another Anki interpreter. Its only 24.04 adapter counts actual fixture cards
because that version's `add_note()` result lacks `count`; the original method
is restored before AnkiQt starts. The same runner accepts a 25.09 interpreter
and records that version separately. Direct `desktop_check.py` runs do not add
the preparation and lookup timing instrumentation.

## Shared attachment browser performance, 2026-09-13 JST

### Frozen-bundle rerun after nonblocking study refresh

The Anki bundle and web asset both had SHA-256
`e849eb7c375f4984114739e9ce5d08ce7bc8234db14a29f2e0d0bfdfecefa2dc`.
The exact-bundle browser run passed every interaction and performance gate.
Its report is
`/var/folders/b0/ggkwwpj56nx_kntnkyr5w3p80000gn/T/shirabe-attach-browser-HT8r3R/report.json`.
No source or bundle edits accompanied this verification.

```sh
node packages/card/test/attach-browser.mjs --sdk-bundle anki-addon/src/web/shirabe-card.js
node packages/card/test/attach-browser.mjs --check-performance-report /var/folders/b0/ggkwwpj56nx_kntnkyr5w3p80000gn/T/shirabe-attach-browser-HT8r3R/report.json
npm --prefix packages/card test
npm --prefix packages/card run check
```

The targeted source tests passed 114 of 114, including the stalled-study and
late-status selection/focus/dismiss regressions. The full SDK suite then passed
256 of 256 tests. TypeScript checking and house rules passed.
Headless Chromium 151.0.7922.34 used the same loopback fixture and viewport
sizes described below, with 12 cold and 12 warm samples per mode.

| Mode          | Cold p50 / p95, ms | Warm p50 / p95, ms |
| ------------- | -----------------: | -----------------: |
| Desktop mouse |         9.3 / 10.7 |          1.3 / 1.4 |
| Mobile touch  |         9.0 / 12.1 |          1.5 / 1.7 |

The event-to-`card_opened` interval now ends before optional read-only study
metadata completes. These numbers measure definition readiness, not status
readiness. Earlier measurements below included that wait; the latency change
does not measure a faster network request.

Each mode recorded 100 lookups and 102 requests over 100 flips: one account
read, one definition read and 100 single-word study reads. Listener inventories
stayed at 36/8/12 on window/document/root, with 20/7/0 after detach. The existing
50 ms cold-p95 and 25 ms warm-p95 ceilings passed. Hidden-host hit-testing,
fade-in parsing, selection without reparsing, review-event isolation and the
zero-request offline bootstrap also passed.

| Mode          | Baseline JS bytes | Growth at 100 flips | Growth at 200 flips | Growth at 300 flips | No-SDK growth at 300 flips |
| ------------- | ----------------: | ------------------: | ------------------: | ------------------: | -------------------------: |
| Desktop mouse |         3,274,444 |              39,152 |             181,268 |             359,812 |                     20,500 |
| Mobile touch  |         3,302,372 |              28,512 |             187,060 |             369,252 |                     18,116 |

SDK samples retained 339 DOM nodes and 77 event listeners throughout. Heap
growth remains unattributed, with no heap budget or leak-free claim. This run
used the default 300-flip diagnostic, not the extended idle/GC procedure.
The 1,000-flip plateau below belongs to the earlier `01abab` bundle and is not
a measurement of this bundle. These are isolated-browser fixture results,
not native-client, production-network or process-memory measurements.

### Earlier frozen-bundle baseline

The exact-bundle run used `anki-addon/src/web/shirabe-card.js`, SHA-256
`01abab33a1b2985b546e7bcb6783c5564f6b30c30a2c74a6de213a3f1449af0c`.
The web asset had the same hash. This records workspace bytes, not proof that
either native runner installed them. Native reports establish that separately.
Raw evidence is the machine-local
`/var/folders/b0/ggkwwpj56nx_kntnkyr5w3p80000gn/T/shirabe-attach-browser-OxXyP5/report.json`.
That report has no failures and passes every performance budget.

The rerun commands, from the repository root, are:

```sh
shasum -a 256 anki-addon/src/web/shirabe-card.js app/assets/javascripts/shirabe-card.js
node packages/card/test/attach-browser.mjs --sdk-bundle anki-addon/src/web/shirabe-card.js
node packages/card/test/attach-browser.mjs --check-performance-report /var/folders/b0/ggkwwpj56nx_kntnkyr5w3p80000gn/T/shirabe-attach-browser-OxXyP5/report.json
```

The harness prints a new artifact directory for each run. `--sdk-bundle` reads
the existing file without building it. Omitting that option builds source in
memory and does not verify the release artifact. The report records its input
hash. The report-only command checks performance assertions without a browser.

Headless Chromium 151.0.7922.34 ran an 1100x800 desktop viewport and a 390x844
mobile viewport with touch emulation. Requests used a loopback HTTP fixture,
production `createAnkiHost`, and Python-generated snapshots. No live API or
large collection was queried. These timings are not Anki Qt, Android WebView,
physical-device, WAN, or production-service measurements.

Each mode used 12 cold and 12 warm samples. Cold samples created a new Anki
host; warm samples reused its account and definition caches. Timing spans the
input event through `card_opened` after DOM rendering, excluding reveal animation.

| Mode          | Cold p50 / p95, ms | Warm p50 / p95, ms |
| ------------- | -----------------: | -----------------: |
| Desktop mouse |        10.2 / 11.4 |          8.5 / 9.9 |
| Mobile touch  |        12.3 / 13.5 |        10.0 / 11.9 |

The enforced latency ceilings are 50 ms cold p95 and 25 ms warm p95, with at
least 12 samples per temperature and at least 100 flips per mode. The separate
100-flip loop recorded 100 explicit lookups and 102 requests per mode: one
`/me`, one definition read, and 100 single-word study-state reads. Page parsing
made no requests in that loop. The request budget rejects extra account or
definition reads, unrelated endpoints, and more than one study read per flip.

Listeners on window/document/`#qa` stayed at 36/8/12 from attachment through
100 reused-root flips. Detach left 20/7/0, including the reusable renderer and
browser harness listeners. The gate compares retained listener inventories,
not just totals. Separate interaction checks passed hidden-host hit-testing,
consented fade-in, selection preservation, zero selection reparses, reviewer
event isolation, and offline bootstrap with zero API requests.

### Heap diagnostic

After 10 warm-up flips, each sample detached the attachment, cleared harness
logs, waited two animation frames, and forced GC. The no-SDK control replaced
and clicked an ordinary button, then made one equivalent loopback HTTP read.
The control followed the SDK series on the same page with attachment disabled;
it was not a fresh page without SDK code. Each series used its own post-warm-up
baseline.

| Mode          | Baseline JS bytes | Growth at 100 flips | Growth at 200 flips | Growth at 300 flips | No-SDK growth at 300 flips |
| ------------- | ----------------: | ------------------: | ------------------: | ------------------: | -------------------------: |
| Desktop mouse |         3,272,044 |              27,204 |             190,888 |             369,312 |                     20,328 |
| Mobile touch  |         3,305,368 |              25,764 |             185,216 |             369,320 |                     18,116 |

The SDK samples retained 340 DOM nodes and 77 JavaScript event listeners in
both modes. Those counts stayed flat. The roughly 0.37 MB heap growth did not.
Its attribution remains unresolved; this is not evidence of leak-free memory.
The diagnostic measures the isolated page heap, not process RSS or native-app
memory. No heap-growth budget is enforced.

### Extended heap follow-up

The same exact bundle also passed a 1,000-flip run with a 15-second idle period
and three further GC samples after each SDK and control series. Its report is
`/var/folders/b0/ggkwwpj56nx_kntnkyr5w3p80000gn/T/shirabe-attach-browser-gqXwRw/report.json`.
The test-only options leave the default 300-flip diagnostic unchanged.

```sh
node packages/card/test/attach-browser.mjs --sdk-bundle anki-addon/src/web/shirabe-card.js --memory-flips 1000 --memory-settle-ms 15000
```

These are JS heap growth bytes relative to each series' post-warm-up baseline:

| Mode          | 300 flips | 600 flips | 1,000 flips | After idle and third GC | Control after idle and third GC |
| ------------- | --------: | --------: | ----------: | ----------------------: | ------------------------------: |
| Desktop mouse |   369,312 |   557,092 |     564,412 |                 564,396 |                          10,620 |
| Mobile touch  |   369,320 |   557,616 |     564,744 |                 564,728 |                           8,640 |

Growth slowed near 600 flips. The next 400 flips added 7,320 bytes on desktop
and 7,128 on mobile, rather than continuing the early roughly 180 KB per 100
flips. All three post-idle SDK GC samples were identical in each mode. SDK
DOM nodes and event listeners remained at 340 and 77 throughout.

The measured transport delegates directly to `window.fetch`; it does not run
`createLink` device-flow polling or an `AbortSignal.timeout`. Idle and repeated
GC did not release the additional roughly 564 KB. The plateau is an observation
in this repeated-word fixture, not proof of JIT or cache attribution. No heap
retainer analysis or native-app memory measurement was performed. The cause
remains unresolved, and these results do not establish leak-free runtime.
No runtime or bundle edits followed this diagnostic.

### Validation reference

The same source checkpoint passed 253 SDK tests, both TypeScript checks, and
house rules. Extension tests passed 301 of 302. The unrelated existing
background-source condition failed in
`extension/test/manifest.test.ts:423`, test
`offline parsing does not wait for profile-state network hydration`.
Its `/return lists;/` assertion reads `extension/src/background/background.ts`;
it does not inspect the Rails `api_dictionaries` helper. No extension fix or
distribution rebuild accompanied these measurements.

```sh
npm --prefix packages/card test
npm --prefix packages/card run check
npm --prefix extension test
npm --prefix extension run check
bin/house-rules
```

## Preparation size and bounds

The fixture runs production preparation with synthetic one-character tokens and
small bilingual definitions. It asserts the selected `dictionaries` list on every
parse and identify request. It does not represent typical dictionary entry sizes.
The measurement command, from the repository root, is:

```sh
python3 anki-addon/tests/offline_measure.py --rounds 5 --latency-ms 25
```

The 2026-09-13 run measured these UTF-8 JSON payload sizes, including the envelope,
saved preferences, source names and license notices:

| Fixture                 | Prepared tokens | Snapshot bytes | Requests |
| ----------------------- | --------------- | -------------: | -------: |
| Two words               | 2/2             |          1,655 |        4 |
| Emoji and repeated word | 3/3             |          2,352 |        5 |
| Twelve repeated words   | 12/12           |          6,775 |       14 |
| Request boundary        | 64/64           |         33,391 |       65 |
| Request-limited note    | 64/70, partial  |         33,938 |       65 |

The five snapshots total 78,111 bytes. Each note also carries a 213-byte export
reference in `ShirabeLookup`; shared JavaScript bundle bytes are separate.

The fixture simulates 25 ms for each of 153 serial requests: 3.825 seconds of
modeled network time, with no actual network or sleeps. The measured local median
over five rounds was 57.2 ms. Together these model 3.882 seconds, or about 1.29
processed notes per second, including the partial note. This is not a WAN or
server-throughput measurement. DOM capture, media persistence, sync and real
server processing are outside the timing. Local execution time varies by run.

Preparation enforces 128 KiB per snapshot, 24 KiB per candidate including its
entries, 256 KiB per response and 65 requests per note. Oversized or missing
results leave reported partial coverage. The default measurement excludes audio
and corpus examples; neither mode downloads full dictionary packs. The
snapshot covers prepared text and definitions, not arbitrary offline search or
full online content parity.

The default collection-wide budget is 25 MiB of owned snapshot and recording files, counting
older and damaged generations. Users may explicitly raise it to 256 MiB.
The preparation batch has a separate 25 MiB payload limit. Identical snapshots
share a file and cost zero additional media bytes; a later preparation date
creates a new hash generation that counts toward collection storage. The native
storage regression checks duplicate batches, retained generations and refusal
when a new file would exceed the budget by one byte:

```sh
python3 -m unittest discover -s anki-addon/tests -p test_offline_measure.py
```

That regression invokes real Anki collection and media operations when installed
Anki Python is available; otherwise it reports a skip. Code bundles have a
separate 4 MiB per-file bound and appear in storage review, but do not count
toward the 25 MiB snapshot budget. Neither bundle generations nor note-field
references are included in the snapshot byte totals above. Cleanup requires
explicit review of verified unreferenced owned files; preparation never
automatically deletes old generations.

### Optional-content preparation measurements

`tmp/anki-preparation-final.RRYFvm/` records four modes on Anki 25.09.4 and
24.04, with source hashes, runtime versions, 12 raw local timing samples and
separate native persistence checks. Use `*-current-final.json` and
`*-minimum-final.json`. The earlier `tmp/anki-preparation-reuse.R2T48e/` candidate
predates the single-backend cache guard and is not the handoff build.
The 24.04 environment uses its own Anki packages with current-installation
dependency fallbacks. The before-cache baseline is
`tmp/anki-preparation-measure.uAx73y/` (`*-final.json` and `*-minimum.json`).

| Five-case mode     | Requests before / after | Stored bytes before / after | Complete notes before / after |
| ------------------ | ----------------------: | --------------------------: | ----------------------------: |
| Definitions        |                153 / 10 |             78,111 / 80,648 |                         4 / 5 |
| Examples           |                159 / 18 |             80,894 / 86,096 |                         2 / 2 |
| Audio              |                156 / 13 |           131,146 / 135,507 |                         4 / 5 |
| Examples and audio |                162 / 21 |           133,929 / 140,955 |                         2 / 2 |

These are boundary workloads with synthetic word identities, not a sample of
learners' decks. The 70-token case now prepares all definitions; extra saved
tokens and examples explain the byte growth. Repeated examples still hit the
per-note allowance. Coverage and issue codes remain in every report;
processed-note throughput is not complete-note throughput. The example text is
from the verified Tatoeba fixture; the local M4A is the approved 11,387-byte clip.
No recording is played, and no network request is made by this measurement.

Both clients' media backends verify every saved snapshot and recording hash.
With both options, the five snapshots use 129,568 bytes and share one recording.
The 100-note repetition stores the same six files, totaling 140,955 bytes, while
the conservative preparation allowance charges 2,602,747 bytes. This confirms
deduplication, not the expected size of 100 distinct notes. Native persistence
is timed once per report; it is not a persistence p95 measurement.

For the five cases, current Anki's final local p95 ranges from 24.880 to
80.348 ms; the minimum version ranges from 25.113 to 151.188 ms. These are local
preparation times, excluding requests and collection setup. At an assumed
100 ms per serial request, the combined mode models 2.1 seconds of network
time. It does not measure production latency or predict a learner's throughput.
The 100-note repetition makes 325 requests instead of 3,183, with two
identifications instead of 2,900. Its local median/p95 is 1,566.355/1,741.617 ms;
the before-cache run measured 1,783.587/1,942.171 ms. Both runs finish 40 of 100
notes with all requested optional content; all definitions are now covered.

The optional budget gate was set from the baseline's `*-verified.json` series:
350 ms local p95 per five-case set gives more than twice the slowest observed
157.784 ms. JSON ceilings are unchanged. Request ceilings now enforce reuse,
and definition coverage requires all 70 tokens in the long case. The gate also rejects reduced token
coverage, insufficient/invalid timing samples and lower complete-note counts.
It is a fixed-workload regression check, not a product-wide throughput promise.

```sh
python3 anki-addon/tests/offline_measure.py --rounds 12 --latency-ms 100 \
  --examples --check-budget \
  --recording tmp/anki-saved-examples.HT5XzS/ankiweb-verified/collection.media/shirabe_6c3cb4099cff2ef1626de4af45243318976b7c446d450ae974d8193c4dd22026.m4a \
  --native-output tmp/anki-preparation-new --output tmp/anki-preparation-new.json
```

Choose unused output paths. Add `--copies 20` for the repeated 100-note case.
Without `--native-output`, only Python preparation and modeled network time are
reported. These tests do not establish card-flip memory retention, native popup
latency, mobile behavior or WAN throughput.

## Real dictionary size baseline, 2026-09-13

`offline_corpus_measure.py` and `offline_corpus_api.rb` measure actual local
parse/identify controller responses, using read-only databases and an unsaved
public-dictionary identity. Captures retain the original response bytes, so the
production response-size cap still applies during replay. Controller executions
and replay hits are counted separately; neither is an authenticated HTTP or WAN
benchmark. The six harness regressions include an escaped response that failed
before preserving its wire bytes.

The 33 excerpts spanning all eleven chapters of _Botchan_ produced 2,614,909
uncompressed snapshot bytes, median 84,294 per note, excluding audio and shared
scripts. All 33 snapshots are distinct. Five notes lose token results at the
128 KiB limit; 755 of 772 token occurrences remain prepared. Another limit also
matters: 29 notes omit alternatives beyond the three-candidate allowance, so only
four notes have complete requested content. These are sentence-heavy excerpts,
not a representative learner-deck sample or linguistic-quality evaluation.

Identical serialized candidate values occupy 1,435,569 repeated bytes across
the snapshots. The captured dictionary contains English and Spanish definitions
even though the fixture selects English UI preferences. These measurements
identify storage work still needed; they do not establish savings from an
unimplemented shared-entry format. Eight one-word notes ranged from 1,967 bytes
for 食べる to 16,447 for かける, whose alternatives were capped.

The reports, original response captures, provenance, exact selected corpus
indexes, and rerun commands are in
[`tmp/anki-real-corpus.jcS1Bd/README.md`](../../tmp/anki-real-corpus.jcS1Bd/README.md).
That baseline predates the compression implementation below. It does not measure
uploaded-entry stress or native media persistence.

## Full-book compression, 2026-09-13

The complete 2,464-passage _Botchan_ capture and production replay are in
[`tmp/anki-full-corpus.fSGAjI/README.md`](../../tmp/anki-full-corpus.fSGAjI/README.md).
Raw JSON remains 124,441,859 bytes, with 37,205 of 38,117 tokens prepared.
Compression writes 25,865,594 verified media bytes across 2,460 files, so all
2,464 snapshots fit within the existing 25 MiB collection budget. The earlier
uncompressed native run stopped after 596 notes. This excludes audio, scripts,
note fields, database overhead and export compression.

The median per-note stored payload is 9,319 bytes; p95 is 22,588. The decoded
128 KiB cap remains in force. Compression leaves the 105 byte-limit notes and
1,817 candidate-limit notes unchanged; 646 notes have all requested content.
These are extracted literary passages, not an estimate for a typical deck.

The Python codec and shared SDK tests cover old and compressed formats, exact
content, hashes, malformed streams, expansion limits and cancellation. Native
storage probes exercise exact-fit and one-byte-short budgets, deduplication,
both APKG formats, mixed-generation cleanup and restoration. The saved-example
SDK probe now consumes the actual Python-produced compressed bytes rather than
reserializing a decoded snapshot.

The storage prototype compared seven representations on the same prepared
objects. Shared compressed entries reduced payloads further but added thousands
of files and exceeded the current reference limit on 411 notes. A note-local
candidate table saved another 2.9% over compression. Its Python-only timings and
modeled file allocations are not client measurements. Prototype timestamps were
not frozen; production replay uses a fixed timestamp and canonical key ordering.

`snapshot_compression_probe.py` runs the actual bundled reader in explicitly
offscreen Qt, with an in-memory profile, blocked network requests and no view.
The largest decoded file (131,051 bytes), largest stored file (26,600 bytes),
and a legacy uncompressed file each passed twelve reads and lookup checks on
both Anki versions. The reports are `compression-current-*.json` and
`compression-minimum-*.json` in the full-corpus evidence directory; use the
current largest-decoded `-pass.json`. Its first report failed only because the
probe included an unsupported CSP directive in a meta element; that report is
retained. The production bundle did not change between these runs.

| File               | Anki 25.09.4 median / p95 load ms | Anki 24.04 median / p95 load ms |
| ------------------ | --------------------------------: | ------------------------------: |
| Largest decoded v2 |                     19.55 / 27.60 |                   23.45 / 64.20 |
| Largest stored v2  |                     19.15 / 25.70 |                   27.55 / 53.70 |
| Legacy v1          |                      9.75 / 23.90 |                   10.30 / 35.90 |

These include hash verification, decoding and validation, but use in-memory
responses. They are not filesystem, network, reviewer interaction, AnkiWeb or
Android timings. Qt reports current library 6.9.1 with WebEngine 6.8.2/Chromium
122, and minimum library 6.6.2/Chromium 112. The minimum Anki environment uses
current-installation dependency fallbacks.

## Snapshot validation CPU, 2026-09-13

Profiling the 33-passage native workload attributed most saving time to strict
validation, not writing files: 3.679 seconds in `persist_snapshot`, including
1.763 seconds validating input and 1.768 seconds reading and validating stored
media, versus 0.050 seconds writing media. The full-book 121.7-second measurement
also includes the harness's additional readback; it is not the save call alone.

Reusing standard JSON encoders reduced process CPU in all twelve interleaved
pairs of the same 33 native saves. Total CPU fell from 18,327.355 to 11,631.492 ms
(36.53%); median fell from 1,498.427 to 962.568 ms. Every filename, byte and
decoded object remained identical. A separate full-corpus re-encode preserved
all 2,460 files and 25,865,594 bytes. Both add-on environments pass 388 tests;
the cross-language snapshot/example contracts pass 3 tests and 131 assertions.

The first sequential after-run was slower in elapsed time than its baseline
(median 1,707.934 versus 1,218.985 ms). Interleaving retains large elapsed
outliers too. Shared-machine wall time does not support a whole-workflow speedup
claim. The reports preserve those results alongside the CPU comparison in
[`tmp/anki-persist-profile.ZNB2uC/README.md`](../../tmp/anki-persist-profile.ZNB2uC/README.md).

`snapshot_persistence_measure.py` is the rerunnable native lever. Each round
uses a fresh empty collection and the same preloaded objects and timestamps.
Its timer covers only `persist_snapshot`, including internal verification;
outer readback and inventory checks are separate. Seven tests cover its input
ownership, limits, timing boundary, unchanged data and zero notes/cards/reviews.
These are background local-library measurements, not GUI, mobile or WAN timings.

## Candidate allocation and rich content, 2026-09-13

`offline_allocation_replay.py` compares all 2,464 captured _Botchan_ passages,
including ordered candidate payloads, parser pins and token spans. The report
`tmp/anki-coverage.vEp0Ff/production-pinned-first.json` freezes Python `51afb76d`
and SDK `ac17613f`. It compares the previous sequential allocation with one
primary candidate per token before alternatives, at the same size limits.

Prepared result records rise from 37,205 to 37,861; notes missing records fall
from 105 to 29. Encoded unique payloads total 25,856,910 bytes. This run projects
storage; it does not persist the new allocation through native Anki.

Actual SDK calls on changed notes find 478 additional expected primary
definitions but lose four. Earlier newly admitted records displace those four
later definitions. Contextual lookups gain 494 expected definitions and lose
140: 128 ambiguity misses from unequal alternative lists, four displaced
definitions and eight changed selections. A single-token snapshot supplies the
expected result, not a human linguistic label. More prepared records alone are
not evidence of better lookup quality. The follow-up compares safe result reuse
and primary reservation without increasing limits.

The input manifest freezes hashes, including the old bundle. Its default bundle
path now contains a newer artifact, so the replay refuses it. A repeat requires
a separate manifest pointing to the frozen artifact, not changing the recorded
hash. `test_offline_allocation_replay.py` exercises that refusal and comparison.

The later Python `77c36fa5` candidate retains dictionary usage examples,
section titles and publisher metadata that the projection previously dropped
even below its size limits. `test_offline_rich_content.py` checks exact field
bounds, malformed neighbors, native media readback and the rendered popup.
Its compiled-artifact check uses the public attachment API and observes both
gloss-bearing and usage-only senses with no network access:

```sh
python3 anki-addon/tests/offline_rich_content_fixture.py |
  node anki-addon/tests/offline_rich_content_probe.mjs --bundle anki-addon/src/web/shirabe-card.js
```

The `63f11895` artifact shows two senses, all three usage examples, their section
titles and publisher tags. Both supported Anki library environments pass all
13 rich-content tests with `SHIRABE_RICH_BUNDLE` set to that artifact. This is
content and native-storage evidence, not actual-reviewer gesture validation.

The `3f2e0e23` reader adds reuse of an existing ordered superset when saved word
contexts and complete candidate payloads agree. The two positive tests failed
with an unavailable-data result before the change, then passed. Tests also
reject conflicting hidden-language content, usage rows, titles, conjugation,
reading, part of speech, parser pins, empty results and alternative order.
`test_offline_allocation_replay.py` verifies definitions and alternatives with
the actual compiled artifact. Its earlier diagnostic expected the bug's miss;
that expectation changed after the source regressions passed.

This bundle also passes all 13 rich-content tests on both library environments.
Separate native saves explicitly report Anki 24.04 and 25.09.4; both preserve
the same 1,651 decoded bytes as 1,082 media bytes, hash `52c6a01d`, with zero
notes, cards or reviews. The minimum full-suite run without the dependency
fallback failed on missing `urllib3.exceptions`. With the documented hybrid
path, all 414 tests pass with the one optional artifact skip. The package's
README records the environment and rerun commands.

The actual `3f2e0e23` bundle replay covers all 4,842 content tokens in the 105
allocation-affected passages. It recovers all 128 contextual lookup failures;
116 also recover the full captured alternative list. No previously callable
definition, selected identity or alternative list changes, and primary lookups
remain identical. This is the reader change on fixed saved snapshots, not a
rerun of Python preparation or a linguistic quality score. The four primary
allocation losses and eight contextual selection differences remain. Frozen
bundles, token-level evidence and the rerunnable script are in
[`tmp/anki-patched-subset.Kamj6v/REPORT.md`](../../tmp/anki-patched-subset.Kamj6v/REPORT.md).

The later actual-popup audit in `tmp/anki-refresh-policy.25nlU6/REPORT.md`
distinguishes those comparison differences from the learner's click path. All
eight wrapped words keep their selected definition; their alternatives shrink.
One nested `なる` lookup changes its selection, while seven inflected nested
lookups already failed in both versions. The four primary losses are real
unavailable-data popups. A previous-callable-first reservation prototype on the
three affected notes recovers ten definitions and loses one, versus 21 gains
and four losses from initial pinned-first allocation. It is not a general
refresh guarantee.

The `b6ab804d` writer instead retains an unchanged prior, including a partial
one, if refreshing would remove a callable parser-selected definition.
`test_offline_refresh.py` has ten passing native tests on each actual Anki
library; five failed before the safeguard. Tests include cross-record reuse,
inactive answer content, changed scope, batch reporting and unchanged schedules.
Retaining the whole snapshot can postpone additions. It does not preserve every
unpinned alternative or change the initial-preparation allocation.

The real three-note witness in `tmp/anki-refresh-native.UIdXE4` retains all four
previously lost selected definitions on Anki 24.04 and 25.09.4. The current
`8a7e8e13` bundle decodes the retained native media and matches all 144
previously callable results. Three files remain at 68,008 bytes total, with no
new refresh media, notes, cards or reviews. Keeping those snapshots postpones
21 new token definitions. `candidate-8a7-replay-final.json` records actual
returned definitions, hashes and zero network/playback attempts. This witness
is terminal; it does not require another full-corpus replay.

The `8a7e8e13` popup also preserves API cross-references and their target reading
through nested navigation. The fresh relations-enabled capture in
`tmp/anki-relations-corpus.hSS8ti` covers all 2,464 passages with original
response bytes and one stack fingerprint. Its 142,313,708 decoded bytes become
26,987,806 unique compressed bytes (25.7 MiB), excluding other media and database
overhead. Median unique snapshot size is 9,879 bytes. 37,536 of 38,117 content
tokens have stored results, not a measured linguistic accuracy score; 167 notes
hit a byte limit and 1,817 hit the candidate limit.

Actual Anki 25.09.4 persistence verifies 2,392 files and 26,211,544 bytes for
2,396 passages before the unchanged 25 MiB collection limit stops it. The last
68 passages are not persisted. All filename hashes match and the collection
contains zero notes, cards or review rows. Relevant implementation hashes stay
fixed, while ten unrelated server files change during the run. Its capture and
audit must remain separate from older request shapes and allocation-only
comparisons. The candidate index records hashes and excludes a performance or
mobile claim.

## Retained-memory controls, 2026-09-13

The frozen `ac17613f` audit uses 33 real compressed snapshots, 512,478 stored
bytes and 2,614,909 decoded bytes. In 1,800 measured popup flips, retained JS
heap grew 1,135,465 bytes with normal word transitions and 269,287 without them.
The measurement follows forced GC; it is not peak memory or native Blink heap.

The page had no view, a zero-sized viewport and no delivered animation frames.
A CSS-only reproduction retained 300 detached roots without loading the SDK;
the transition-free control retained none. Suppressing the SDK transition
listener did not help, and its registrations and removals stayed balanced.
These controls do not justify a production lifecycle change or larger payload
limits. Full reports, executable probes and their safeguards are in
[`tmp/sdk-retained-memory.zvcs6t/README.md`](../../tmp/sdk-retained-memory.zvcs6t/README.md).

A separate feasibility probe delivered 351 native animation frames and one
completed CSS transition without a window. `QWebEnginePage.setVisible(true)`
must run after `loadFinished`; setting it before navigation was reset by the
load. All 34 safety observations reported the offscreen platform, zero windows
and zero top-level widgets. Audio stayed muted and network requests blocked.
The viewport remained zero-sized: this enables frame-aware memory controls,
not actual-client layout or gesture acceptance. The executable probe and final
`page-visible-setter-confirmation.json` are in `tmp/qt-page-visible.NalanO/`.
Current-bundle retained-memory and device gates remain open.

The completed `8a7e8e13` frame-aware audit selected 33 evenly spaced decoded-size
ranks from the fresh native media, totaling 359,023 stored and 1,914,620 decoded
bytes. After 66 warm-up and 600 measured flips, plus 300 idle frames, retained
JS heap grew 21,036 bytes for decoding/lookup and 72,073 for attachment/detachment.
These are successful component checks, not a popup or process-memory result.

The DOM control completed its measured flips but failed a final 240-frame wait
whose two-second deadline was too short; its final idle result is invalid.
The popup stopped on its first card, before a heap baseline. A one-card
diagnostic observed `sb-word-open` and a real 120 ms background-color transition,
but that transition stayed pending at time zero through 180 further native
frames. Reduced motion, forced colors, missing styles and failed lookup were
excluded; the engine's underlying cause remains unproven. Standalone opacity
animation does not prove the popup's background transition advances.

All runs are terminal. No 1,800-flip retry, CSS change or guard relaxation was
made. Full reports and rerunnable instruments are in
[`tmp/sdk-frame-memory.q86FTw/REPORT.md`](../../tmp/sdk-frame-memory.q86FTw/REPORT.md).
A real viewport/client check is still needed before interpreting popup retention.

## Empty cross-reference metadata, 2026-09-13

The fresh full-novel capture above includes optional empty reference arrays.
Removing only those arrays from its 8,040 distinct identify responses removes
1,658,980 uncompressed JSON bytes (4.98%). The frozen popup returned identical
full definitions for all 7,405 changed candidate objects; all 8,000 nonempty
targets, readings and labels remain intact.

| Frozen-response replay              | Unique compressed bytes | Passages fitting 25 MiB |
| ----------------------------------- | ----------------------: | ----------------------: |
| Original                            |              26,987,806 |                   2,396 |
| Compact already saved content       |              26,683,604 |                   2,417 |
| Compact responses before allocation |              26,896,640 |                   2,401 |

The last two persistence counts are modeled, not native save results. Repeated
empty keys already compress well: content-only compaction saves 1.13% on disk.
Reallocation spends most of that saving on additional content. It gains 146
primary lookups but loses 18 across 13 notes because earlier results now fit
before later results. No surviving pinned definition changes; four unpinned
selections change. These are availability counts, not linguistic quality scores.

The server now omits empty lists from compact responses; full-detail responses
retain their existing shape. Three API tests failed before the change, and the
[identify tests](../../test/controllers/api/v1/word_identify_test.rb) guard both
shapes and nonempty content. This does not raise a limit or rewrite saved notes.
The existing refresh guard rejects all 13 losing proposals in the narrow replay.
Actual native persistence also passes on Anki 24.04 and 25.09.4: each retains
the same 13 snapshot files (286,856 bytes), adds zero refresh media bytes and
preserves note fields and scheduling rows through close/reopen. The frozen
`8a7e8e13` SDK opens all 18 protected definitions from those retained files;
the corresponding proposed snapshots fail those same 18 controls. Reports and
the rerunnable native/SDK witness are in `tmp/anki-empty-refresh.KBHkCv/`.
The initial native witness failed because its baseline used fields before Anki
normalized them on insertion; the final runs read the saved baseline first.
That failed report remains separate from the two `*-final.json` results.

Physical-device control is a separate, fail-closed mode. Identify the attached
device first. Every later command requires the exact reported model; taps also
require an explicit physical-input flag and refuse unless AnkiDroid owns the
current UI dump. Physical mode uses the standard ADB server port 5037 unless
`--adb-port` overrides it.

```sh
android_root="$PWD/tmp/anki-android-current.xMKeTD"
serial=REPLACE_WITH_ADB_SERIAL
python3 anki-addon/tests/android_probe.py \
  --android-root "$android_root" --target physical --serial "$serial" info
python3 anki-addon/tests/android_probe.py \
  --android-root "$android_root" --target physical --serial "$serial" \
  --expected-model 'REPLACE_WITH_INFO_MODEL' ui
python3 anki-addon/tests/android_probe.py \
  --android-root "$android_root" --target physical --serial "$serial" \
  --expected-model 'REPLACE_WITH_INFO_MODEL' screenshot tmp/anki-physical.png
python3 anki-addon/tests/android_probe.py \
  --android-root "$android_root" --target physical --serial "$serial" \
  --expected-model 'REPLACE_WITH_INFO_MODEL' --allow-physical tap '猫'
```

Frozen inputs, exact witnesses and executable replays are in
[`tmp/anki-empty-relations.fPugwC/REPORT.md`](../../tmp/anki-empty-relations.fPugwC/REPORT.md).
The corpus replay is terminal. It is not a new capture from the changed server,
a wire-compression measurement, or a solution to the collection storage limit.

## Current feature-complete UX candidate, 2026-09-14 JST

The installable candidate and isolated four-note UX deck are indexed in
`tmp/anki-complete-current-v10/README.md`. The add-on archive SHA-256 is
`f1c0c52a662d332c116bd398fff1b0686f68d9ea035e07e09f63b4775e985d42` and its
shared SDK SHA-256 is
`fde2109b1f395bbacc8dc6ac4f44584f7cd3d7dff14751c5dabdc3ee93c9212e`.
The package audit found 24 members, stable source, and no runtime metadata or
populated credentials. The website and add-on copies of the bundle are
byte-identical.

The shared card typecheck and integration checks pass. The full offscreen add-on
suite passes all 450 tests, with 1 intentional skip, on Anki 25.09.4 and Anki
24.04. The current Cocoa run passes 414 tests with 5 intentional GUI skips.
Native backend fixture runs on both versions each pass 41 checks plus the study
corpus.
Offscreen layout checks cover settings, field mapping, material selection,
phone-template setup and removal, recording selection, editor lookup, single
and batch field previews, cleanup, preparation consent, and preparation
progress. Each fits an 800 × 800 available screen at 28 pt;
instructions scroll vertically, actions remain visible, and non-tabular content
does not scroll sideways. Rendered evidence is under `tmp/anki-ui-current-v3`,
`tmp/anki-consent-current-e4-fixed`, and `tmp/anki-consent-24-fixed`. This is Qt
component evidence, not native-client foreground acceptance.
The minimum-version corpus first exposed that the importer invented absent deck
and preset settings from newer Anki schemas. Focused tests failed before the fix;
the corrected run passes 620 corpus assertions and the complete 24.04 gate.

The prepared UX deck SHA-256 is
`6d67e3f42e3ffb88111c06ef9e01b346702a6d99ec8649e632b007619f3a83b3`.
Its cat card contains one prepared token, two public examples with attribution,
one 10,700-byte MP3 pronunciation and the current shared bundle. Native reimport
preserves all notes, cards, fields, snapshot data and media hashes. It has not
been installed in the user's regular profile. It was imported over the existing
temporary test note and normal AnkiWeb sync completed.

The isolated current-Desktop run in
`tmp/anki-complete-current-v10/desktop-final/desktop-saved-audio-report.json` passes all
18 checks. The popup opened the saved `猫 / ねこ / cat` entry and source details,
then a trusted click played the verified MP3 to 0.864 seconds. No note, card, or
review-log row changed. The same run against the M4A reached a QtWebEngine media
error 4, establishing a codec limitation rather than a missing gesture. The
production preparation path now prefers the exact licensed MP3 derivative when
metadata supplies it, while retaining M4A fallback for recordings without one.
Opening source details also scrolls the expanded references into the popup's
visible area. The current Desktop screenshot records readable, touch-sized
sentence, source, and license links instead of raw URLs.

The synced v10 card passes all 20 guarded AnkiWeb checks in
`tmp/anki-complete-current-v10/ankiweb-saved-audio-report.json`.
The browser loaded the frozen `fde2109b` bundle, opened the saved
`猫 / ねこ / cat` definition, displayed both Tatoeba references and their license,
recovered from missing and corrupt media, completed two verified MP3 plays,
reused its verified cache, and stopped the third play when the popup closed.
The run fetched the recording exactly three times and contacted Shirabe zero
times. It revealed the answer but did not grade the card. The matching screenshot
is `ankiweb-source-details.png` in the same directory.

The initial harness attempts attached to the user's Brave window instead of the
reserved window. Playwright's page and the visible workspace-10 tab therefore
did not match, and trusted input never reached the test page. Reattaching while
the reserved Brave window was active made the unchanged interaction path pass.
The failed reports remain beside the passing report as automation diagnostics,
not popup failures.

The latest Android emulator evidence is in `tmp/anki-complete-current-v8/`.
AnkiDroid imported the v8 APKG as one updated note and loaded SDK `1f65a44b` plus
the new prepared snapshot from collection media. Native taps opened the offline
definition, expanded source details with every URL and license visible, and
played the exact 10,700-byte MP3 through `ended` at 0.864 seconds. A cold launch
loaded the same bundle and snapshot and reopened the popup offline. A signed-out
card has no account control, while attaching the native bridge preserves the
live connection predicate. The stopped collection retains 37 exact notes, 38
card and scheduling rows unchanged from the prior acceptance, zero reviews, and
all 53 expected media files. The app is stopped. The v10 source-link change
still needs the same emulator acceptance pass. Physical Android remains open;
the emulator had no host audio and is not a physical-device or audible-quality
claim.
