# C2XM XLS Proc co-simulation

This flow runs the optimized C2XM XLS Proc IR directly against gem5. It does
not compile or simulate RTL. The Python peer translates the repository's
newline-JSON co-simulation protocol into C2XM boundary-channel values, while a
small C++ helper owns the real XLS JIT Proc runtime and its persistent state.

```text
gem5 ChiCosimBridge
        |
        | CHI flits, memory requests, barriers
        v
xls_cosim_peer.py
        |
        | line-oriented RPC
        v
xls_proc_rpc_main -> XLS JIT Proc runtime -> optimized C2XM Proc IR
```

AXI reads and writes emitted by C2XM are sent back through the bridge, so
gem5's DDR remains the single memory image. C2XM behavior is not reimplemented
in Python.

## Prerequisites

- Linux with Python 3 and Unix-domain sockets.
- This repository cloned with the `XS-DSU-GEM5` submodule initialized.
- A RISCV gem5 binary built from that submodule.
- An XLS source checkout and Bazel, for building the RPC helper.
- Optimized C2XM Proc IR (`core.opt.ir`) generated outside this repository.
- A writable gem5 GCPT workload.

The generated IR, workload checkpoint and compiled binaries are intentionally
not stored in git.

## Build the XLS runtime helper

The build script links `xls_backend/` into the supplied XLS checkout as the
Bazel package `//c2xm_cosim_backend`, builds it, and copies the resulting
binary to `bin/xls_proc_rpc_main`.

```bash
XLS_ROOT=/path/to/xls ./build_xls_backend.sh
```

The following variables are optional:

| Variable | Default | Meaning |
|---|---|---|
| `BAZEL` | `bazel` | Bazel executable |
| `XLS_BAZEL_JOBS` | online CPU count | parallel build jobs |
| `XLS_BAZEL_OUTPUT_ROOT` | Bazel default | output user root |
| `XLS_BAZEL_REPO_CACHE` | Bazel default | repository cache |
| `XLS_BAZEL_SANDBOX_BASE` | Bazel default | sandbox directory |

Additional arguments to `build_xls_backend.sh` are forwarded to `bazel build`.
Compiler and runtime compatibility workarounds, when a host needs them, should
be supplied by that host's toolchain environment rather than added to this
repository.

## Run with gem5

Build gem5 first if needed:

```bash
cd XS-DSU-GEM5
scons build/RISCV/gem5.opt -j32
cd ..
```

Then run the complete workload:

```bash
C2XM_XLS_IR=/path/to/core.opt.ir \
C2XM_GCPT=/path/to/coremark-2-iteration.bin \
./run_xls_cosim.sh
```

`run_xls_cosim.sh` requires both processes to exit successfully. A full run
must reach `m5_exit`; when the workload basename is
`coremark-2-iteration.bin`, the launcher also requires final CRC `0x72be`.
Logs and gem5 output are retained in the printed `/tmp/c2xm_xls_cosim_run.*`
directory.

Useful configuration variables:

| Variable | Default | Meaning |
|---|---|---|
| `GEM5_ROOT` | `XS-DSU-GEM5` | gem5 source/configuration tree |
| `C2XM_GEM5_BIN` | `$GEM5_ROOT/build/RISCV/gem5.opt` | gem5 executable or wrapper |
| `C2XM_GCPT` | required | writable raw GCPT workload |
| `C2XM_XLS_IR` | required | optimized C2XM Proc IR |
| `C2XM_XLS_RUNTIME` | `bin/xls_proc_rpc_main` | XLS RPC helper |
| `C2XM_PYTHON` | `python3` | Python interpreter |
| `C2XM_QUANTUM` | `100` | logical Proc ticks per gem5 barrier |
| `C2XM_NUM_CPUS` | `1` | gem5 CPU count; multicore is not validated |
| `C2XM_MAX_SYNCS` | `0` | barrier limit; zero runs to workload exit |
| `C2XM_GEM5_ARGS` | empty | whitespace-separated extra gem5 arguments |
| `C2XM_SOCK` | fresh temporary path | bridge Unix socket |

Command-line arguments after `run_xls_cosim.sh` are also forwarded to gem5.
A bounded startup test can be run with `C2XM_MAX_SYNCS=100`; it checks the
protocol path but does not claim workload completion.

## Exercise the write path

The normal CoreMark configuration may perform no C2XM AXI writes because CPU
stores stay in caches or use the configured bypass path. The supplied stress
launcher reduces the cache hierarchy so dirty SLC victims produce real CHI
writes through C2XM:

```bash
C2XM_XLS_IR=/path/to/core.opt.ir \
C2XM_GCPT=/path/to/coremark-2-iteration.bin \
./run_xls_writeback_cosim.sh
```

This launcher adds a nonzero `axi_writes` requirement to the normal exit and
CRC checks. Its cache sizes are a verification profile, not a production
configuration.

## Protocol regression

The supplemental regression uses `fake_gem5.py` and the real C2XM XLS Proc
network. It checks returned read bytes and committed write bytes independently
of a full gem5 workload:

```bash
python3 run_xls_protocol_tests.py \
  --xls-runtime bin/xls_proc_rpc_main \
  --ir /path/to/core.opt.ir
```

It covers read, write, mixed, bypass and combined traffic, plus a pressure
case with 80 reads and 40 writes. Results are written under `results/` unless
`--output` is supplied.

## Verified reference results

The following results were observed on 2026-09-21 with repository commit
`69ec18a`, its gem5 submodule revision, one CPU, quantum 100 and the same
optimized C2XM Proc IR:

| Test | Result | C2XM AXI reads/writes | Syncs / Proc ticks | Peer wall time |
|---|---|---:|---:|---:|
| CoreMark, default cache | CRC `0x72be`, `m5_exit` | 228 / 0 | 9869 / 986900 | 87.549 s |
| CoreMark, writeback profile | CRC `0x72be`, `m5_exit` | 360 / 34 | 9979 / 997900 | 88.752 s |
| Six protocol scenarios | all passed | pressure: 80 / 40 | scenario-dependent | 3.7-4.2 s each |

These are functional results for the exercised single-core, aligned-transfer
paths. They are not proof of every C2XM behavior. Partial and arbitrary
unaligned transfers, injected errors and multicore traffic remain outside the
validated scope. XLS Proc queues also do not model finite RTL FIFO
backpressure, and logical Proc ticks are not synthesized RTL clock cycles, so
the wall times are not RTL performance estimates.

## Adapter details

- Gem5 `WriteNoSnpFull` opcode `0x5c` is translated to C2XM's `0x1c`.
- Byte-count request sizes are translated to the CHI log2 encoding.
- The adapter allocates a local 12-bit transaction ID and restores gem5's
  source/transaction identity on responses.
- A 64-byte write is split into C2XM's two 32-byte DAT beats (`data_id` 0/2).
- RetryAck/PCrdGrant is absorbed locally and the request is resubmitted with
  `AllowRetry=0`.
- C2XM AXI bursts are proxied through `mem_read`/`mem_write`, preserving gem5
  memory as the source of truth.
- Each gem5 barrier advances the persistent XLS Proc runtime by the requested
  number of logical ticks, drains outputs, completes memory traffic, and only
  then acknowledges the barrier.
