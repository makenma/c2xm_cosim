#!/usr/bin/env bash
# gem5 <-> C2XM XLS Proc-IR co-simulation launcher (no RTL/VCS involved).
set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
GEM5_ROOT=${GEM5_ROOT:-$ROOT/XS-DSU-GEM5}
GEM5_BIN=${C2XM_GEM5_BIN:-$GEM5_ROOT/build/RISCV/gem5.opt}
GCPT=${C2XM_GCPT:-}
QUANTUM=${C2XM_QUANTUM:-100}
NUM_CPUS=${C2XM_NUM_CPUS:-1}
MAX_SYNCS=${C2XM_MAX_SYNCS:-0}
XLS_RUNTIME=${C2XM_XLS_RUNTIME:-$ROOT/bin/xls_proc_rpc_main}
XLS_IR=${C2XM_XLS_IR:-}
PYTHON=${C2XM_PYTHON:-python3}

if [[ ! -d "$GEM5_ROOT" ]]; then
  echo "gem5 source tree not found: $GEM5_ROOT" >&2
  exit 2
fi
if [[ ! -x "$GEM5_BIN" ]]; then
  echo "gem5 binary not found: $GEM5_BIN" >&2
  exit 2
fi
if [[ ! -x "$XLS_RUNTIME" ]]; then
  echo "XLS runtime not found: $XLS_RUNTIME" >&2
  exit 2
fi
if ! command -v "$PYTHON" >/dev/null 2>&1 && [[ ! -x "$PYTHON" ]]; then
  echo "Python runtime not found: $PYTHON" >&2
  exit 2
fi
if [[ -z "$XLS_IR" ]]; then
  echo "Set C2XM_XLS_IR to the optimized C2XM Proc IR" >&2
  exit 2
fi
if [[ ! -f "$XLS_IR" ]]; then
  echo "optimized XLS IR not found: $XLS_IR" >&2
  exit 2
fi
if [[ -z "$GCPT" ]]; then
  echo "Set C2XM_GCPT to a gem5 GCPT workload" >&2
  exit 2
fi
if [[ ! -f "$GCPT" ]]; then
  echo "gem5 checkpoint not found: $GCPT" >&2
  exit 2
fi
if [[ ! -w "$GCPT" ]]; then
  echo "gem5 checkpoint must be writable: $GCPT" >&2
  exit 2
fi

RUN_DIR=$(mktemp -d /tmp/c2xm_xls_cosim_run.XXXXXX)
SOCK=${C2XM_SOCK:-$RUN_DIR/cosim.sock}
GEM5_LOG=$RUN_DIR/gem5.log
XLS_LOG=$RUN_DIR/xls.log

if [[ -e "$SOCK" ]]; then
  echo "Refusing to replace an existing socket: $SOCK" >&2
  exit 2
fi
# Failed simulations should leave logs, not multi-GB core dumps.
ulimit -c 0
cd "$GEM5_ROOT"
echo "[run_xls_cosim] gem5 log: $GEM5_LOG"
GEM5_COMMAND=(
  "$GEM5_BIN" --outdir="$RUN_DIR/m5out"
  configs/example/kmhv2_chi_2x2_router_cosim.py
  --generic-rv-cpt "$GCPT" --raw-cpt --num-cpus "$NUM_CPUS"
)
if [[ -n "${C2XM_GEM5_ARGS:-}" ]]; then
  GEM5_EXTRA_ARGS=()
  read -r -a GEM5_EXTRA_ARGS <<< "$C2XM_GEM5_ARGS"
  GEM5_COMMAND+=("${GEM5_EXTRA_ARGS[@]}")
fi
GEM5_COMMAND+=("$@")
CHI_COSIM_SOCKET="$SOCK" CHI_COSIM_QUANTUM="$QUANTUM" \
  "${GEM5_COMMAND[@]}" >"$GEM5_LOG" 2>&1 &
GEM5_PID=$!

cleanup() {
  if kill -0 "$GEM5_PID" 2>/dev/null; then
    kill "$GEM5_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

for ((attempt = 0; attempt < 600; attempt++)); do
  [[ -S "$SOCK" ]] && break
  kill -0 "$GEM5_PID" 2>/dev/null || break
  sleep 1
done
if [[ ! -S "$SOCK" ]]; then
  echo "gem5 did not create $SOCK" >&2
  tail -n 20 "$GEM5_LOG"
  exit 1
fi

cd "$ROOT"
echo "[run_xls_cosim] XLS log: $XLS_LOG"
"$PYTHON" xls_cosim_peer.py --socket "$SOCK" --xls-runtime "$XLS_RUNTIME" \
  --ir "$XLS_IR" --max-syncs "$MAX_SYNCS" >"$XLS_LOG" 2>&1
XLS_RC=$?
if [[ "$XLS_RC" -ne 0 ]]; then
  cleanup
fi

wait "$GEM5_PID" 2>/dev/null
GEM5_RC=$?
trap - EXIT INT TERM

echo "==================== gem5 tail ===================="
tail -n 12 "$GEM5_LOG"
echo "===================== XLS tail ====================="
tail -n 12 "$XLS_LOG"
echo "===================================================="
echo "[run_xls_cosim] xls_rc=$XLS_RC gem5_rc=$GEM5_RC logs=$RUN_DIR"
[[ "$XLS_RC" -eq 0 && "$GEM5_RC" -eq 0 ]] || exit 1
if [[ "${C2XM_REQUIRE_WRITES:-0}" -eq 1 ]]; then
  grep -Eq "\[xls\] done.*'axi_writes': [1-9][0-9]*" "$XLS_LOG" || {
    echo "Required C2XM AXI write coverage was not observed" >&2
    exit 1
  }
fi
if [[ "$MAX_SYNCS" -eq 0 ]]; then
  grep -Eq 'Exiting.*m5_exit' "$GEM5_LOG" || {
    echo "Workload did not reach m5_exit" >&2
    exit 1
  }
  if [[ "$(basename "$GCPT")" == coremark-2-iteration.bin ]]; then
    grep -Eq 'crcfinal[[:space:]]*:[[:space:]]*0x72be' "$GEM5_LOG" || {
      echo "CoreMark final CRC does not match 0x72be" >&2
      exit 1
    }
  fi
  echo "[run_xls_cosim] PASS: complete workload and exit checks"
else
  echo "[run_xls_cosim] PASS: bounded smoke test only"
fi
