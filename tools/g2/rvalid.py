# SPDX-License-Identifier: Apache-2.0
"""Offline registered R_valid ladder; consume existing G2 gates without refitting."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from .reference import strict_json

GATES = ('median', 'p99', 'order', 'storage')
LAYERS = {
    'nic': {'nic_imissed', 'nic_ierrors', 'nic_oerrors', 'nic_rx_nombuf',
            'rx_missed_errors', 'rx_errors', 'tx_errors', 'rx_mbuf_allocation_errors'},
    'ring': {'handoff_drop', 'rx_handoff_drop', 'tx_handoff_drop', 'rx_ring_drops', 'tx_ring_drops'},
    'storage': {'delay_storage_drop', 'storage_drops'},
    'Q': {'q_drop', 'q_drops'},
    'model': {'trace_loss_drop', 'trace_outage_drop', 'trace_loss_drops', 'trace_outage_drops'},
    'other': {'invalid_input_drop', 'terminal_tx_drop', 'shutdown_drop', 'missed_deadline_outage_drop'},
    'observer': {'records_dropped', 'capture_drops', 'malformed_records', 'identity_errors',
                 'timestamp_missing', 'clock_invalid'},
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def positive(value):
    return type(value) is int and 0 < value < 2**64


def sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def cell_key(row):
    return tuple(row[name] for name in ('direction', 'line', 'phase'))


def validate_plan(plan, evidence):
    require(isinstance(plan, dict) and plan.get('schema') == 'hng-m7-rvalid-plan-v1', 'unsupported plan')
    require(isinstance(plan.get('registration_id'), str) and bool(plan['registration_id']), 'missing registration ID')
    require(plan.get('scope') in ('physical', 'software-only') and plan.get('composition') in ('U', 'X'),
            'invalid search scope/composition')
    require(sha(plan.get('config_sha256')), 'invalid config hash')
    require(isinstance(plan.get('offer_rule'), str) and bool(plan['offer_rule']), 'register the offer qualification rule')
    rates = plan.get('ladder_bps_l1')
    require(isinstance(rates, list) and bool(rates) and all(positive(r) for r in rates), 'invalid rate ladder')
    require(rates == sorted(set(rates)), 'rate ladder must be strictly ascending')
    require(positive(plan.get('observation_seconds')) and type(plan.get('confirmation_runs')) is int
            and plan['confirmation_runs'] == 1, 'register a duration and exactly one independent confirmation')
    cells = plan.get('cells')
    require(isinstance(cells, list) and bool(cells), 'required cells missing')
    for row in cells:
        require(isinstance(row, dict) and set(row) == {'direction', 'line', 'phase'}
                and all(isinstance(v, str) and bool(v) for v in row.values()), 'invalid required cell')
        require(row['direction'].isdigit() and str(int(row['direction'])) == row['direction']
                and int(row['direction']) <= 65535, 'direction must be an ingress port string')
    require(len({row['direction'] for row in cells}) == 2 and len(set(map(cell_key, cells))) == len(cells),
            'register unique cells for both simultaneous directions')
    directions = {row['direction'] for row in cells}
    for phase in {row['phase'] for row in cells}:
        require({row['direction'] for row in cells if row['phase'] == phase} == directions,
                'every phase requires both simultaneous directions')
    require(isinstance(evidence, dict) and evidence.get('schema') == 'hng-m7-rvalid-evidence-v1'
            and evidence.get('registration_id') == plan['registration_id'], 'evidence registration mismatch')
    trials = evidence.get('trials')
    require(isinstance(trials, list) and all(isinstance(t, dict) for t in trials), 'invalid trials')
    seen = set()
    for trial in trials:
        rate = trial.get('rate_bps_l1')
        require(positive(rate) and rate in rates and rate not in seen, 'unregistered or duplicate trial rate')
        seen.add(rate)
    return {trial['rate_bps_l1']:trial for trial in trials}


def drop_layers(report):
    """Do not add overlapping counters or infer a missing layer's value as zero."""
    result = {name:dict(status='unavailable', counters=[], unavailable_fields=[]) for name in LAYERS}
    counters = report.get('counters') or {}
    for group in ('ports', 'diagnostics', 'observer'):
        for token, fields in (counters.get(group) or {}).items():
            for field, value in fields.items():
                for layer, names in LAYERS.items():
                    compact = layer == 'storage' and re.fullmatch(
                        r'm7_line\d+_(item|record|cell|normal|materialize|core)_exhaustions', field)
                    if field not in names and not compact:
                        continue
                    target = result[layer]
                    if type(value) is not int or value < 0:
                        target['unavailable_fields'].append(field)
                        continue
                    # Scope/name/value are evidence, not an exclusive loss decomposition.
                    target['counters'].append(dict(scope=group+':'+token, field=field, value=value))
    for item in result.values():
        if item['counters']:
            item['status'] = 'observed_nonzero' if any(x['value'] for x in item['counters']) else 'observed_zero'
        item['unavailable_fields'] = sorted(set(item['unavailable_fields']))
    return result


