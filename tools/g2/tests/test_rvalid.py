# SPDX-License-Identifier: Apache-2.0
"""Registered ascending searches consume sealed G2 decisions, never new gates."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RvalidTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.plan = dict(schema='hng-m7-rvalid-plan-v1', registration_id='synthetic-plan',
            scope='software-only', composition='X', config_sha256='a'*64,
            ladder_bps_l1=[20_000_000_000, 25_000_000_000, 30_000_000_000],
            observation_seconds=600, confirmation_runs=1, confirmation_fallbacks=1, offer_rule='registered-offer-rule',
            cells=[dict(direction=str(p), line='cable', phase='measure') for p in (0, 1)])
        self.index = dict(schema='hng-m7-rvalid-evidence-v1', registration_id='synthetic-plan',
                          trials=[], confirmation=None)

    def bundle(self, run, status='PASS', *, changes=None):
        rows = [dict(cell, required=True, repeat=0, status='INVALID' if status=='INCONCLUSIVE' else status,
            measurement_status=status, gates={g:dict(status='PASS') for g in ('median','p99','order','storage')},
            reason_codes=[], observed_failures=[], offered_bps_l1=20_000_000_000,
            measurement_window=dict(start_ns=0, end_ns=600_000_000_000, clock_id='common'))
            for cell in self.plan['cells']]
        if status == 'FAIL':
            rows[0]['gates']['p99']['status'] = 'FAIL'
            rows[0]['observed_failures'] = ['p99']
        elif status == 'INCONCLUSIVE':
            rows[0]['gates']['p99']['status'] = 'INCONCLUSIVE'
            rows[0]['reason_codes'] = ['insufficient_main_samples']
        value = dict(schema='hng-g2-report-v1', run=run, scope=self.plan['scope'],
            status='INVALID' if status=='INCONCLUSIVE' else status, measurement_status=status,
            reason_codes=[], rows=rows, sources=dict(config_sha256=self.plan['config_sha256']),
            registration=dict(scope=self.plan['scope']), counters=None)
        if changes:
            changes(value)
        raw = (json.dumps(value, allow_nan=False)+'\n').encode()
        path = self.root/(run+'.json')
        path.write_bytes(raw)
        return dict(bundle=path.name, sha256=hashlib.sha256(raw).hexdigest())

    def trial(self, index, status='PASS', changes=None):
        rate = self.plan['ladder_bps_l1'][index]
        def fill(b):
            for row in b['rows']:
                row['offered_bps_l1'] = rate
            if changes:
                changes(b)
        entry = dict(rate_bps_l1=rate, **self.bundle('trial-'+str(index), status, changes=fill))
        if (self.root/entry['bundle']).is_file():
            try:
                entry['offer'] = self.offer(entry)
            except (KeyError, TypeError):
                entry['offer'] = None
        self.index['trials'].append(entry)
        return entry

    def confirm(self, index, status='PASS', changes=None, *, fallback=False):
        rate = self.plan['ladder_bps_l1'][index]
        def fill(b):
            for row in b['rows']:
                row['offered_bps_l1'] = rate
            if changes:
                changes(b)
        entry = dict(rate_bps_l1=rate, **self.bundle('fallback' if fallback else 'confirmation', status, changes=fill))
        entry['offer'] = self.offer(entry)
        self.index['fallback_confirmation' if fallback else 'confirmation'] = entry

    def offer(self, entry, mutate=None):
        bundle = json.loads((self.root/entry['bundle']).read_text())
        windows = {row['phase']:row.get('measurement_window') for row in bundle['rows']}
        for phase, window in windows.items():
            if window is None:
                windows[phase] = next(w for w in bundle['registration']['windows'] if w['phase']==phase)
        doc = dict(schema='hng-m7-offer-v1', run=bundle['run'],
            plan_sha256=hashlib.sha256((json.dumps(self.plan)+'\n').encode()).hexdigest(),
            bundle_sha256=entry['sha256'], composition=self.plan['composition'],
            rate_bps_l1=entry['rate_bps_l1'], rule=self.plan['offer_rule'], status='PASS',
            windows=windows, directions=[dict(direction=str(p),target_bps_l1=entry['rate_bps_l1'],
                measured_bps_l1=entry['rate_bps_l1']) for p in (0,1)])
        if mutate:
            mutate(doc)
        path = self.root/(entry['bundle']+'.offer.json')
        raw = (json.dumps(doc)+'\n').encode()
        path.write_bytes(raw)
        return dict(path=path.name,sha256=hashlib.sha256(raw).hexdigest())

    def invoke(self):
        for name,value in (('plan.json',self.plan),('evidence.json',self.index)):
            (self.root/name).write_text(json.dumps(value)+'\n')
            if name == 'plan.json':
                self.index['plan_sha256'] = hashlib.sha256((self.root/name).read_bytes()).hexdigest()
        process = subprocess.run([sys.executable, '-m', 'tools.g2.rvalid',
            str(self.root/'plan.json'), str(self.root/'evidence.json')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(len(process.stdout.splitlines()), 1, process.stdout+process.stderr)
        return process, json.loads(process.stdout)

    def test_all_pass_requires_independent_confirmation_and_reports_tested_ceiling(self):
        for i in range(3):
            self.trial(i)
        self.confirm(2)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['confirmed_rate_bps_l1'], 30_000_000_000)
        self.assertIsNone(result['R_valid_bps_l1'])  # synthetic evidence is not hardware
        self.assertTrue(result['tested_ceiling'])
        self.assertIsNone(result['stop'])
        self.assertEqual(len(result['stages']), 3)
        self.assertEqual(result['scope'], 'software-only')


    def test_middle_fail_stops_without_opening_later_bundle(self):
        self.trial(0)
        self.trial(1, 'FAIL')
        self.index['trials'].append(dict(rate_bps_l1=self.plan['ladder_bps_l1'][2],
                                         bundle='does-not-exist.json', sha256='0'*64))
        self.confirm(0)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(result['confirmed_rate_bps_l1'], self.plan['ladder_bps_l1'][0])
        self.assertEqual(result['stop']['status'], 'FAIL')
        self.assertEqual(result['stop']['step'], 2)
        self.assertEqual(result['stop']['failed_gates'], ['p99'])
        self.assertEqual(len(result['stages']), 2)
        self.assertEqual(result['ignored_trials'], 1)

    def test_inconclusive_stops_but_previous_pass_can_be_confirmed(self):
        self.trial(0)
        self.trial(1, 'INCONCLUSIVE')
        self.confirm(0)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(result['stop']['status'], 'INCONCLUSIVE')
        self.assertIn('insufficient_main_samples', result['stop']['reason_codes'])
        self.assertEqual(result['confirmed_rate_bps_l1'], self.plan['ladder_bps_l1'][0])

    def test_failed_repeat_waits_for_registered_fallback_without_promoting_initial_pass(self):
        self.trial(0)
        self.trial(1)
        self.trial(2, 'FAIL')
        self.confirm(1, 'FAIL')
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertEqual(result['status'], 'INCONCLUSIVE')
        self.assertIsNone(result['confirmed_rate_bps_l1'])
        self.assertIsNone(result['R_valid_bps_l1'])
        self.assertIn('fallback_confirmation_missing', result['reason_codes'])
        self.assertEqual(result['confirmation']['status'], 'FAIL')

    def test_missing_repeat_is_pending_not_pass(self):
        self.trial(0)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertEqual(result['status'], 'INCONCLUSIVE')
        self.assertIsNone(result['confirmed_rate_bps_l1'])
        self.assertIn('confirmation_missing', result['reason_codes'])

    def test_first_failure_has_no_valid_rate(self):
        self.trial(0, 'FAIL')
        process, result = self.invoke()
        self.assertEqual(process.returncode, 1, process.stdout)
        self.assertIsNone(result['candidate_rate_bps_l1'])
        self.assertIsNone(result['confirmed_rate_bps_l1'])
        self.assertEqual(result['stop']['step'], 1)

    def test_unrun_step_stops_without_jump_to_later_pass(self):
        self.trial(0)
        self.trial(2)
        self.confirm(0)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(result['stop']['step'], 2)
        self.assertIn('bundle_missing', result['stop']['reason_codes'])
        self.assertEqual(result['confirmed_rate_bps_l1'], self.plan['ladder_bps_l1'][0])

    def test_reused_run_is_not_independent_confirmation(self):
        entry = self.trial(0)
        self.index['confirmation'] = copy.deepcopy(entry)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIsNone(result['confirmed_rate_bps_l1'])
        self.assertIn('run_reused', result['confirmation']['reason_codes'])

    def test_wrong_confirmation_rate_cannot_confirm(self):
        self.trial(0)
        self.confirm(1)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIsNone(result['confirmed_rate_bps_l1'])
        self.assertIn('confirmation_rate_mismatch', result['reason_codes'])

    def test_bad_hash_is_inconclusive_and_previous_pass_is_preserved(self):
        self.trial(0)
        self.trial(1)['sha256'] = '0'*64
        self.confirm(0)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertIn('bundle_hash_mismatch', result['stop']['reason_codes'])
        self.assertEqual(result['confirmed_rate_bps_l1'], self.plan['ladder_bps_l1'][0])

    def test_plan_shape_and_unique_ascending_positive_rates(self):
        mutations = [lambda p:p.update(ladder_bps_l1=[30, 20]),
            lambda p:p.update(ladder_bps_l1=[20, 20]), lambda p:p.update(ladder_bps_l1=[True]),
            lambda p:p.update(ladder_bps_l1=[0]), lambda p:p.update(confirmation_runs=2), lambda p:p.update(confirmation_fallbacks=2),
            lambda p:p.pop('confirmation_fallbacks'), lambda p:p.update(confirmation_fallbacks=True),
            lambda p:p.update(composition='arbitrary'), lambda p:p.update(scope='unknown'),
            lambda p:p.update(cells=p['cells'][:1]), lambda p:p.update(config_sha256='bad'),
            lambda p:p.update(observation_seconds=0)]
        original = copy.deepcopy(self.plan)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.plan = copy.deepcopy(original)
                mutate(self.plan)
                process, result = self.invoke()
                self.assertEqual(process.returncode, 2, process.stdout)
                self.assertIn('search_input_error', result['reason_codes'])
                self.assertIsNone(result['R_valid_bps_l1'])

    def test_bundle_must_match_scope_config_cells_gates_and_duration(self):
        mutations = [lambda b:b.update(schema='other'), lambda b:b.update(scope='physical'),
            lambda b:b['sources'].update(config_sha256='b'*64), lambda b:b.update(rows=b['rows'][:1]),
            lambda b:b['rows'][0]['gates'].pop('storage'),
            lambda b:b['rows'][0]['gates']['median'].update(status='not_applicable'),
            lambda b:b['rows'][0]['measurement_window'].update(end_ns=1),
            lambda b:b['rows'][0].update(repeat='pooled'),
            lambda b:b['rows'][0].update(required=False)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.index['trials'] = []
                self.trial(0, changes=mutate)
                process, result = self.invoke()
                self.assertEqual(process.returncode, 2, process.stdout)
                self.assertEqual(result['stop']['status'], 'INCONCLUSIVE')
                self.assertIsNone(result['candidate_rate_bps_l1'])

    def test_physical_scope_returns_only_the_confirmed_tested_rate(self):
        self.plan['scope'] = 'physical'
        self.trial(0)
        self.trial(1, 'FAIL')
        self.confirm(0)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(result['R_valid_bps_l1'], self.plan['ladder_bps_l1'][0])
        self.assertFalse(result['tested_ceiling'])

    def test_duplicate_stage_rate_is_rejected_instead_of_selecting_a_pass(self):
        self.trial(0, 'FAIL')
        self.index['trials'].append(dict(self.index['trials'][0], **self.bundle('retry')))
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIn('search_input_error', result['reason_codes'])

    def test_drop_attribution_preserves_layers_and_missing_is_not_zero(self):
        def counters(b):
            b['counters'] = dict(ports={'0':dict(q_drop=7, handoff_drop=2,
                delay_storage_drop=1, trace_loss_drop=3)}, observer={},
                diagnostics={'native:1':dict(nic_imissed=4, nic_rx_nombuf=None,
                    m7_line0_record_exhaustions=1, m7_line0_hold_ns=10_000_000)})
        self.trial(0, 'FAIL', changes=counters)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 1, process.stdout)
        layers = result['stages'][0]['drop_layers']
        for name in ('nic', 'ring', 'storage', 'Q', 'model'):
            self.assertEqual(layers[name]['status'], 'observed_nonzero')
        self.assertEqual(layers['observer']['status'], 'unavailable')
        self.assertNotIn('hold_ns', json.dumps(layers))
        self.assertIn('nic_rx_nombuf', layers['nic']['unavailable_fields'])
        self.assertEqual(result['stop']['bottleneck_layers'], ['nic','ring','storage'])
        self.assertEqual(layers['Q']['counters'][0]['value'], 7)  # intended Q loss stays modeled


    def test_registration_cannot_be_substituted_after_evidence_index_is_written(self):
        self.trial(0)
        self.confirm(0)
        self.invoke()
        path = self.root/'plan.json'
        plan = json.loads(path.read_text())
        plan['ladder_bps_l1'] = [20_000_000_000, 40_000_000_000]
        path.write_text(json.dumps(plan)+'\n')
        process = subprocess.run([sys.executable, '-m', 'tools.g2.rvalid', str(path),
            str(self.root/'evidence.json')], capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIn('plan_hash_mismatch', json.loads(process.stdout)['reason_codes'])

    def test_row_disagreement_cannot_turn_bundle_pass_into_search_pass(self):
        def mutate(b):
            b['rows'][0]['gates']['order']['status'] = 'FAIL'
        self.trial(0, changes=mutate)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIn('four_gates_not_all_pass', result['stop']['reason_codes'])
        self.assertIsNone(result['confirmed_rate_bps_l1'])

    def test_inconclusive_confirmation_does_not_report_rvalid(self):
        self.trial(0)
        self.confirm(0, 'INCONCLUSIVE')
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIn('confirmation_not_pass', result['reason_codes'])
        self.assertIsNone(result['confirmed_rate_bps_l1'])

    def test_failing_software_only_bundle_has_diagnostic_attribution_without_causal_claim(self):
        self.trial(0, 'FAIL')
        process, result = self.invoke()
        self.assertEqual(process.returncode, 1, process.stdout)
        self.assertEqual(result['stop']['failed_gates'], ['p99'])
        self.assertEqual(result['stop']['bottleneck_layers'], [])
        self.assertEqual(result['stop']['drop_layers']['nic']['status'], 'unavailable')

    def test_reads_actual_g2_bundle_output_without_requiring_new_producer_fields(self):
        from tools.g2.bundle import build_bundle_report
        from tools.g2.tests.test_bundle import fixture
        raw = self.root/'raw'
        raw.mkdir()
        cfg, manifest, sources, calibration = fixture(raw)
        bundle = build_bundle_report(cfg, {}, manifest, sources, calibration)
        self.assertEqual(bundle['status'], 'PASS', bundle['reason_codes'])
        # Keep the real bundle schema/fields; mirror its one synthetic cell to
        # represent the other required direction, and register its real duration.
        self.plan.update(config_sha256=bundle['sources']['config_sha256'], observation_seconds=1,
            cells=[dict(direction=str(p), line=bundle['rows'][0]['line'], phase=bundle['rows'][0]['phase'])
                   for p in (0, 1)])
        bundle['rows'].append(dict(copy.deepcopy(bundle['rows'][0]), direction='1'))
        def real(value):
            run = value['run']
            value.clear()
            value.update(copy.deepcopy(bundle), run=run)
        self.trial(0, changes=real)
        self.trial(1, 'FAIL')
        self.confirm(0, changes=real)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
        self.assertEqual(result['confirmed_rate_bps_l1'], self.plan['ladder_bps_l1'][0])


    def test_actual_sampled_bundle_uses_registration_windows_and_keeps_coverage(self):
        from tools.g2.bundle import build_bundle_report
        from tools.g2.tests.test_bundle import fixture
        from tools.g2.tests.test_sampled_bundle import checker
        raw = self.root/'sampled-raw'
        raw.mkdir()
        cfg, manifest, sources, calibration = fixture(raw, flow_id='0a000001:0a000002:1:9000')
        checker(raw, manifest, 400)
        bundle = build_bundle_report(cfg, {}, manifest, sources, calibration, registration_root=raw)
        self.assertEqual(bundle['status'], 'PASS', bundle['reason_codes'])
        self.assertNotIn('measurement_window', bundle['rows'][0])
        self.plan.update(config_sha256=bundle['sources']['config_sha256'], observation_seconds=1,
            cells=[dict(direction=str(p), line=bundle['rows'][0]['line'], phase=bundle['rows'][0]['phase'])
                   for p in (0, 1)])
        bundle['rows'].append(dict(copy.deepcopy(bundle['rows'][0]), direction='1'))
        def real(value):
            run = value['run']
            value.clear()
            value.update(copy.deepcopy(bundle), run=run)
        self.trial(0, changes=real)
        self.trial(1, 'FAIL')
        self.confirm(0, changes=real)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
        self.assertEqual(result['stages'][0]['coverage'], bundle['coverage'])
        self.assertIsNone(result['stages'][0]['measured_rates'][0]['offered_bps_l1'])

    def test_duplicate_window_or_malformed_rows_are_inconclusive(self):
        mutations = [lambda b:b.update(rows=None), lambda b:b.update(counters=['bad']),
                     lambda b:b['rows'][0].update(gates=None)]
        for mutate in mutations:
            self.index['trials'] = []
            self.trial(0, changes=mutate)
            process, result = self.invoke()
            self.assertEqual(process.returncode, 2, process.stdout)
            self.assertEqual(result['stop']['status'], 'INCONCLUSIVE')

    def test_invalid_json_is_one_line_inconclusive(self):
        entry = self.trial(0)
        raw = b'{"status":NaN}'
        (self.root/entry['bundle']).write_bytes(raw)
        entry['sha256'] = hashlib.sha256(raw).hexdigest()
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIn('bundle_unusable', result['stop']['reason_codes'])


    def test_two_directions_must_share_the_registered_measurement_window(self):
        def mutate(b):
            b['rows'][1]['measurement_window'].update(start_ns=600_000_000_000, end_ns=1_200_000_000_000)
        self.trial(0, changes=mutate)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIn('bundle_unusable', result['stop']['reason_codes'])

    def test_plan_cannot_register_separate_one_way_phases_as_both_directions(self):
        self.plan['cells'][1]['phase'] = 'later'
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIn('search_input_error', result['reason_codes'])


    def test_missing_offer_receipt_never_turns_a_rate_label_into_rvalid(self):
        self.plan['scope'] = 'physical'
        self.trial(0)['offer'] = None
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIsNone(result['R_valid_bps_l1'])
        self.assertIn('offer_unverified', result['stop']['reason_codes'])

    def test_offer_readback_decision_and_binding_are_required(self):
        mutations = [lambda o:o.update(status='INCONCLUSIVE'), lambda o:o.update(status='FAIL'),
            lambda o:o.update(composition='U'), lambda o:o.update(bundle_sha256='0'*64),
            lambda o:o.update(plan_sha256='0'*64), lambda o:o.update(run='different'),
            lambda o:o.update(rule='posthoc-rule'), lambda o:o.update(rate_bps_l1=1),
            lambda o:o.update(directions=o['directions'][:1]),
            lambda o:o['directions'][1].update(target_bps_l1=1),
            lambda o:o['directions'][0].update(measured_bps_l1=None),
            lambda o:o.update(windows={})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.index['trials'] = []
                entry = self.trial(0)
                entry['offer'] = self.offer(entry, mutate)
                process, result = self.invoke()
                self.assertEqual(process.returncode, 2, process.stdout)
                self.assertIn('offer_unverified', result['stop']['reason_codes'])
                self.assertIsNone(result['candidate_rate_bps_l1'])

    def test_nonfinite_json_number_emits_one_line_inconclusive(self):
        entry = self.trial(0)
        raw = (self.root/entry['bundle']).read_bytes().replace(b'20000000000', b'1e999')
        (self.root/entry['bundle']).write_bytes(raw)
        entry['sha256'] = hashlib.sha256(raw).hexdigest()
        self.confirm(0)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIsNone(result['R_valid_bps_l1'])


    def test_first_confirmation_pass_never_opens_fallback(self):
        for i in range(3):
            self.trial(i)
        self.confirm(2)
        self.index['fallback_confirmation'] = dict(rate_bps_l1=25_000_000_000,
            bundle='must-not-open.json', sha256='0'*64)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(result['confirmed_step'], 3)
        self.assertEqual(len(result['confirmations']), 1)
        self.assertEqual(result['confirmations'][0]['step'], 3)
        self.assertEqual(result['confirmations'][0]['run'], 'confirmation')
        self.assertEqual(result['confirmations'][0]['status'], 'PASS')

    def test_primary_fail_fallback_pass_returns_next_lower_pass_and_both_results(self):
        self.plan['scope'] = 'physical'
        for i in range(3):
            self.trial(i)
        self.confirm(2, 'FAIL')
        self.confirm(1, fallback=True)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(result['R_valid_bps_l1'], 25_000_000_000)
        self.assertEqual(result['candidate_rate_bps_l1'], 30_000_000_000)
        self.assertEqual(result['confirmed_step'], 2)
        self.assertEqual([(c['step'],c['run'],c['status']) for c in result['confirmations']],
                         [(3,'confirmation','FAIL'),(2,'fallback','PASS')])
        self.assertEqual(result['confirmation']['status'], 'FAIL')

    def test_two_failed_confirmations_return_null_and_never_open_third(self):
        for i in range(3):
            self.trial(i)
        self.confirm(2, 'FAIL')
        self.confirm(1, 'FAIL', fallback=True)
        self.index['third_confirmation'] = dict(rate_bps_l1=20_000_000_000,
            bundle='must-not-open.json', sha256='0'*64)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 1, process.stdout)
        self.assertIsNone(result['confirmed_rate_bps_l1'])
        self.assertIsNone(result['R_valid_bps_l1'])
        self.assertIsNone(result['confirmed_step'])
        self.assertEqual(len(result['confirmations']), 2)
        self.assertEqual([c['status'] for c in result['confirmations']], ['FAIL','FAIL'])
        self.assertIn('fallback_confirmation_not_pass', result['reason_codes'])

    def test_no_lower_pass_returns_null_after_first_failed_confirmation(self):
        self.trial(0)
        self.trial(1, 'FAIL')
        self.confirm(0, 'FAIL')
        self.index['fallback_confirmation'] = dict(rate_bps_l1=1, bundle='must-not-open.json', sha256='0'*64)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 1, process.stdout)
        self.assertIsNone(result['R_valid_bps_l1'])
        self.assertEqual(len(result['confirmations']), 1)
        self.assertIn('no_lower_pass_stage', result['reason_codes'])

    def test_inconclusive_confirmation_uses_same_single_fallback_rule(self):
        for first, second, code in [('INCONCLUSIVE','PASS',0),('FAIL','INCONCLUSIVE',2),
                                     ('INCONCLUSIVE','INCONCLUSIVE',2)]:
            with self.subTest(first=first,second=second):
                self.index['trials'] = []
                self.trial(0)
                self.trial(1)
                self.trial(2, 'FAIL')
                self.confirm(1, first)
                self.confirm(0, second, fallback=True)
                process, result = self.invoke()
                self.assertEqual(process.returncode, code, process.stdout)
                self.assertEqual([c['status'] for c in result['confirmations']], [first,second])
                self.assertEqual(result['confirmed_rate_bps_l1'], 20_000_000_000 if code==0 else None)

    def test_fallback_cannot_skip_next_lower_pass_or_repeat_same_rate(self):
        for fallback in (0,2):
            with self.subTest(fallback=fallback):
                self.index['trials'] = []
                for i in range(3):
                    self.trial(i)
                self.confirm(2, 'FAIL')
                self.confirm(fallback, fallback=True)
                process, result = self.invoke()
                self.assertEqual(process.returncode, 2, process.stdout)
                self.assertIsNone(result['confirmed_rate_bps_l1'])
                self.assertIn('fallback_confirmation_rate_mismatch', result['reason_codes'])

    def test_fallback_run_must_be_independent_of_every_consumed_run(self):
        for i in range(3):
            self.trial(i)
        self.confirm(2, 'FAIL')
        self.index['fallback_confirmation'] = copy.deepcopy(self.index['trials'][1])
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertIsNone(result['confirmed_rate_bps_l1'])
        self.assertIn('run_reused', result['confirmations'][1]['reason_codes'])

    def test_no_primary_attempt_remains_pending_and_does_not_open_fallback(self):
        self.trial(0)
        self.trial(1)
        self.index['fallback_confirmation'] = dict(rate_bps_l1=20_000_000_000,
            bundle='must-not-open.json', sha256='0'*64)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 2, process.stdout)
        self.assertEqual(result['confirmations'], [])
        self.assertIn('confirmation_missing', result['reason_codes'])


    def test_unusable_primary_retains_readable_run_and_prevents_fallback_reuse(self):
        for mutate in (lambda b:b['sources'].update(config_sha256='b'*64),
                       lambda b:b.update(scope='other'), lambda b:b.update(schema='other')):
            with self.subTest(mutate=mutate):
                self.index['trials'] = []
                for i in range(3):
                    self.trial(i)
                self.confirm(2, 'FAIL', changes=mutate)
                primary = self.index['confirmation']
                self.confirm(1, changes=lambda b:b.update(run='confirmation'), fallback=True)
                process, result = self.invoke()
                self.assertEqual(process.returncode, 2, process.stdout)
                self.assertIsNone(result['confirmed_rate_bps_l1'])
                first, second = result['confirmations']
                self.assertEqual(first['run'], 'confirmation')
                self.assertEqual(first['bundle'], primary['bundle'])
                self.assertEqual(first['sha256'], primary['sha256'])
                self.assertIn('run_reused', second['reason_codes'])

    def test_unusable_primary_can_fallback_to_distinct_valid_run_without_losing_evidence(self):
        for i in range(3):
            self.trial(i)
        self.confirm(2, changes=lambda b:b['sources'].update(config_sha256='b'*64))
        self.confirm(1, fallback=True)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(result['confirmed_step'], 2)
        self.assertEqual([(c['run'],c['status']) for c in result['confirmations']],
                         [('confirmation','INCONCLUSIVE'),('fallback','PASS')])

    def test_hash_mismatch_reserves_same_run_like_correct_hash_even_in_another_file(self):
        self.plan['scope'] = 'physical'
        for bad_hash in (False, True):
            for copied_file in (False, True):
                with self.subTest(bad_hash=bad_hash, copied_file=copied_file):
                    self.index['trials'] = []
                    for i in range(3):
                        self.trial(i)
                    self.confirm(1, fallback=True)
                    fallback = self.index['fallback_confirmation']
                    primary = dict(copy.deepcopy(fallback), rate_bps_l1=30_000_000_000)
                    if bad_hash:
                        primary['sha256'] = '0'*64
                    self.index['confirmation'] = primary
                    if copied_file:
                        target = self.root/'copied-report.json'
                        target.write_bytes((self.root/fallback['bundle']).read_bytes())
                        fallback['bundle'] = target.name
                    process, result = self.invoke()
                    self.assertEqual(process.returncode, 2, process.stdout)
                    self.assertIsNone(result['R_valid_bps_l1'])
                    first, second = result['confirmations']
                    self.assertEqual(first['status'], 'INCONCLUSIVE')
                    self.assertIn('bundle_hash_mismatch' if bad_hash else 'offer_unverified', first['reason_codes'])
                    self.assertEqual(first['run'], 'fallback')
                    self.assertEqual(first['bundle'], primary['bundle'])
                    self.assertEqual(first['sha256'], primary['sha256'])
                    self.assertIn('run_reused', second['reason_codes'])

    def test_bad_hash_does_not_reject_a_genuinely_distinct_fallback_run(self):
        self.plan['scope'] = 'physical'
        for i in range(3):
            self.trial(i)
        self.confirm(2)
        self.index['confirmation']['sha256'] = '0'*64
        self.confirm(1, fallback=True)
        process, result = self.invoke()
        self.assertEqual(process.returncode, 0, process.stdout)
        first, second = result['confirmations']
        self.assertEqual((first.get('run'), first['status']), ('confirmation','INCONCLUSIVE'))
        self.assertEqual(first['reason_codes'], ['bundle_hash_mismatch'])
        self.assertEqual((second['run'], second['status']), ('fallback','PASS'))
        self.assertEqual(result['R_valid_bps_l1'], 25_000_000_000)

    def test_structural_and_offer_early_returns_also_reserve_readable_runs(self):
        for failure in ('rows', 'config', 'offer'):
            with self.subTest(failure=failure):
                self.index['trials'] = []
                for i in range(3):
                    self.trial(i)
                def mutate(b):
                    if failure == 'rows':
                        b['rows'] = None
                    elif failure == 'config':
                        b['sources'].pop('config_sha256')
                if failure == 'rows':
                    self.index['confirmation'] = dict(rate_bps_l1=30_000_000_000,
                        **self.bundle('confirmation', changes=mutate))
                else:
                    self.confirm(2, changes=mutate)
                primary = self.index['confirmation']
                if failure == 'offer':
                    primary['offer']['sha256'] = '0'*64
                self.confirm(1, changes=lambda b:b.update(run='confirmation'), fallback=True)
                process, result = self.invoke()
                self.assertEqual(process.returncode, 2, process.stdout)
                first, second = result['confirmations']
                self.assertEqual(first['run'], 'confirmation')
                self.assertIn('offer_unverified' if failure == 'offer' else 'bundle_unusable', first['reason_codes'])
                self.assertIn('run_reused', second['reason_codes'])
                self.assertIsNone(result['R_valid_bps_l1'])

    def test_malformed_sha_reference_still_reserves_readable_run(self):
        for declared_hash in (None, '', 'not-a-sha256'):
            with self.subTest(declared_hash=declared_hash):
                self.index['trials'] = []
                for i in range(3):
                    self.trial(i)
                self.confirm(2)
                self.index['confirmation']['sha256'] = declared_hash
                self.confirm(1, changes=lambda b:b.update(run='confirmation'), fallback=True)
                process, result = self.invoke()
                self.assertEqual(process.returncode, 2, process.stdout)
                first, second = result['confirmations']
                self.assertEqual(first['reason_codes'], ['bundle_unusable'])
                self.assertEqual(first.get('run'), 'confirmation')
                self.assertEqual(second['reason_codes'], ['run_reused'])

    def test_unreadable_run_preserves_hash_error_priority(self):
        for malformed in (b'{', b'[]', b'{}'):
            for bad_hash in (False, True):
                with self.subTest(malformed=malformed, bad_hash=bad_hash):
                    self.index['trials'] = []
                    for i in range(3):
                        self.trial(i)
                    self.confirm(2)
                    primary = self.index['confirmation']
                    (self.root/primary['bundle']).write_bytes(malformed)
                    primary['sha256'] = '0'*64 if bad_hash else hashlib.sha256(malformed).hexdigest()
                    self.confirm(1, fallback=True)
                    process, result = self.invoke()
                    self.assertEqual(process.returncode, 0, process.stdout)
                    first, second = result['confirmations']
                    self.assertEqual(first['reason_codes'],
                                     ['bundle_hash_mismatch' if bad_hash else 'bundle_unusable'])
                    self.assertNotIn('run', first)
                    self.assertEqual(second['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
