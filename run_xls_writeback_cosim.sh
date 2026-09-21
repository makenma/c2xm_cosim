#!/usr/bin/env bash
# Small-cache CPU workload profile to exercise HNF dirty-victim writes.
# The profile requires at least one C2XM AXI write as well as the normal
# workload exit and CRC checks. See XLS_COSIM.md for the tested result.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
export C2XM_REQUIRE_WRITES=1
exec bash "$ROOT/run_xls_cosim.sh" \
  --no-pf \
  --l1d_size=2kB --l1d_assoc=2 \
  --l2_size=8kB --l2_assoc=8 --l2_slices=1 \
  '--param=system.home_node[0].slcsf.slc_num_sets=8' \
  '--param=system.home_node[0].slcsf.slc_num_ways=2' \
  '--param=system.home_node[0].slcsf.sf_num_sets=32' \
  '--param=system.home_node[0].slcsf.sf_num_ways=4' \
  "$@"