def inconclusive(rate, reason, **details):
    return dict(rate_bps_l1=rate, status='INCONCLUSIVE', reason_codes=[reason], failed_gates=[],
                bottleneck_layers=[], drop_layers=drop_layers({}), **details)


def read_offer(reference, rate, plan, plan_hash, bundle_hash, run, windows, root):
    """Consume a registered apparatus decision, without inventing rate tolerance."""
    try:
        require(isinstance(reference, dict) and sha(reference.get('sha256')), 'offer receipt missing')
        raw = (root / reference['path']).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == reference['sha256'], 'offer hash mismatch')
        offer = strict_json(raw)
        require(offer['schema'] == 'hng-m7-offer-v1' and offer['run'] == run
                and offer['plan_sha256'] == plan_hash and offer['bundle_sha256'] == bundle_hash,
                'offer receipt identity mismatch')
        require(offer['composition'] == plan['composition'] and offer['rate_bps_l1'] == rate
                and offer['rule'] == plan['offer_rule'], 'offer condition/rule mismatch')
        require(set(offer['windows']) == set(windows) and all(
            tuple(offer['windows'][phase][key] for key in ('start_ns', 'end_ns', 'clock_id')) == interval
            for phase, interval in windows.items()), 'offer measurement window mismatch')
        directions = offer['directions']
        require(len(directions) == 2 and {d['direction'] for d in directions} ==
                {c['direction'] for c in plan['cells']}, 'offer directions mismatch')
        for d in directions:
            require(type(d['target_bps_l1']) is int and d['target_bps_l1'] == rate
                    and type(d['measured_bps_l1']) in (int, float)
                    and math.isfinite(d['measured_bps_l1']) and d['measured_bps_l1'] > 0,
                    'offer target/readback missing')
        require(offer['status'] == 'PASS', 'registered offer qualification is not PASS')
        return dict(status='PASS', source=reference, directions=directions, rule=offer['rule'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        return dict(status='INCONCLUSIVE', error=str(exc), source=reference)


def read_stage(trial, rate, plan, root, used_runs, plan_hash):
    if trial is None:
        return inconclusive(rate, 'bundle_missing')
    try:
        require(isinstance(trial.get('bundle'), str) and bool(trial['bundle']) and sha(trial.get('sha256')),
                'bundle path and SHA256 required')
        path = root / trial['bundle']
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != trial['sha256']:
            return inconclusive(rate, 'bundle_hash_mismatch')
        report = strict_json(raw)
        require(report.get('schema') == 'hng-g2-report-v1', 'unsupported G2 report schema')
        require(report.get('scope') == plan['scope'] and report['registration']['scope'] == plan['scope'],
                'bundle scope mismatch')
        require(report['sources']['config_sha256'] == plan['config_sha256'], 'bundle config mismatch')
        run = report.get('run')
        require(isinstance(run, str) and bool(run), 'missing run ID')
        if run in used_runs:
            return inconclusive(rate, 'run_reused', run=run)
        used_runs.add(run)
        require(report.get('status') in ('PASS', 'FAIL', 'INVALID', 'INCONCLUSIVE')
                and report.get('measurement_status') in ('PASS', 'FAIL', 'INVALID', 'INCONCLUSIVE'),
                'missing G2 measurement status')
        rows = [row for row in report['rows'] if row.get('required') is True]
        wanted = set(map(cell_key, plan['cells']))
        require(len(rows) == len(wanted) and set(map(cell_key, rows)) == wanted, 'required cell mismatch')
        failed, reasons = set(), set(report.get('reason_codes', []))
        complete = True
        windows_seen = {}
        for row in rows:
            require(type(row.get('repeat')) is int, 'pooled or missing repeat evidence')
            require(row.get('status') in ('PASS', 'FAIL', 'INVALID', 'INCONCLUSIVE')
                    and row.get('measurement_status') in ('PASS', 'FAIL', 'INVALID', 'INCONCLUSIVE'),
                    'missing cell measurement status')
            require(set(GATES) <= row['gates'].keys(), 'four G2 gates required')
            for name in GATES:
                state = row['gates'][name]['status']
                require(state in ('PASS', 'FAIL', 'INCONCLUSIVE', 'not_applicable'), 'unknown gate status')
                if state == 'FAIL':
                    failed.add(name)
                if state != 'PASS':
                    complete = False
            window = row.get('measurement_window')
            if window is None:
                # The sampled producer keeps windows in the manifest, not rows.
                windows = [w for w in report['registration']['windows'] if w['phase'] == row['phase']]
                require(len(windows) == 1, 'missing or duplicate measurement window')
                window = windows[0]
            require(type(window['start_ns']) is int and type(window['end_ns']) is int
                    and window['end_ns']-window['start_ns'] >= plan['observation_seconds']*10**9,
                    'observation shorter than registration')
            interval = window['start_ns'], window['end_ns'], window['clock_id']
            require(windows_seen.setdefault(row['phase'], interval) == interval,
                    'required directions have different measurement windows')
            reasons.update(row.get('reason_codes', []))
            reasons.update(row.get('observed_failures', []))
            complete &= row['status'] == 'PASS' and row['measurement_status'] == 'PASS'
        state = report['measurement_status']
        status = 'FAIL' if state == 'FAIL' or report['status'] == 'FAIL' else 'INCONCLUSIVE'
        if state == report['status'] == 'PASS' and complete:
            status = 'PASS'
        elif state == report['status'] == 'PASS':
            reasons.add('four_gates_not_all_pass')
        offer = read_offer(trial.get('offer'), rate, plan, plan_hash, trial['sha256'], run, windows_seen, root)
        if offer['status'] != 'PASS':
            reasons.add('offer_unverified')
            if status == 'PASS':
                status = 'INCONCLUSIVE'
        layers = drop_layers(report)
        bottlenecks = [name for name in ('nic', 'ring', 'storage', 'other', 'observer')
                       if layers[name]['status'] == 'observed_nonzero']
        return dict(rate_bps_l1=rate, status=status, run=run, bundle=trial['bundle'], sha256=trial['sha256'],
                    reason_codes=sorted(reasons), failed_gates=sorted(failed), bottleneck_layers=bottlenecks,
                    drop_layers=layers, offer=offer, coverage=report.get('coverage'), measured_rates=[dict(zip(('direction','line','phase'), cell_key(row)),
                        offered_bps_l1=row.get('offered_bps_l1'), delivered_bps_l1=row.get('delivered_bps_l1'),
                        delivered_bps_l2=row.get('delivered_bps_l2'), l7_bps=row.get('l7_bps'), pps=row.get('pps'))
                        for row in rows])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        return inconclusive(rate, 'bundle_unusable', error=str(exc))


def judge(plan, evidence, root):
    trials = validate_plan(plan, evidence)
    stages, used_runs, stop, candidate = [], set(), None, None
    for step, rate in enumerate(plan['ladder_bps_l1'], 1):
        stage = dict(read_stage(trials.get(rate), rate, plan, root, used_runs, evidence.get('plan_sha256')), step=step)
        stages.append(stage)
        if stage['status'] != 'PASS':
            stop = stage
            break
        candidate = rate
    result = dict(schema='hng-m7-rvalid-result-v1', registration_id=plan['registration_id'],
        scope=plan['scope'], composition=plan['composition'], status='INCONCLUSIVE',
        R_valid_bps_l1=None, confirmed_rate_bps_l1=None, candidate_rate_bps_l1=candidate,
        tested_ceiling=stop is None, stop=stop, stages=stages, confirmation=None, reason_codes=[],
        ignored_trials=sum(rate not in {s['rate_bps_l1'] for s in stages} for rate in trials),
        hardware_qualification='not_assessed; consumes supplied G2 decisions',
        rate_basis='registered offered L1 per direction; measured rates retained separately',
        attribution='observed diagnostic counters; overlapping, not causal proof')
    if candidate is None:
        result.update(status=stop['status'], reason_codes=['no_pass_stage'])
        return result
    repeat = evidence.get('confirmation')
    if repeat is None:
        result['reason_codes'] = ['confirmation_missing']
    elif not isinstance(repeat, dict) or type(repeat.get('rate_bps_l1')) is not int or repeat['rate_bps_l1'] != candidate:
        result['reason_codes'] = ['confirmation_rate_mismatch']
    else:
        confirmation = read_stage(repeat, candidate, plan, root, used_runs, evidence.get('plan_sha256'))
        result.update(confirmation=confirmation, status=confirmation['status'])
        if confirmation['status'] == 'PASS':
            result.update(confirmed_rate_bps_l1=candidate,
                          R_valid_bps_l1=candidate if plan['scope'] == 'physical' else None)
        else:
            result['reason_codes'] = ['confirmation_not_pass']
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('evidence', type=Path, help='bundle paths resolve relative to this evidence index')
    args = parser.parse_args(argv)
    try:
        raw_plan = args.plan.read_bytes()
        plan = strict_json(raw_plan)
        evidence = strict_json(args.evidence.read_bytes())
        if not isinstance(evidence, dict) or evidence.get('plan_sha256') != hashlib.sha256(raw_plan).hexdigest():
            result = dict(schema='hng-m7-rvalid-result-v1', status='INCONCLUSIVE', R_valid_bps_l1=None,
                          confirmed_rate_bps_l1=None, reason_codes=['plan_hash_mismatch'])
        else:
            result = judge(plan, evidence, args.evidence.parent)
            result['plan_sha256'] = evidence['plan_sha256']
        output = json.dumps(result, sort_keys=True, allow_nan=False)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        result = dict(schema='hng-m7-rvalid-result-v1', status='INCONCLUSIVE', R_valid_bps_l1=None,
                      confirmed_rate_bps_l1=None, reason_codes=['search_input_error'], error=str(exc))
        output = json.dumps(result, sort_keys=True, allow_nan=False)
    print(output)
    return {'PASS':0, 'FAIL':1, 'INCONCLUSIVE':2}[result['status']]


if __name__ == '__main__':
    sys.exit(main())
