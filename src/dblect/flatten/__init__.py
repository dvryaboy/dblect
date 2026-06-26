"""Inner-flatten row-drop detection grounded against propagated array non-emptiness.

The structural detector in :mod:`dblect.sql.patterns` flags an inner ``UNNEST`` that
can drop a parent row, clearing only the locally provable cases (an outer form, a
literal array). This package adds the cross-model layer: it propagates the
``array_nonemptiness`` property over the manifest column graph and feeds the result
back into the detector, so an ``UNNEST`` of an array a model rebuilt non-empty
upstream stays quiet while an ``UNNEST`` of a raw source array keeps firing.
"""

from dblect.flatten.detector import make_array_nonemptiness_detectors

__all__ = ["make_array_nonemptiness_detectors"]
