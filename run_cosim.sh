#!/usr/bin/env bash
# =============================================================================
# gem5 <-> pyuvm/VCS CHI co-simulation launcher.
#
#   ./run_cosim.sh [extra gem5 args]
#
# Environment:
#   C2XM_SOCK        socket path           (default /tmp/c2xm_cosim.sock)
#   C2XM_QUANTUM     barrier quantum       (default 100 cycles)
#   C2XM_GEM5_ARGS   extra gem5 args, e.g. "--max-insts 200000"
#   C2XM_NUM_CPUS    gem5 CPU count        (default 1)
#                   NOTE: >1 CPU requires --enable-difftest, which needs a
#                   NEMU proxy (LSQ golden-memory updates are unconditional
#                   in this tree and crash without difftest on multicore).
#
# Workload: XiangShan GCPT checkpoint (CoreMark), restored like the user's
# vanilla 2x2 runs.
# =============================================================================
set -u

C2XM_ROOT="$(cd "$(dirname "$0")" && pwd)"
# gem5 tree: submodule checkout inside this repo, or the source-tree path.
if [ -n "${GEM5_ROOT:-}" ]; then
    :
elif [ -d "$C2XM_ROOT/XS-DSU-GEM5/src/mem/cache/CHI" ]; then
    GEM5_ROOT="$C2XM_ROOT/XS-DSU-GEM5"
else
    GEM5_ROOT=/nfs/home/majunhong/project/XS-DSU-GEM5
fi
# repo layout: the env sits at the repo root; source-tree layout:
# c2xm_exp/run_cosim.sh with the env in c2xm_exp/c2xm_pyuvm_env.
if [ -f "$C2XM_ROOT/c2xm_env.py" ]; then
    TB_DIR="$C2XM_ROOT"
else
    TB_DIR="$C2XM_ROOT/c2xm_pyuvm_env"
fi
SOCK=${C2XM_SOCK:-/tmp/c2xm_cosim.sock}
QUANTUM=${C2XM_QUANTUM:-100}
NUM_CPUS=${C2XM_NUM_CPUS:-1}
CLK_NS=${C2XM_CLK_NS:-0.334}   # match gem5's default 3 GHz system clock
WAVES=${C2XM_WAVES:-none}       # none | fsdb | vcd  (TB waveform dump)
MAX_SYNCS=${C2XM_MAX_SYNCS:-0}  # stop after N barriers (0 = run to exit);
                                # NOTE gem5 --max-insts kills this workload
GCPT=${C2XM_GCPT:-/nfs/home/majunhong/workloads/ready-to-run/coremark-2-iteration.bin}
RUN_DIR=$(mktemp -d /tmp/c2xm_cosim_run.XXXX)
GEM5_LOG="$RUN_DIR/gem5.log"
TB_LOG="$RUN_DIR/tb.log"

rm -f "$SOCK"
cd "$GEM5_ROOT"

echo "[run_cosim] gem5 -> $GEM5_LOG"
CHI_COSIM_SOCKET="$SOCK" CHI_COSIM_QUANTUM="$QUANTUM" \
    nohup build/RISCV/gem5.opt \
    --outdir="$RUN_DIR/m5out" \
    configs/example/kmhv2_chi_2x2_router_cosim.py \
    --generic-rv-cpt "$GCPT" --raw-cpt --num-cpus "$NUM_CPUS" \
    ${C2XM_GEM5_ARGS:-} > "$GEM5_LOG" 2>&1 &
GEM5_PID=$!

# The TB retries connecting for 60s, so just wait for the socket to appear.
echo "[run_cosim] waiting for $SOCK ..."
for _ in $(seq 1 600); do [ -S "$SOCK" ] && break; sleep 1; done
[ -S "$SOCK" ] || { echo "[run_cosim] gem5 never created $SOCK"; tail -5 "$GEM5_LOG"; kill $GEM5_PID; exit 1; }

echo "[run_cosim] VCS/pyuvm TB -> $TB_LOG"
cd "$TB_DIR"
# cocotb imports through pytest's rewriter; its bytecode cache does not
# always notice python edits -- clear it to be safe.
rm -rf __pycache__
C2XM_COSIM_SOCK="$SOCK" C2XM_COSIM_CLK_NS="$CLK_NS" \
    C2XM_COSIM_MAX_SYNCS="$MAX_SYNCS" \
    make TESTCASE=C2xmCosimTest WAVES="$WAVES" sim > "$TB_LOG" 2>&1
TB_RC=$?
# relocate the waveform next to the logs if one was produced
for ext in fsdb vcd; do
    [ -f "waves.$ext" ] && mv -f "waves.$ext" "$RUN_DIR/"
done

# Give gem5 a moment to finish (it exits on m5_exit / peer close).
wait $GEM5_PID 2>/dev/null
GEM5_RC=$?

echo "==================== gem5 tail ===================="
tail -4 "$GEM5_LOG"
echo "==================== tb tail ======================"
tail -6 "$TB_LOG"
echo "==================================================="
echo "[run_cosim] tb_rc=$TB_RC gem5_rc=$GEM5_RC logs: $RUN_DIR"
[ "$TB_RC" -eq 0 ] && exit 0 || exit "$TB_RC"
