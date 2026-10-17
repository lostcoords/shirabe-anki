import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import template_reviewer_check as runner
from gui_safety import offscreen_test_application


class ReleasePackage(unittest.TestCase):
    def test_verifies_the_packaged_bundle_and_refuses_runtime_data(self):
        bundle = b'fixture bundle, not the release SDK'
        digest = hashlib.sha256(bundle).hexdigest()
        cases = (
            ({}, digest, None),
            ({}, '0' * 64, 'Packaged bundle changed'),
            ({'meta.json': '{}'}, digest, 'runtime metadata'),
            ({'nested/.env.local': 'FIXTURE=value'}, digest, 'environment files'),
            ({'config.json': json.dumps({'cardToken': 'fake-test-token'})}, digest, 'credentials'),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.ankiaddon'
            for extra, expected, error in cases:
                with self.subTest(extra=extra, expected=expected):
                    entries = {'web/shirabe-card.js': bundle, 'config.json': '{}', **extra}
                    with zipfile.ZipFile(path, 'w') as archive:
                        for name, data in entries.items():
                            archive.writestr(name, data)
                    if error:
                        with self.assertRaisesRegex(ValueError, error):
                            runner.verify_package(path, expected)
                    else:
                        runner.verify_package(path, expected)

    def test_wrong_hash_stops_before_creating_a_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'must-not-be-created'
            result = subprocess.run([
                sys.executable, runner.__file__, '--bundle-sha256', '0' * 64,
                '--output', str(output), '--prepare-offline',
            ], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('no reviewer started', result.stderr)
            self.assertFalse(output.exists())

    def test_fixture_preserves_native_add_note_results_without_a_count_adapter(self):
        native_result = SimpleNamespace(changes=1)

        class Collection:
            def add_note(self, note, deck_id):
                return native_result

        def fixture():
            return Collection().add_note(object(), 1)

        bundle = Path(runner.__file__).resolve().parents[1] / 'src/web/shirabe-card.js'
        argv = [runner.__file__, '--bundle-sha256', hashlib.sha256(bundle.read_bytes()).hexdigest()]
        with patch.object(sys, 'argv', argv), \
                patch.dict(sys.modules, {'anki.collection': SimpleNamespace(Collection=Collection), 'aqt': SimpleNamespace()}), \
                patch.object(runner.desktop_check, 'create_fixture', side_effect=fixture), \
                patch.object(runner.desktop_check, 'main', side_effect=lambda: runner.desktop_check.create_fixture()):
            self.assertIs(runner.main(), native_result)


class PreparationMetrics(unittest.TestCase):
    def test_counts_saved_notes_and_unique_snapshot_bytes_over_the_measured_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory)
            (media / 'first.json').write_bytes('猫'.encode())
            (media / 'second.json').write_bytes(b'1234567')
            snapshots = {'basic': {'filename': 'first.json'}, 'ruby': {'filename': 'first.json'},
                         'cloze': {'filename': 'second.json'}}
            result = runner.preparation_metrics(5_000_000_000, 7_000_000_000, snapshots, media, 9)
        self.assertEqual(result['seconds'], 2)
        self.assertEqual(result['notes_saved'], 3)
        self.assertEqual(result['snapshot_files'], 2)
        self.assertEqual(result['snapshot_bytes'], 10)
        self.assertEqual(result['notes_per_second'], 1.5)
        self.assertEqual(result['snapshot_bytes_per_second'], 5)
        self.assertEqual(result['dictionary_requests'], 9)
        self.assertIn('user consent wait', result['excluded'])
        self.assertIn('loopback HTTP', result['conditions'])

    def test_refuses_missing_or_nonpositive_measurements(self):
        for start, finish in ((None, 1), (1, None), (1, 1), (2, 1)):
            with self.subTest(start=start, finish=finish), self.assertRaisesRegex(ValueError, 'timestamps'):
                runner.preparation_metrics(start, finish, {}, '.', 0)

    def test_completed_preparation_keeps_its_measurement_if_the_later_replay_fails(self):
        with patch.object(runner.desktop_check.Driver, '__init__', return_value=None):
            driver = runner.MeasuredDriver()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'snapshot.json').write_bytes(b'123456')
            driver.mw = SimpleNamespace(col=SimpleNamespace(media=SimpleNamespace(dir=lambda: directory)))
            driver.report = {}

            def prepared_then_failed():
                driver.prepare_started_ns = 5_000_000_000
                driver.prepare_finished_ns = 7_000_000_000
                driver.prepare_request_count = 2
                driver.report['native_prepared_snapshots'] = {'basic': {'filename': 'snapshot.json'}}
                raise RuntimeError('offline replay failed after apply')

            with patch.object(runner.desktop_check.Driver, 'prepare_native', side_effect=prepared_then_failed), \
                    patch.object(driver, 'measure_lookups', create=True):
                with self.assertRaisesRegex(RuntimeError, 'offline replay failed after apply'):
                    driver.prepare_native()
            self.assertEqual(driver.report['native_prepare_performance']['seconds'], 2)
            self.assertEqual(driver.report['native_prepare_performance']['snapshot_bytes'], 6)


