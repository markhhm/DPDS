# SPDX-License-Identifier: Apache-2.0
"""Cost-balanced condition assignments preserve every acquisition and report."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.g2.tests.test_m6_x710_full import bind

ROOT=Path(__file__).resolve().parents[3]


class JudgeShardTests(unittest.TestCase):
    def matrix(self):
        return json.loads((ROOT/'examples/m6-x710.json').read_text())

    def helper(self):
        self.assertTrue((ROOT/'tools/g2/judge_shards.py').is_file(), 'judge shard helper is missing')
        from tools.g2 import judge_shards
        return judge_shards

    def test_assignment_is_reproducible_complete_disjoint_and_cost_balanced(self):
        helper=self.helper();matrix=self.matrix();plan=matrix['plan']
        assignment=helper.assign_shards(plan)
        self.assertEqual(assignment,plan['judge_shards'])
        a,b=(set(assignment['shards'][name]['condition_ids']) for name in ('a','b'))
        self.assertEqual((len(a),len(b)),(180,180));self.assertFalse(a&b)
        self.assertEqual(a|b,{c['id'] for c in plan['conditions']})
        costs=[assignment['shards'][name]['estimated_cpu_ns'] for name in ('a','b')]
        self.assertLessEqual(200*abs(costs[0]-costs[1]),5*sum(costs))
        reordered=copy.deepcopy(plan);reordered['conditions'].reverse()
        self.assertEqual(helper.assign_shards(reordered),assignment)

    def test_shard_jobs_preserve_all_repeats_episodes_and_directions(self):
        helper=self.helper();matrix=self.matrix();before=copy.deepcopy(matrix);seen=[]
        for name in ('a','b'):
            job=helper.select_job(matrix,name,'f'*64)
            chosen=set(job['plan']['judge_selection']['condition_ids'])
            want=[r for r in matrix['runs'] if r['extensions']['m6']['condition_id'] in chosen]
            self.assertEqual(job['runs'],want)
            self.assertEqual({c['id'] for c in job['plan']['conditions']},chosen)
            self.assertNotIn('budget',job['plan'])
            seen.extend(r['id'] for r in job['runs'])
        self.assertEqual(sorted(seen),sorted(r['id'] for r in matrix['runs']))
        self.assertEqual(len(set(seen)),1960)
        self.assertEqual(matrix,before)

    def test_cost_includes_warmup_repeats_and_shared_bidirectional_native(self):
        helper=self.helper();plan=dict(warmup_seconds=1,traffic=dict(U=dict(wire_bytes=[100])))
        condition=dict(direction_mode='forward',input_kind='fixed',traffic='U',
            lines=[dict(offered_bps=800,c_bps=1000)],observation_seconds=1,repeats=2,episodes_per_repeat=3)
        self.assertEqual(helper.estimate_condition_ns(plan,condition),1560078876)
        condition['direction_mode']='both'
        self.assertEqual(helper.estimate_condition_ns(plan,condition),3120315504)

    def test_unmapped_reports_cannot_disappear_from_both_shards(self):
        helper=self.helper();matrix=self.matrix()
        matrix['runs'][0]['extensions']['m6']['condition_id']='unregistered'
        with self.assertRaises(ValueError):helper.select_job(matrix,'a','f'*64)

    def test_incomplete_or_duplicate_repeat_episode_direction_is_rejected(self):
        helper=self.helper();original=self.matrix()
        for kind in ('repeat','episode','direction','duplicate'):
            with self.subTest(kind=kind):
                matrix=copy.deepcopy(original)
                if kind=='duplicate':
                    matrix['runs'][1]['extensions']=copy.deepcopy(matrix['runs'][0]['extensions'])
                else:
                    condition=next(c for c in matrix['plan']['conditions'] if
                        (c['input_kind']=='trace' if kind=='episode' else
                         c['direction_mode']=='both' if kind=='direction' else c['repeats']>1))
                    index=next(i for i,r in enumerate(matrix['runs'])
                               if r['extensions']['m6']['condition_id']==condition['id'])
                    matrix['runs'].pop(index)
                with self.assertRaises(ValueError):helper.select_job(matrix,'a','f'*64)

    def test_closed_condition_cli_is_exclusive_and_never_opens_future_captures(self):
        helper=self.helper();matrix=bind(self.matrix())
        condition=matrix['plan']['judge_shards']['shards']['a']['condition_ids'][0]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'master.json';out=root/'jobs/job.json';out.parent.mkdir()
            source.write_text(json.dumps(matrix));original=source.read_bytes()
            args=[sys.executable,'-B','-m','tools.g2.judge_shards',str(source),
                  '--shard','a','--condition',condition,'--out',str(out)]
            result=subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            job=json.loads(out.read_text())
            self.assertEqual(job['plan']['judge_selection']['condition_ids'],[condition])
            want_paths=[str((source.parent/r['manifest']).resolve()) for r in matrix['runs']
                        if r['extensions']['m6']['condition_id']==condition]
            self.assertEqual([r['manifest'] for r in job['runs']],want_paths)
            want_roots=[str((source.parent/r['extensions']['m6']['raw_retention']['capture_directory']).resolve())
                        for r in matrix['runs'] if r['extensions']['m6']['condition_id']==condition]
            self.assertEqual([r['extensions']['m6']['raw_retention']['capture_directory'] for r in job['runs']],want_roots)
            self.assertEqual([r['extensions']['m6']['raw_retention']['keep_ns'] for r in job['runs']],
                [r['extensions']['m6']['raw_retention']['keep_ns'] for r in matrix['runs']
                 if r['extensions']['m6']['condition_id']==condition])
            saved=out.read_bytes();again=subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(again.returncode,2)
            self.assertEqual(out.read_bytes(),saved);self.assertEqual(source.read_bytes(),original)
            self.assertEqual(set(root.iterdir()),{source,out.parent})
            invalid=args[:-1]+[str(root/'invalid.json'),'--workers','0']
            self.assertEqual(subprocess.run(invalid,capture_output=True).returncode,2)
            self.assertFalse((root/'invalid.json').exists())
        with self.assertRaises(ValueError):helper.select_job(matrix,'b','f'*64,condition)


if __name__=='__main__':unittest.main()
