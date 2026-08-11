"""Historical research: outcome labels, calibration and dataset export.

Everything in this package answers "what happened *after* a signal". Nothing in
it may be consumed by feature computation or scoring -- that separation is the
entire point of phase 5, and it is enforced structurally rather than by
convention: labels live in their own tables, are written by their own service,
and are joined to features only at export time, where the two column groups are
declared explicitly.
"""