class LookupMetrics(unittest.TestCase):
    def test_reports_raw_samples_and_nearest_rank_percentiles_without_rounding_them_away(self):
        samples = [{'elapsedMs': value / 10} for value in range(24, 0, -1)]
        result = runner.lookup_latencies(samples)
        self.assertEqual(result['samples'], 24)
        self.assertEqual(result['p50ms'], 1.2)
        self.assertEqual(result['p95ms'], 2.3)
        self.assertEqual(result['maxMs'], 2.4)
        self.assertEqual(result['raw'], samples)

    def test_refuses_too_few_samples_and_invalid_timings(self):
        for values in ([1] * 11, [1] * 11 + [math.nan], [1] * 11 + [-1], [1] * 11 + [math.inf],
                       [1] * 11 + [True], [1] * 11 + ['1'], [1] * 11 + [None]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                runner.lookup_latencies([{'elapsedMs': value} for value in values])

    def test_renderer_probe_requires_visible_definition_and_times_real_bridge_callback(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('template_lookup_probe_test.mjs'))],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('visible definition and native callback timing verified', result.stdout)


class NativeLookupBudgets(unittest.TestCase):
    def report(self):
        return {'native_lookup_performance': {
            mode: {temperature: runner.lookup_latencies([{'elapsedMs': ceiling}] * 12)
                   for temperature, ceiling in zip(('cold', 'warm'), ceilings)}
            for mode, ceilings in (('online', (300, 225)), ('prepared_offline', (60, 85)))
        }}

    def check_report(self, report):
        guard = '''
import builtins, runpy, socket, sys
original = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name.split('.')[0] in {'aqt', 'anki', 'PyQt6'}:
        raise RuntimeError('Report validation reached native imports')
    return original(name, *args, **kwargs)
class NoSocket(socket.socket):
    def __new__(cls, *args, **kwargs):
        raise RuntimeError('Report validation opened a socket')
socket.socket = NoSocket
builtins.__import__ = checked_import
sys.path.insert(0, sys.argv[1])
sys.argv = [sys.argv[2], '--check-performance-report', '-']
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        return subprocess.run([sys.executable, '-B', '-c', guard, str(Path(runner.__file__).parent), runner.__file__],
                              input=json.dumps(report), capture_output=True, text=True, timeout=5)

    def test_report_only_accepts_all_four_native_ceilings_without_native_imports_or_network(self):
        result = self.check_report(self.report())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Native fixture lookup latency budgets passed', result.stdout)
        self.assertIn('saved report only', result.stdout)

    def test_report_only_rejects_each_p95_above_its_native_ceiling(self):
        for mode, ceilings in (('online', (300, 225)), ('prepared_offline', (60, 85))):
            for temperature, ceiling in zip(('cold', 'warm'), ceilings):
                with self.subTest(mode=mode, temperature=temperature):
                    report = self.report()
                    report['native_lookup_performance'][mode][temperature] = runner.lookup_latencies(
                        [{'elapsedMs': ceiling + 0.01}] * 12)
                    result = self.check_report(report)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(f'{mode} {temperature} p95', result.stderr)
                    self.assertIn(f'{ceiling} ms', result.stderr)

    def test_report_only_rejects_absent_or_malformed_measurements(self):
        for missing in ((), ('native_lookup_performance',), ('native_lookup_performance', 'online'),
                        ('native_lookup_performance', 'prepared_offline')):
            for value in (None, {}, []):
                with self.subTest(missing=missing, value=value):
                    report = self.report()
                    parent = report
                    for key in missing[:-1]:
                        parent = parent[key]
                    if missing:
                        parent[missing[-1]] = value
                    else:
                        report = value
                    result = self.check_report(report)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('native_lookup_performance', result.stderr)
        for temperature in ('cold', 'warm'):
            for field in (None, 'samples', 'p50ms', 'p95ms', 'maxMs', 'raw'):
                with self.subTest(temperature=temperature, field=field):
                    report = self.report()
                    mode = report['native_lookup_performance']['prepared_offline']
                    if field:
                        del mode[temperature][field]
                    else:
                        del mode[temperature]
                    result = self.check_report(report)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(f'prepared_offline {temperature}', result.stderr)

    def test_report_only_rejects_invalid_or_inconsistent_summary_and_raw_samples(self):
        bad_fields = {
            'samples': (11, 13, 12.0, True, '12'),
            'p50ms': (None, -1, math.nan, math.inf, True, '300', 299),
            'p95ms': (None, -1, math.nan, math.inf, True, '300', 299),
            'maxMs': (None, -1, math.nan, math.inf, True, '300', 299),
            'raw': (None, {}, [], [{'elapsedMs': 300}] * 11, [{}] * 12,
                    [None] * 12, [{'elapsedMs': True}] * 12, [{'elapsedMs': math.nan}] * 12),
        }
        for field, values in bad_fields.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    report = self.report()
                    report['native_lookup_performance']['online']['cold'][field] = value
                    result = self.check_report(report)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('online cold', result.stderr)
                    self.assertNotIn('Traceback', result.stderr)

    def test_report_only_reads_a_file_without_a_bundle_or_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            path.write_text(json.dumps(self.report()))
            result = subprocess.run([sys.executable, '-B', runner.__file__, '--check-performance-report', str(path)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_completed_native_measurement_enforces_budgets(self):
        with patch.object(runner.desktop_check.Driver, '__init__', return_value=None):
            driver = runner.MeasuredDriver()
        driver.report = self.report()
        driver.report['native_lookup_performance']['prepared_offline']['warm'] = runner.lookup_latencies(
            [{'elapsedMs': 86}] * 12)
        with patch.object(runner.desktop_check.Driver, 'prepare_native'), patch.object(driver, 'measure_lookups'):
            with self.assertRaisesRegex(ValueError, 'prepared_offline warm p95'):
                driver.prepare_native()


class NativeFixtureMetadata(unittest.TestCase):
    def test_loopback_preferences_and_notices_survive_preparation_with_real_show_default(self):
        import support
        from shirabe_addon import offline, offline_ui

        server, requests = runner.desktop_check.fixture_api()
        try:
            backend = offline.DictionaryBackend(f'http://127.0.0.1:{server.server_port}',
                                               json.dumps({'access': 'fixture-only', 'expiresAt': 2000}), now=lambda: 1)
            preferences = backend.me()['preferences']
            self.assertEqual(preferences['dictionaryReveal'], {'jmdict': 'show'})
            sources = backend.dictionaries()['dictionaries']
            batch = offline_ui.prepare_batch([offline_ui.CapturedNote(1, 'fixture', (), None, None)], {1: ['猫']},
                                             backend, preferences=preferences, approved={'jmdict'}, sources=sources)
            self.assertEqual(len(batch.rows), 1)
            snapshot = batch.rows[0][1].snapshot
            self.assertEqual(offline.validate_snapshot(snapshot), snapshot)
            self.assertEqual(snapshot['preferences'], {'languages': ['en'], 'dictionaryReveal': {'jmdict': 'show'}})
            self.assertEqual(snapshot['sources'][0]['source'], {
                'name': 'Deterministic test data', 'url': 'https://example.test', 'license': 'CC0-1.0',
            })
            self.assertNotIn('entryCount', snapshot['sources'][0])
            self.assertEqual(len(requests), 6)
        finally:
            server.shutdown()
            server.server_close()


@unittest.skipUnless(importlib.util.find_spec('aqt'), 'Requires native Qt, but no Anki profile or reviewer')
class NativePreparationTiming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = offscreen_test_application(['shirabe-timing-tests'])

    def test_native_dialog_signals_exclude_consent_wait_and_result_acknowledgement(self):
        from aqt.qt import QDialog
        with patch.object(runner.desktop_check.Driver, '__init__', return_value=None):
            driver = runner.MeasuredDriver()
        workflow = QDialog()
        consent = QDialog(workflow)
        consent.setWindowTitle('Prepare for offline lookup')
        driver.app = SimpleNamespace(activeModalWidget=lambda: consent)
        driver.prepare_consent_pending = True
        driver.requests = ['metadata before consent']
        try:
            with patch.object(runner.desktop_check.Driver, 'modal_tick'):
                driver.modal_tick()
            self.assertIsNone(driver.prepare_started_ns)
            self.assertIsNone(driver.prepare_finished_ns)
            with patch.object(runner.time, 'monotonic_ns', side_effect=[5_000_000_000, 7_000_000_000]):
                consent.accept()
                self.assertEqual(driver.prepare_started_ns, 5_000_000_000)
                self.assertIsNone(driver.prepare_finished_ns)
                driver.requests.extend(['parse', 'identify'])
                workflow.accept()
            result_dialog = QDialog()
            driver.requests.append('later offline replay probe')
            result_dialog.accept()
            result_dialog.deleteLater()
            self.assertEqual(driver.prepare_finished_ns, 7_000_000_000)
            self.assertEqual(driver.prepare_request_count, 2)
        finally:
            workflow.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
