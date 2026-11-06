#!/usr/bin/env bash
set -euo pipefail

TARGET=$(python3 -c "import math; print(math.pi**2/6)")
PARTIAL=$(python3 -c "vals = [1/(n*n) for n in range(1, 22)]; print(sum(vals))")

echo "PARTIAL vs TARGET within 0.01."
if python3 -c "import sys; sys.exit(0 if abs($PARTIAL - $TARGET) <= 0.01 else 1)"; then
    echo "CONVERGENCE CERTIFIED: partial=$PARTIAL target=$TARGET"
    exit 0
else
    echo "CONVERGENCE FAILED: partial=$PARTIAL target=$TARGET delta exceeds 0.01"
    exit 1
fi
