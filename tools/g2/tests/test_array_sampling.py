# SPDX-License-Identifier: Apache-2.0
"""The bulk selector must retain exactly the scalar SHA-256 population."""
import hashlib
import unittest
from unittest.mock import patch

import numpy as np

from tools.g2.array_sampling import _sha256_words, sample_mask
from tools.g2.online import sample


DTYPE = np.dtype([(name, '<u8') for name in (
    'ingress_port', 'source', 'destination', 'source_port',
    'destination_port', 'identity', 'identity_kind')])


def items(count=192):
    rows = np.zeros(count, dtype=DTYPE)
    ordinal = np.arange(count, dtype=np.uint64)
    rows['ingress_port'] = ordinal % 3
    rows['source'] = 0xC0000201 + ordinal % 4
    rows['destination'] = 0xC6336401 + ordinal % 2
    rows['source_port'] = 65535 - ordinal % 3
    rows['destination_port'] = 443
    rows['identity'] = ordinal
    rows['identity_kind'] = 1
    extremes = [0, 9, 10, 99, 100, 2**53 - 1, 2**53, 2**53 + 1, 2**64 - 1]
    rows['identity'][:min(count, len(extremes))] = extremes[:count]
    return rows


def scalar_mask(run, seed, bits, rows):
    result = []
    for row in rows:
        port = int(row['ingress_port'])
        if row['identity_kind'] == 2:
            flow = f'software-l2-{port}'
        else:
            flow = (f"{int(row['source']):08x}:{int(row['destination']):08x}:"
                    f"{int(row['source_port'])}:{int(row['destination_port'])}")
        external = f"{flow}:{int(row['identity'])}"
        result.append(sample(dict(run=run, ingress_port=port, external_identity=external), seed, bits))
    return np.asarray(result, dtype=bool)


class ArraySamplingTests(unittest.TestCase):
    def test_sha256_matches_hashlib_at_padding_boundaries(self):
        lengths = np.asarray([0, 1, 55, 56, 63, 64, 65, 119, 120, 127, 128, 129, 255], dtype=np.uint64)
        data = np.arange(len(lengths) * 255, dtype=np.uint64).astype(np.uint8).reshape(-1, 255)
        actual = _sha256_words(data, lengths)
        for index, length in enumerate(lengths):
            digest = hashlib.sha256(data[index, :int(length)].tobytes()).digest()
            np.testing.assert_array_equal(actual[index], np.frombuffer(digest, dtype='>u4'))

    def test_stage1_matches_scalar_across_seeds_masks_and_uint64_extremes(self):
        rows = items()
        for seed in (0, 17, 2**64 - 1):
            for bits in (0, 1, 4, 10):
                with self.subTest(seed=seed, bits=bits):
                    np.testing.assert_array_equal(sample_mask('demo', seed, bits, rows),
                                                  scalar_mask('demo', seed, bits, rows))

    def test_escaped_unicode_and_long_runs_preserve_canonical_encoding(self):
        rows = items(64)
        for run in ('', '"\\\n\t台灣😀', 'x' * 55, 'x' * 56, 'x' * 64, 'x' * 128, 'x' * 257):
            with self.subTest(run=run):
                np.testing.assert_array_equal(sample_mask(run, 23, 3, rows),
                                              scalar_mask(run, 23, 3, rows))

    def test_software_l2_and_reordered_views_match_scalar(self):
        rows = items()[::-2]
        rows['identity_kind'][::2] = 2
        np.testing.assert_array_equal(sample_mask('mixed', 7, 2, rows),
                                      scalar_mask('mixed', 7, 2, rows))

    def test_chunk_boundary_and_every_decimal_width_match_scalar(self):
        rows = items(8209)
        rows['ingress_port'] = 65535
        rows['source'] = 2**32 - 1
        rows['destination'] = 0
        rows['source_port'] = 0
        rows['destination_port'] = 65535
        rows['identity'][::2] = np.arange(len(rows[::2]), dtype=np.uint64) + 2**63
        rows['identity'][:20] = [10**width - 1 for width in range(1, 20)] + [2**64 - 1]
        np.testing.assert_array_equal(sample_mask('chunks', 2**63, 4, rows),
                                      scalar_mask('chunks', 2**63, 4, rows))

    def test_uniform_flow_chunks_match_scalar_without_sorting(self):
        rows = items(8209)
        for field in ('ingress_port', 'source', 'destination', 'source_port', 'destination_port'):
            rows[field] = rows[field][0]
        rows['identity'][8190:8195] = [2**53 - 1, 2**53, 2**53 + 1, 2**64 - 2, 2**64 - 1]
        run = 'uniform-台灣😀"\\\n'
        for kind in (1, 2, None):
            rows['identity_kind'] = 1 if kind is None else kind
            given = rows if kind is not None else rows[[name for name in rows.dtype.names if name != 'identity_kind']]
            expected = scalar_mask(run, 2**64 - 1, 3, rows)
            with self.subTest(kind=kind), patch('tools.g2.array_sampling.np.unique',
                    side_effect=AssertionError('uniform flow must not sort')):
                np.testing.assert_array_equal(sample_mask(run, 2**64 - 1, 3, given), expected)

    def test_each_varying_flow_field_keeps_distinct_hashes(self):
        for field in ('ingress_port', 'source', 'destination', 'source_port', 'destination_port', 'identity_kind'):
            rows = items(64)
            rows['source_port'] = 1111
            for name in rows.dtype.names:
                if name != 'identity':
                    rows[name] = rows[name][0]
            rows[field][32:] += 1
            with self.subTest(field=field):
                np.testing.assert_array_equal(sample_mask('mixed-field', 9, 1, rows),
                                              scalar_mask('mixed-field', 9, 1, rows))

    def test_empty_and_default_stage1_kind(self):
        self.assertEqual(sample_mask('empty', 0, 10, items(0)).dtype, np.dtype(bool))
        rows = items(32)
        fields = [name for name in rows.dtype.names if name != 'identity_kind']
        np.testing.assert_array_equal(sample_mask('default', 5, 2, rows[fields]),
                                      scalar_mask('default', 5, 2, rows))

    def test_rejects_invalid_seed_mask_and_identity_kind(self):
        for seed, bits in ((-1, 1), (2**64, 1), (0, -1), (0, 11), (True, 1)):
            with self.subTest(seed=seed, bits=bits), self.assertRaises(ValueError):
                sample_mask('demo', seed, bits, items(1))
        rows = items(1)
        rows['identity_kind'] = 0
        with self.assertRaises(ValueError):
            sample_mask('demo', 0, 1, rows)


if __name__ == '__main__':
    unittest.main()
