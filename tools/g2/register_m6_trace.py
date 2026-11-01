# SPDX-License-Identifier: Apache-2.0
"""Register the 60 G-short trace conditions; generate external profiles serially."""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b''):
            value.update(block)
    return value.hexdigest()


def write(path, value):
    with path.open('x') as output:
        json.dump(value, output, indent=2, allow_nan=False)
        output.write('\n')


def ceil(value):
    return -(-value.numerator // value.denominator)


def ratio(value):
    return dict(numerator=value.numerator, denominator=value.denominator,
                estimate=float(value))


def episode_budget(rate, late_fraction):
    if late_fraction == 0:
        return None, 20
    need = ceil(Fraction(431) / (Fraction(rate, 8 * 1542 * 1024) * 9 * late_fraction))
    return need, min(20, need)


def register_episode_acceptance(matrix):
    """Apply the adopted main-per-episode policy without changing the run inventory."""
    plan = matrix['plan']
    plan['acceptance'] = dict(decision='2026-11-01 08:1x', unit='individual_episode',
        timing_population='main', minimum_main_pairs=368, all_episodes_must_pass=True,
        cross_episode_aggregation=False,
        order_population='all packet identities, including unsampled packets',
        storage_population='all packet identities and conservation counters',
        late_subset_role='diagnostic_only', late_subset_minimum_for_bounded_p99=368,
        late_subset_insufficient_action='report diagnostic insufficient; do not gate acceptance')
    plan['caveat'] = ('Episode counts retain the capped planning formula. Each episode is judged '
        'independently from measured main timing pairs and full-identity integrity checks. '
        'Late-subset sample estimates are diagnostic only; no episode aggregation is needed.')
    for condition in plan['trace_conditions']:
        rate = condition['main_samples_per_second_estimate']
        main = Fraction(rate['numerator'], rate['denominator']) * condition['steady_seconds']
        condition['expected_main_pairs_per_episode'] = ratio(main)
        condition['main_planning_target_431_met_per_episode'] = main >= 431
        condition['acceptance_policy'] = 'main_per_episode_all_pass_no_pooling'
        condition['all_late_diagnostic_targets_met'] = condition.pop('all_steady_planning_targets_met')
        for row in condition['directions'].values():
            row['late_diagnostic_target_431_met'] = row.pop('steady_planning_target_431_met')
            row['late_diagnostic_expected_368_met'] = row.pop('steady_expected_368_met')
            row['late_subset_affects_acceptance'] = False
    return matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--raw', type=Path, required=True)
    parser.add_argument('--m3-evidence', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    assert episode_budget(8_000_000_000, Fraction(1, 100)) == (8, 8)
    assert episode_budget(8_000_000_000, Fraction(1, 200)) == (16, 16)
    assert episode_budget(8_000_000_000, Fraction(0)) == (None, 20)
    assert episode_budget(8_000_000_000, Fraction(1, 1_000_000))[1] == 20
    import numpy as np
    sys.path.insert(0, str(args.core / 'scripts'))
    from garcia_trace import read_source
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    examples = out / 'examples'; examples.mkdir()
    profiles = examples / 'trace-profiles'; profiles.mkdir()
    stamp, start, duration = '2025-04-16-09-26-16', 38_000_000_000, 11_000_000_000
    pool = args.m3_evidence / 'pool-11s.json'
    pool_doc = json.loads(pool.read_text())
    index = next(i for i, item in enumerate(pool_doc['candidates'])
                 if item['stamp'] == stamp and item['start_ns'] == start and item['duration_ns'] == duration)
    candidate = pool_doc['candidates'][index]
    converter = args.core / 'scripts/garcia_trace.py'
    windows = {}
    receipts = {}
    for direction, corridor in itertools.product(('ul', 'dl'), ('A', 'B')):
        name = f'{stamp}-{start}-11s-{direction}-{corridor}'
        old_dir = args.m3_evidence / 'tolerance-sweep/10000ns' / name
        old = json.loads((old_dir / 'result.json').read_text())
        assert old['recipe']['adaptive_ns'] == 10000
        assert old['recipe']['implementation_sha256'] == sha(converter)
        assert old['fidelity']['status'] == 'PASS'
        target = profiles / f'{name}.json'
        command = ['prlimit', '--as=4294967296', '--', sys.executable, '-B', str(converter),
                   str(args.raw / f'{stamp}__{direction}.csv.zip'), str(target),
                   '--start-ns', str(start), '--duration-ns', str(duration), '--corridor', corridor,
                   '--seed', '1', '--queue-drain-ns', '20000000', '--pool', str(pool), '--pool-index', str(index)]
        run = subprocess.run(command, capture_output=True, text=True,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=300)
        (profiles / f'{name}.conversion.log').write_text(run.stdout + run.stderr)
        if run.returncode:
            raise RuntimeError(f'{name}: conversion exit {run.returncode}: {run.stderr}')
        manifest = json.loads(target.with_suffix('.manifest.json').read_text())
        baseline_path = Path(old['normalized_path'])
        assert sha(baseline_path) == old['normalized_sha256']
        # Only the recipe and terminal horizon may differ from the canonical bytes.
        def unchanged_rows(path):
            with path.open() as stream:
                for line in stream:
                    if not line.lstrip().startswith(('"conversion":', '"terminal_horizon_ns":')):
                        yield line
        sentinel = object()
        assert all(a == b for a, b in itertools.zip_longest(
            unchanged_rows(baseline_path), unchanged_rows(target), fillvalue=sentinel))
        assert manifest['recipe']['drain_budget']['queue_drain_ns'] == 20_000_000
        assert manifest['interval_count'] == old['interval_count']
        assert manifest['normalized_sha256'] == sha(target)
        source = read_source(args.raw / f'{stamp}__{direction}.csv.zip')
        assert source.sha256 == old['source_sha256'] == candidate['sources'][direction]['sha256']
        bins = (source.send_ns - source.send_ns[0]) // 15_000_000_000
        baseline = np.empty(len(bins))
        for value in np.unique(bins):
            baseline[bins == value] = np.median(source.owd_ns[bins == value])
        full = (source.send_ns >= start) & (source.send_ns < start + duration)
        steady = full & (source.send_ns >= start + 2_000_000_000)
        late = source.owd_ns - baseline > 20_000_000
        full_n, full_l = int(full.sum()), int(np.count_nonzero(late & full))
        steady_n, steady_l = int(steady.sum()), int(np.count_nonzero(late & steady))
        assert full_n == old['packets'] and full_l == old['fidelity']['source']['n_late']
        key = direction + '-' + corridor
        windows[key] = dict(source_id=stamp, direction=direction, corridor=corridor,
            start_ns=start, duration_ns=duration, phase_ns=old['recipe']['phase_ns'],
            pool_index=index, pool_sha256=sha(pool), source_sha256=source.sha256,
            source_member_sha256=source.member_sha256, source_license='CC-BY-4.0',
            source_dataset='https://doi.org/10.5281/zenodo.16275284',
            adaptive_ns=10000, boundary_quantum_ns=1000, latency_quantum_ns=1000,
            offset_ns=old['recipe']['offset_ns'], seed=1,
            profile_path='trace-profiles/' + target.name, profile_sha256=sha(target),
            profile_manifest_path='trace-profiles/' + target.with_suffix('.manifest.json').name,
            profile_manifest_sha256=sha(target.with_suffix('.manifest.json')),
            canonical_profile_sha256=old['normalized_sha256'], canonical_fidelity_status='PASS',
            canonical_interval_sequence_unchanged=True, interval_count=manifest['interval_count'],
            queue_drain_ns=20_000_000, terminal_horizon_ns=duration + manifest['recipe']['drain_budget']['extension_ns'],
            full_received=full_n, full_late=full_l, f=ratio(Fraction(full_l, full_n)),
            steady_received=steady_n, steady_late=steady_l, steady_f=ratio(Fraction(steady_l, steady_n)))
        receipts[key] = dict(command=command, normalized_bytes=target.stat().st_size,
                             canonical_profile_path=str(baseline_path), profile_path=str(target))
        print('PROFILE', key, manifest['interval_count'], 'interval sequence unchanged', flush=True)
    conditions, runs = [], []
    direction_sets = dict(forward=['ul'], reverse=['dl'], both=['ul', 'dl'])
    # R is per line/direction. Pair calibration splits the new 9G physical offer equally.
    loads = [('single', 'L0', 10_000_000_000, 9_000_000_000),
             ('single', 'L8', 8_000_000_000, 10_000_000_000),
             ('single', 'L9', 9_000_000_000, 10_000_000_000),
             ('pair', 'calibration', 10_000_000_000, 4_500_000_000),
             ('pair', 'stress', 4_500_000_000, 5_000_000_000)]
    for (kind, load, capacity, offered), corridor, mode, traffic in itertools.product(
            loads, ('A', 'B'), direction_sets, ('U', 'X')):
        identifier = f'trace-{kind}-{corridor}-{mode}-{load}-{traffic}'
        active = direction_sets[mode]
        survivor = min(capacity, offered)
        main_rate = Fraction(survivor, 8 * 1542 * 1024)
        per_direction = {}
        for direction in active:
            window = windows[direction + '-' + corridor]
            fraction = Fraction(window['full_late'], window['full_received'])
            required, _ = episode_budget(survivor, fraction)
            steady_required, _ = episode_budget(survivor, Fraction(window['steady_late'], window['steady_received']))
            per_direction[direction] = dict(window_key=direction + '-' + corridor,
                f=ratio(fraction), required_episodes_uncapped=required,
                steady_f=window['steady_f'], steady_required_episodes_uncapped=steady_required)
        required = max(x['required_episodes_uncapped'] for x in per_direction.values())
        episodes = min(20, required)
        for direction, entry in per_direction.items():
            window = windows[entry['window_key']]
            steady_expected = main_rate * 9 * episodes * Fraction(window['steady_late'], window['steady_received'])
            entry['expected_late_main_using_registered_f'] = ratio(main_rate * 9 * episodes * Fraction(window['full_late'], window['full_received']))
            entry['expected_late_main_after_warmup'] = ratio(steady_expected)
            entry['steady_planning_target_431_met'] = steady_expected >= 431
            entry['steady_expected_368_met'] = steady_expected >= 368
        condition = dict(id=identifier, family='trace', trace_kind=kind, corridor=corridor,
            direction_mode=mode, active_source_directions=active, traffic=traffic,
            q_bytes_per_line=8_388_608, c_bps_per_line=capacity, offered_bps_per_line=offered,
            physical_offered_bps_per_direction=offered * (2 if kind == 'pair' else 1),
            assumed_surviving_bps_per_line=survivor, main_samples_per_second_estimate=ratio(main_rate),
            wire_bytes_for_sample_budget=1542, timing_mask_bits=10,
            model_t_ns=0, model_delta_ns=0, model_p='0', seed=1,
            cable_t_ns=(65_000_000 if corridor == 'A' else 100_000_000) if kind == 'pair' else None,
            required_episodes_uncapped=required, episodes_per_repeat=episodes, repeats=2,
            cap_limited=required > 20, directions=per_direction,
            warmup_seconds=2, steady_seconds=9, drain_seconds=1, reset_seconds=5,
            all_steady_planning_targets_met=all(x['steady_planning_target_431_met'] for x in per_direction.values()),
            status='PLANNING_ONLY', run_ids=[])
        assert condition['physical_offered_bps_per_direction'] <= 10_000_000_000
        assert ceil(Fraction((8_388_608 + 20 * (8_388_608 // 64)) * 8_000_000_000, capacity)) <= 20_000_000
        for repeat in range(1, 3):
            for episode in range(1, episodes + 1):
                run_id = f'{identifier}-r{repeat:02}-e{episode:02}'
                condition['run_ids'].append(run_id)
                runs.append(dict(id=run_id, family='trace', manifest=f'captures/{run_id}/manifest.json'))
        conditions.append(condition)
    assert len(conditions) == 60
    assert Counter(x['trace_kind'] for x in conditions) == Counter(single=36, pair=24)
    assert len({r['id'] for r in runs}) == len(runs)
    plan = dict(name='G-short-trace-registration', inventory='60 trace conditions only; captures not acquired',
        trace=dict(conditions=60, repeats=2), timing_mask_bits=10, minimum_pairs=368,
        planning_pairs_estimate=431, trace_episode_seconds=11, warmup_seconds=2,
        steady_seconds=9, drain_seconds=1, reset_seconds=5, trace_episodes_maximum=20,
        f_definition='full 11s received-source late share; full-source 15s median baseline; strict excess >20ms',
        episode_formula='min(20,ceil(431/(surviving_bps/(8*1542*1024)*9*f)))',
        caveat='Full-window f can overestimate the steady 9s late share; per-direction shortfalls remain explicit. Episode aggregation is not implemented by the matrix reporter.',
        traffic=dict(U=dict(q_bytes=[1522], wire_bytes=[1542], flows=1),
                     X=dict(q_bytes=[64, 512, 1522], wire_bytes=[84, 532, 1542], equal_packet_counts=True, flows=64)),
        pair_calibration='C=10G/R=4.5G per line; 9G aggregate, applying the G-short 9G no-Q offer',
        windows=windows, trace_conditions=conditions)
    matrix = register_episode_acceptance(dict(schema='hng-m6-matrix-v1', scope='software-only', plan=plan, runs=runs))
    write(examples / 'm6-short.json', matrix)
    write(out / 'generation-receipt.json', dict(core=str(args.core.resolve()), raw=str(args.raw.resolve()),
        m3_evidence=str(args.m3_evidence.resolve()), generator_sha256=sha(Path(__file__)),
        converter_sha256=sha(converter), pool_sha256=sha(pool), profiles=receipts,
        conditions=len(conditions), runs=len(runs),
        maximum_episodes=max(x['episodes_per_repeat'] for x in conditions),
        acceptance_decision='2026-11-01 08:1x',
        late_diagnostic_shortfall_conditions=sum(not x['all_late_diagnostic_targets_met'] for x in conditions),
        matrix_sha256=sha(examples / 'm6-short.json')))
    print('REGISTERED', len(conditions), 'conditions', len(runs), 'episode runs', flush=True)


if __name__ == '__main__':
    main()
