#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
XLS_ROOT=${XLS_ROOT:-}
BAZEL=${BAZEL:-bazel}
DEFAULT_JOBS=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
JOBS=${XLS_BAZEL_JOBS:-$DEFAULT_JOBS}

if [[ -z "$XLS_ROOT" ]]; then
  echo "Set XLS_ROOT to an XLS source checkout" >&2
  exit 2
fi
if [[ ! -f "$XLS_ROOT/MODULE.bazel" ]]; then
  echo "XLS source tree not found: $XLS_ROOT" >&2
  exit 2
fi
if ! command -v "$BAZEL" >/dev/null 2>&1 && [[ ! -x "$BAZEL" ]]; then
  echo "Bazel executable not found: $BAZEL" >&2
  exit 2
fi

link="$XLS_ROOT/c2xm_cosim_backend"
if [[ -e "$link" && ! -L "$link" ]]; then
  echo "Refusing to replace non-symlink: $link" >&2
  exit 2
fi
ln -sfn "$ROOT/xls_backend" "$link"

cd "$XLS_ROOT"
startup_args=()
build_args=(-c opt --jobs="$JOBS")
if [[ -n "${XLS_BAZEL_OUTPUT_ROOT:-}" ]]; then
  startup_args+=(--output_user_root="$XLS_BAZEL_OUTPUT_ROOT")
fi
if [[ -n "${XLS_BAZEL_REPO_CACHE:-}" ]]; then
  build_args+=(--repository_cache="$XLS_BAZEL_REPO_CACHE")
fi
if [[ -n "${XLS_BAZEL_SANDBOX_BASE:-}" ]]; then
  build_args+=(--sandbox_base="$XLS_BAZEL_SANDBOX_BASE")
fi

"$BAZEL" "${startup_args[@]}" build "${build_args[@]}" "$@" \
  //c2xm_cosim_backend:xls_proc_rpc_main

mkdir -p "$ROOT/bin"
cp -f "$XLS_ROOT/bazel-bin/c2xm_cosim_backend/xls_proc_rpc_main" \
  "$ROOT/bin/xls_proc_rpc_main"
echo "Built $ROOT/bin/xls_proc_rpc_main"
