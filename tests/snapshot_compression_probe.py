"""Compare stored snapshot media with the frozen SDK in explicitly offscreen Qt."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest

from gui_safety import offscreen_test_application


ROUNDS = 12
MAX_BYTES = 128 * 1024
BUNDLE = Path(__file__).resolve().parents[1] / "addon/src/web/shirabe-card.js"
CSP = "default-src 'none'; base-uri 'none'; form-action 'none'"

PROBE = r"""
(async () => {
  const result = {ok: false, rounds: [], userAgent: navigator.userAgent,
    decompressionStream: typeof DecompressionStream === 'function', forbiddenCalls: []};
  window.probeResult = null;
  const refuse = name => { result.forbiddenCalls.push(name); throw Error('Forbidden API: ' + name); };
  window.fetch = () => refuse('fetch');
  const canonical = value => {
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    if (value !== null && typeof value === 'object') return '{' + Object.keys(value).sort()
      .map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
    return JSON.stringify(value);
  };
  const equal = (actual, expected, label) => {
    if (canonical(actual) !== canonical(expected)) throw Error(label + ' differs from Python-validated data');
  };
  const bytes = Uint8Array.from(atob(fixture.stored), character => character.charCodeAt(0));
  const response = () => new Response(bytes, {headers: {'Content-Length': String(bytes.length)}});
  const requests = [];
  const fetchStored = async input => {
    if (input !== fixture.filename) throw Error('Unexpected media filename');
    requests.push(input);
    return response();
  };
  try {
    const SDK = window.ShirabeCard;
    if (typeof SDK?.loadSnapshot !== 'function' || typeof SDK?.snapshotHost !== 'function') {
      throw Error('The frozen bundle must export loadSnapshot and snapshotHost');
    }
    for (let round = 0; round < fixture.rounds; round++) {
      const started = performance.now();
      const loaded = await SDK.loadSnapshot(fixture.filename, {fetch: fetchStored});
      const loadedAt = performance.now();
      equal(loaded, fixture.expected, 'loadSnapshot round ' + (round + 1));
      const checkedAt = performance.now();
      let tokenizations = 0, lookups = 0;
      for (const segment of fixture.expected.segments) {
        const host = SDK.snapshotHost(undefined, loaded, {
          contentVersion: fixture.expected.contentVersion, apiBase: 'https://snapshot-probe.invalid',
          texts: [segment.text], fetch: () => refuse('snapshotHost fetch'),
        });
        equal(await host.tokenize(segment.text), segment.tokens, 'snapshotHost tokens');
        tokenizations++;
        const prepared = segment.results.find(row => row.candidates.length > 0);
        if (prepared) {
          const token = segment.tokens[prepared.tokenIndex];
          const candidate = prepared.candidates.find(value => value.dictionary + ':' + value.sourceId === token.entry)
            || prepared.candidates[0];
          const entry = candidate.dictionary + ':' + candidate.sourceId;
          const found = await host.lookupEntry(entry, token.surface);
          if (found.status !== 'found' || found.definition?.entry !== entry
              || found.definition.headword !== candidate.headword || found.definition.offline !== true
              || found.definition.preparedAt !== fixture.expected.createdAt) throw Error('snapshotHost pinned lookup differs');
          equal(found.candidates.map(value => value.entry), prepared.candidates.map(value => value.dictionary + ':' + value.sourceId),
            'snapshotHost candidates');
          lookups++;
        }
      }
      equal(loaded, fixture.expected, 'snapshotHost must not mutate the loaded snapshot');
      result.rounds.push({loadMs: loadedAt - started, equalityMs: checkedAt - loadedAt,
        hostMs: performance.now() - checkedAt, tokenizations, lookups});
    }
    if (requests.length !== fixture.rounds) throw Error('Expected one media read per load');
    const corrupt = bytes.slice();
    corrupt[corrupt.length - 1] ^= 1;
    equal(await SDK.loadSnapshot(fixture.filename, {fetch: async () => new Response(null, {status: 404})}), null, 'Missing media');
    equal(await SDK.loadSnapshot(fixture.filename, {fetch: async () => new Response(corrupt)}), null, 'Corrupt media hash');
    const decompression = Object.getOwnPropertyDescriptor(window, 'DecompressionStream');
    try {
      Object.defineProperty(window, 'DecompressionStream', {configurable: true, value: undefined});
      equal(await SDK.loadSnapshot(fixture.filename, {fetch: async () => response()}),
        fixture.version === 2 ? null : fixture.expected, 'Missing decompression API');
    } finally {
      if (decompression) Object.defineProperty(window, 'DecompressionStream', decompression);
      else delete window.DecompressionStream;
    }
    if (result.forbiddenCalls.length) throw Error('An unexpected API was called');
    const samples = result.rounds.map(round => round.loadMs).sort((a, b) => a - b);
    result.loadTimingMs = {samples: samples.length, minimum: samples[0],
      median: (samples[5] + samples[6]) / 2, p95: samples[Math.ceil(samples.length * .95) - 1],
      maximum: samples[samples.length - 1]};
    result.rejections = {missingMedia: true, corruptStoredHash: true,
      missingDecompressionApi: fixture.version === 2 ? 'rejected' : 'legacy-readable'};
    result.storedFetches = requests.length;
    result.lookupScope = 'One pinned candidate per nonempty segment per round; every segment tokenized';
    result.ok = true;
  } catch (error) { result.error = String(error); }
  window.probeResult = result;
})();
"""


def read_regular(path, maximum):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise ValueError(f"Expected a regular file containing 1..{maximum} bytes: {path}")
        data = source.read(maximum + 1)
    if not 0 < len(data) <= maximum:
        raise ValueError(f"File changed or exceeds {maximum} bytes: {path}")
    return data


def run_page(app, bundle, fixture):
    from aqt.qt import QCoreApplication, QEvent, QEventLoop, QTimer
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor

    blocked, errors = [], []

    class BlockRequests(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, request):
            if (request.requestUrl().scheme() in ("data", "about")
                    and request.resourceType() == request.ResourceType.ResourceTypeMainFrame):
                return
            blocked.append(request.requestUrl().scheme())
            request.block(True)

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):
            if level == self.JavaScriptConsoleMessageLevel.ErrorMessageLevel and len(errors) < 20:
                errors.append(message)

    profile = QWebEngineProfile()
    if not profile.isOffTheRecord():
        raise RuntimeError("The probe requires an in-memory WebEngine profile")
    interceptor = BlockRequests(profile)
    profile.setUrlRequestInterceptor(interceptor)
    page = Page(profile)
    page.setAudioMuted(True)
    loop = QEventLoop()
    poll_timer = QTimer()
    deadline = QTimer()
    deadline.setSingleShot(True)
    result = {}
    pending = False

    def received(value):
        nonlocal pending
        pending = False
        if value and not result:
            try:
                result.update(json.loads(value))
            except (TypeError, ValueError) as error:
                result.update(ok=False, error=f"Invalid JavaScript report: {error}")
            loop.quit()

    def poll():
        nonlocal pending
        if not pending and not result:
            pending = True
            page.runJavaScript("window.probeResult ? JSON.stringify(window.probeResult) : ''", received)

    def loaded(ok):
        if not ok:
            result.update(ok=False, error="The isolated HTML page failed to load")
            loop.quit()
            return
        script = "window.fetch = () => { throw Error('Network disabled'); };\n" + bundle
        script += "\n;const fixture = " + json.dumps(fixture, ensure_ascii=True) + ";\n" + PROBE
        page.runJavaScript(script, lambda _: poll_timer.start(10))

    page.loadFinished.connect(loaded)
    poll_timer.timeout.connect(poll)
    deadline.timeout.connect(loop.quit)
    try:
        page.setHtml(f'<meta http-equiv="Content-Security-Policy" content="{CSP}"><title>Offscreen snapshot probe</title>')
        deadline.start(30000)
        loop.exec()
    finally:
        poll_timer.stop()
        deadline.stop()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    if not result:
        result.update(ok=False, error="Offscreen probe timed out after 30 seconds")
    result.update(blockedRequestSchemes=blocked, consoleErrors=errors)
    if blocked or errors:
        result.update(ok=False, error="Unexpected request or JavaScript console error")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
        epilog="Requires Anki's Python and explicit QT_QPA_PLATFORM=offscreen. Does not build or install the SDK.")
    parser.add_argument("snapshot", type=Path, help="Existing hash-named Shirabe v1/v2 media file, at most 128 KiB")
    parser.add_argument("--output", type=Path, required=True, help="New JSON report file in an existing directory; never overwritten")
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink() or not args.output.parent.is_dir():
        parser.error("--output must be a new file in an existing directory")
    if os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING") or "remote-debugging" in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""):
        parser.error("Remote debugging must be disabled for this isolated probe")
    try:
        app = offscreen_test_application(["shirabe-snapshot-compression-probe"])
    except unittest.SkipTest as error:
        parser.error(str(error))

    import support
    from shirabe_addon import offline
    from aqt.qt import qVersion
    from anki.buildinfo import version

    try:
        media = SimpleNamespace(dir=lambda: str(args.snapshot.absolute().parent))
        expected = offline.read_snapshot(media, args.snapshot.name)
        if expected is None:
            raise ValueError("offline.read_snapshot rejected the snapshot; use valid, owned v1/v2 media")
        stored = read_regular(args.snapshot, MAX_BYTES)
        digest = hashlib.sha256(stored).hexdigest()
        if digest != offline.MEDIA_NAME.fullmatch(args.snapshot.name)[1]:
            raise ValueError("The stored snapshot changed after Python validation")
        bundle = read_regular(BUNDLE, 2 * 1024 * 1024)
        wire = json.loads(stored)
        storage_version = 2 if args.snapshot.name.startswith("_shirabe_snapshot_v2_") else 1
        decoded_bytes = wire["decodedBytes"] if storage_version == 2 else len(stored)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    fixture = {"filename": args.snapshot.name, "stored": base64.b64encode(stored).decode("ascii"),
               "expected": expected, "version": storage_version, "rounds": ROUNDS}
    report = run_page(app, bundle.decode("utf-8"), fixture)
    report.update({"scope": "Explicitly offscreen Qt API probe only; not native-client, GUI, review, layout, or full-corpus validation. "
                            "No view, show, attach, network, playback, clipboard, collection, account, or bundle build.",
                   "snapshot": str(args.snapshot.absolute()), "storageVersion": storage_version,
                   "storedSha256": digest, "storedBytes": len(stored), "decodedBytes": decoded_bytes,
                   "bundle": str(BUNDLE), "bundleSha256": hashlib.sha256(bundle).hexdigest(), "bundleBytes": len(bundle),
                   "expectedReader": "offline.read_snapshot", "roundsRequested": ROUNDS,
                   "qt": qVersion(), "qtPlatform": app.platformName(), "anki": version,
                   "python": sys.version, "csp": CSP})
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"ok": report["ok"], "output": str(args.output.absolute()), "bundleSha256": report["bundleSha256"]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
