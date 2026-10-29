# SPDX-License-Identifier: Apache-2.0
"""Nonzero CPU IDs and small CI affinity masks retain four logical owners."""
from unittest.mock import patch
from eal_args import eal_arguments

with patch('os.sched_getaffinity', return_value={6, 9}):
    assert eal_arguments(4) == ['--lcores=0@6,1@9,2@6,3@9']
    assert eal_arguments(1) == ['--lcores=0@6']
with patch('os.sched_getaffinity', return_value={11}):
    assert eal_arguments(4) == ['--lcores=0@11,1@11,2@11,3@11']
print('affinity mapping for nonzero and oversubscribed CPUs: PASS')
