"""Run desktop_check with a frozen bundle and native preparation/lookup timing.

Use the target Anki Python, --bundle-sha256 HASH, and desktop_check's flags.
Use --check-performance-report REPORT_JSON for saved timings without a client.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch
import zipfile

import desktop_check


NATIVE_LOOKUP_P95_MS = {
    'online': {'cold': 300, 'warm': 225},
    'prepared_offline': {'cold': 60, 'warm': 85},
}


def valid_milliseconds(value):
    return type(value) in (int, float) and 0 <= value < math.inf


def lookup_latencies(samples):
    if (not isinstance(samples, list) or len(samples) < 12
            or any(not isinstance(sample, dict) or not valid_milliseconds(sample.get('elapsedMs')) for sample in samples)):
        raise ValueError('Lookup timing requires at least 12 finite nonnegative samples')
    values = sorted(sample['elapsedMs'] for sample in samples)
    return {'samples': len(values), 'p50ms': values[math.ceil(len(values) * .5) - 1],
            'p95ms': values[math.ceil(len(values) * .95) - 1], 'maxMs': values[-1], 'raw': samples}


def check_lookup_performance(report):
    measurements = report.get('native_lookup_performance') if isinstance(report, dict) else None
    if not isinstance(measurements, dict):
        raise ValueError('Missing or malformed native_lookup_performance')
    for mode, ceilings in NATIVE_LOOKUP_P95_MS.items():
        measurement = measurements.get(mode)
        if not isinstance(measurement, dict):
            raise ValueError(f'Missing or malformed native_lookup_performance {mode}')
        for temperature, ceiling in ceilings.items():
            label = f'native_lookup_performance {mode} {temperature}'
            summary = measurement.get(temperature)
            if not isinstance(summary, dict):
                raise ValueError(f'Missing or malformed {label}')
            try:
                actual = lookup_latencies(summary.get('raw'))
            except ValueError as error:
                raise ValueError(f'{label}: {error}') from error
            if type(summary.get('samples')) is not int or summary['samples'] != actual['samples']:
                raise ValueError(f'{label}: samples must match the raw sample count')
            for key in ('p50ms', 'p95ms', 'maxMs'):
                value = summary.get(key)
                if not valid_milliseconds(value) or value != actual[key]:
                    raise ValueError(f'{label}: {key} must match the finite nonnegative raw measurement')
            if actual['p95ms'] > ceiling:
                raise ValueError(f'{mode} {temperature} p95 {actual["p95ms"]} ms exceeds native fixture ceiling {ceiling} ms')


def verify_package(path, expected_bundle):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if any(Path(name).name == 'meta.json' or Path(name).name.startswith('.env') for name in names):
            raise ValueError('Release package contains runtime metadata or environment files')
        defaults = json.loads(archive.read('config.json'))
        if any(defaults.get(key) for key in ('cardToken', 'accessToken', 'refreshToken', 'installId')):
            raise ValueError('Release package contains runtime credentials or installation identity')
        actual = hashlib.sha256(archive.read('web/shirabe-card.js')).hexdigest()
        if actual != expected_bundle:
            raise ValueError(f'Packaged bundle changed: expected {expected_bundle}, got {actual}')


def preparation_metrics(start_ns, finish_ns, snapshots, media_dir, request_count):
    if start_ns is None or finish_ns is None or finish_ns <= start_ns:
        raise ValueError('Native consent/apply timestamps were not recorded')
    seconds = (finish_ns - start_ns) / 1_000_000_000
    filenames = {row['filename'] for row in snapshots.values()}
    size = sum((Path(media_dir) / name).stat().st_size for name in filenames)
    return {
        'seconds': seconds, 'notes_saved': len(snapshots), 'snapshot_files': len(filenames),
        'snapshot_bytes': size, 'notes_per_second': len(snapshots) / seconds,
        'snapshot_bytes_per_second': size / seconds, 'dictionary_requests': request_count,
        'start': 'Native consent dialog accepted',
        'finish': 'Native workflow finished after apply',
        'excluded': ['metadata and initial collection reads', 'user consent wait', 'result acknowledgement', 'APKG export and offline replay'],
        'conditions': 'Real AnkiQt DOM capture and collection writes; synthetic dictionary on loopback HTTP; no simulated network latency; no sync or user account. Small fixture measurement, not a bulk or production-network benchmark.',
    }


class MeasuredDriver(desktop_check.Driver):
    def __init__(self, *args):
        self.prepare_started_ns = self.prepare_finished_ns = None
        self.prepare_request_count = 0
        super().__init__(*args)

    def modal_tick(self):
        dialog = self.app.activeModalWidget()
        if dialog and dialog.windowTitle() == 'Prepare for offline lookup' and self.prepare_consent_pending:
            dialog.accepted.connect(self.consent_accepted)
            dialog.parent().finished.connect(self.preparation_finished)
        super().modal_tick()

    def consent_accepted(self):
        self.prepare_started_ns = time.monotonic_ns()
        self.prepare_request_count = len(self.requests)

    def preparation_finished(self, *_):
        self.prepare_finished_ns = time.monotonic_ns()
        self.prepare_request_count = len(self.requests) - self.prepare_request_count

    def prepare_native(self):
        self.measure_lookups('online')
        try:
            super().prepare_native()
        finally:
            if 'native_prepared_snapshots' in self.report:
                self.report['native_prepare_performance'] = preparation_metrics(
                    self.prepare_started_ns, self.prepare_finished_ns, self.report['native_prepared_snapshots'],
                    self.mw.col.media.dir(), self.prepare_request_count,
                )
        self.measure_lookups('prepared_offline')
        check_lookup_performance(self.report)

    def collection_fingerprint(self):
        result = {}
        for table in ('notes', 'cards', 'revlog'):
            rows = self.mw.col.db.all(f'select * from {table} order by id')
            result[table] = {'rows': len(rows), 'sha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest()}
        return result

    def measure_lookups(self, mode):
        from aqt.qt import qVersion

        prepared = mode == 'prepared_offline'
        baseline = self.collection_fingerprint()
        report = self.report.setdefault('native_lookup_performance', {
            'clock': 'Renderer performance.now(), captured click event to first animation frame with expected visible headword and gloss',
            'cold': 'First lookup in a fresh reviewer document and SDK host; not cold app startup or cold filesystem cache',
            'warm': 'Same host and target reopened after dismissal; no host reset between the pair',
            'conditions': 'Actual AnkiQt reviewer and production SDK; QTest mouse gesture; synthetic loopback HTTP API with no added network delay; collection-state bridge remains enabled',
            'limits': 'First visible definition includes frame sampling delay, not a wait for study metadata. Native collection-state replies are checked separately. Not compositor presentation time, production-network latency, heap usage or Android p95.',
            'qt_runtime': qVersion(), 'qt_platform': self.app.platformName(), 'python': sys.version,
        })
        measurement = report[mode] = {'case': 'basic', 'collection_before': baseline, 'cold_samples': [], 'warm_samples': [], 'screenshots': []}
        script = Path(__file__).with_name('template_lookup_probe.js').read_text()
        if bool(getattr(self.server, 'fixture_offline', False)) != prepared:
            raise AssertionError('Loopback availability does not match the lookup benchmark mode')
        for index in range(12):
            before_render = len(self.requests)
            self.mw.reviewer._initWeb()
            self.settle()
            self.show('basic')
            ready = self.wait(lambda: self.js('!!(window.ShirabeCard && window.ShirabeAnki && window.__shirabe?.native)'), 'native benchmark attachment')
            if not ready or len(self.requests) != before_render:
                raise AssertionError('Lookup benchmark sent API requests before the gesture')
            if not self.js(script):
                raise AssertionError('Native lookup observer did not install')
            for temperature in ('cold', 'warm'):
                request_start = len(self.requests)
                self.js("window.__shirabeLookupTiming.arm('猫', 'cat')")
                self.raw_mouse('猫', click=True)
                sample = self.wait(lambda: self.js('window.__shirabeLookupTiming.result'), 'visible native definition timing', 12)
                if sample.get('error'):
                    raise AssertionError(sample['error'])
                first_definition_ms = sample['elapsedMs']
                sample = self.wait(lambda: self.js('''(() => {
                  const sample = window.__shirabeLookupTiming.result;
                  return sample?.collectionStates?.length && sample.collectionStates.every(state => state.completed)
                    ? sample : null;
                })()'''), 'native study reply after definition', 5)
                if sample['elapsedMs'] != first_definition_ms:
                    raise AssertionError('Study metadata changed the first-definition timestamp')
                states = sample.get('collectionStates', [])
                if not states or any(not state.get('completed') or not isinstance(state.get('states'), dict) for state in states):
                    raise AssertionError('Lookup did not receive a completed native collection-state reply')
                sample['requests'] = self.requests[request_start:]
                sample['sample'] = index + 1
                sample['note_id'] = self.mw.reviewer.card.nid
                if prepared or temperature == 'warm':
                    if sample['requests']:
                        raise AssertionError('Prepared or warm lookup unexpectedly requested the dictionary API')
                else:
                    paths = [request['path'] for request in sample['requests']]
                    if '/api/v1/me' not in paths or not any(path.startswith('/api/v1/words/identify') for path in paths):
                        raise AssertionError('Cold online lookup did not fetch preferences and a definition')
                measurement[temperature + '_samples'].append(sample)
                if index == 0:
                    name = f'desktop-lookup-{mode}-{temperature}'
                    self.screenshot(name)
                    measurement['screenshots'].append(str(self.root / f'{name}.png'))
                self.js("document.querySelector('.sb-host').shadowRoot.querySelector('.sb-close').click()")
                self.wait(lambda: self.js("Array.from(document.querySelectorAll('.sb-host')).every(h=>!h.shadowRoot.querySelector('.sb-visible') && Array.from(h.shadowRoot.querySelectorAll('.sb-popup')).every(p=>Number(getComputedStyle(p).opacity)===0))"), 'benchmark popup dismissal')
            self.js('window.__shirabeLookupTiming.dispose()')
        for temperature in ('cold', 'warm'):
            measurement[temperature] = lookup_latencies(measurement.pop(temperature + '_samples'))
        measurement['collection_after'] = self.collection_fingerprint()
        measurement['collection_unchanged'] = measurement['collection_after'] == baseline
        if not measurement['collection_unchanged']:
            raise AssertionError('Lookup-only samples changed notes, cards or review history')
        print(json.dumps({'native_lookup_performance': mode,
                          'cold_p95ms': measurement['cold']['p95ms'], 'warm_p95ms': measurement['warm']['p95ms']}), flush=True)


def main():
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--bundle-sha256')
    mode.add_argument('--check-performance-report', metavar='REPORT_JSON')
    args, remaining = parser.parse_known_args()
    if args.check_performance_report is not None:
        if remaining:
            parser.error('Report-only validation does not accept reviewer arguments')
        try:
            text = sys.stdin.read() if args.check_performance_report == '-' else Path(args.check_performance_report).read_text()
            check_lookup_performance(json.loads(text))
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print('Native fixture lookup latency budgets passed (saved report only; no current SDK validation)')
        return 0
    if not re.fullmatch('[a-f0-9]{64}', args.bundle_sha256):
        parser.error('--bundle-sha256 requires the full lowercase hash announced by main')
    bundle = Path(__file__).resolve().parents[1] / 'addon/src/web/shirabe-card.js'
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != args.bundle_sha256:
        parser.error('The local bundle does not match the announced release hash; no reviewer started')
    try:
        import aqt
    except ImportError as error:
        parser.error(f'The selected Python cannot load AnkiQt; no interpreter fallback: {error}')
    sys.argv = [sys.argv[0], *remaining]

    package = desktop_check.package

    def checked_package(root):
        path = package(root)
        verify_package(path, args.bundle_sha256)
        return path

    with patch.object(desktop_check, 'package', checked_package), \
            patch.object(desktop_check, 'Driver', MeasuredDriver):
        return desktop_check.main()


if __name__ == '__main__':
    raise SystemExit(main())
