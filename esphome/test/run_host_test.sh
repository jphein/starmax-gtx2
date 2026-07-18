#!/usr/bin/env bash
# Offline byte-parity test for the gtx2_client C++ protocol port.
# Regenerates the golden vectors from the verified starmax_client Python, then compiles + runs
# the host test (no ESPHome/IDF/BLE). Exit non-zero on any parity failure.
set -euo pipefail
cd "$(dirname "$0")"

echo "== regenerating golden vectors from starmax_client =="
python3 gen_golden.py > golden_vectors.h

echo "== compiling host test (g++ -std=c++17) =="
g++ -std=c++17 -Wall -Wextra -I../components/gtx2_client \
    test_gtx2_protocol.cpp ../components/gtx2_client/gtx2_protocol.cpp -o /tmp/gtx2_host_test

echo "== running =="
/tmp/gtx2_host_test
