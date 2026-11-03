import json
import math
import random
import unittest
from great_scale_projection_witness import (
    N, analyze, heat, norm_sq, orientation_witness,
    project_mean, reflect, residual, rotate,
)

class ProjectionWitnessTests(unittest.TestCase):
    def test_dihedral_relations(self):
        x = tuple(range(N))
        self.assertEqual(reflect(reflect(x)), x)
        self.assertEqual(rotate(x, N), x)
        self.assertEqual(reflect(rotate(x)), rotate(reflect(x), -1))

    def test_projection_orthogonality_and_commutation_random(self):
        rng = random.Random(919)
        for _ in range(2500):
            x = tuple(rng.uniform(-10, 10) for _ in range(N))
            p, q = project_mean(x), residual(x)
            self.assertAlmostEqual(math.fsum(q), 0, delta=1e-12)
            self.assertAlmostEqual(norm_sq(x), norm_sq(p) + norm_sq(q), delta=1e-10)
            self.assertEqual(project_mean(reflect(x)), reflect(p))
            self.assertAlmostEqual(norm_sq(q), norm_sq(residual(reflect(x))), delta=1e-10)
            self.assertAlmostEqual(orientation_witness(reflect(x)), -orientation_witness(x), delta=1e-12)

    def test_orientation_erased_by_projection(self):
        x = tuple(math.sin(2 * math.pi * j / N) for j in range(N))
        self.assertAlmostEqual(orientation_witness(x), -orientation_witness(reflect(x)), places=12)
        self.assertGreater(abs(orientation_witness(x)), 1)
        self.assertEqual(project_mean(x), project_mean(reflect(x)))
        self.assertAlmostEqual(norm_sq(residual(x)), norm_sq(residual(reflect(x))))
        self.assertEqual(analyze(x).evidence_sha256, analyze(x).evidence_sha256)
        self.assertNotEqual(analyze(x).evidence_sha256, analyze(reflect(x)).evidence_sha256)

    def test_heat_identifiability(self):
        for v in [0, .01, .2, .45, .5, .8, 1]:
            self.assertAlmostEqual(heat(v), heat(1-v))
        for bad in [float('nan'), float('inf'), -.1, 1.01, True, '0.1']:
            with self.assertRaises(ValueError):
                heat(bad)

    def test_input_fail_closed(self):
        vectors = [[], [1]*8, [1]*10, [0]*8+[float('nan')], [0]*8+[float('inf')], [0]*8+[True], '123456789']
        for bad in vectors:
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    analyze(bad)

    def test_canonical_report_is_finite_and_stable(self):
        x = list(range(9))
        r = analyze(x)
        self.assertEqual(json.loads(r.to_json())['n'], 9)
        self.assertLess(r.projection_commutator_error, 1e-12)
        self.assertEqual(r.to_json(), analyze(x).to_json())

if __name__ == '__main__':
    unittest.main()
