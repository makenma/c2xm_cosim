# c2xm_pyuvm_env — pyuvm/cocotb environment for `c2xm_top`

Skeleton verification environment for the generated C2XM core (CHI
ReadNoSnp/WriteNoSnp in, AXI4 master out).  It compiles and runs a real
end-to-end stimulus today; the components are deliberately thin so the
checker/coverage layer can be added on top.

```
c2xm_pyuvm_env/
├── Makefile              VCS + cocotb entry point (make / make cosim[-fake])
├── tb/
│   ├── c2xm_tb_top.sv    DUT wrapper: config tie-offs, signal exposure, debug taps
│   └── dbg_pool.sv       optional (make DBG_POOL=1) transaction-pool dump
├── chi_flit.py           gem5 CHI flit mirror + RTL wire pack/unpack
├── chi_txn.py            ReadNoSnp / WriteNoSnp transactions
├── gem5_adaptor.py       flit -> transaction (standalone tests)
├── driver.py             transaction -> CHI link flits (ChiLinkBfm + uvm driver)
├── chi_mon.py            CHI link monitor
├── axi_mon.py            AXI master monitor
├── axi_slave_stub.py     placeholder B/R responder (standalone tests)
├── c2xm_env.py           uvm_env wiring (standalone / cosim component sets)
├── c2xm_test.py          three standalone tests + C2xmCosimTest
├── tb_config.py          clock, node ids, timeouts
├── tb_signals.py         X-safe signal readers
├── cosim_protocol.py     co-sim message ABI (newline JSON over UNIX socket)
├── cosim_transport.py    socket client + I/O threads + flit (de)serialization
├── snf_agent.py          gem5 SN-F ABI adapter (txnid<->pool slot, field fixups)
├── cosim_responder.py    chi_mon TX flits -> snf_agent -> gem5
├── axi_ddr_proxy.py      AXI slave proxying every burst to gem5's DDR
├── cosim_runtime.py      barrier loop, gated clock, MemBroker, bypass proxy
└── fake_gem5.py          stand-alone gem5 peer for co-sim bring-up (M1)
```

## Run

```bash
cd c2xm_pyuvm_env
make                                  # all three standalone tests
make TESTCASE=C2xmReadNoSnpTest      # one test
make DBG_POOL=1 TESTCASE=C2xmWriteNoSnpTest   # + transaction pool dump
make GUI=1                            # rebuild with -kdb for Verdi
C2XM_ROOT=/path/to/other/generated make

# Co-simulation (see "gem5 co-simulation" below)
make cosim-fake SCENARIO=all          # TB + fake_gem5, no gem5 needed
make cosim SOCKET=/tmp/x.sock         # TB against a listening gem5 peer
```

`make` compiles `../c2xm_generated_dsl_core_20260916` (same file order as its
`filelist_rtl.f`) plus `tb/c2xm_tb_top.sv` with VCS and runs cocotb.

Current result:

```
** c2xm_test.C2xmSmokeTest        PASS **
** c2xm_test.C2xmReadNoSnpTest    PASS **
** c2xm_test.C2xmWriteNoSnpTest   PASS **
** c2xm_test.C2xmCosimTest        PASS **   (skipped without C2XM_COSIM_SOCK)
** TESTS=4 PASS=4 FAIL=0 SKIP=0 **
```

`make cosim-fake SCENARIO=all` additionally proves the full co-sim loop
against `fake_gem5.py` (reads' data verified byte-exact against the peer's
DDR, writes' DDR landing verified, RSP/DAT contract checked, barrier
protocol exercised).

## Structure

```
                        +-----------------+
  RawReq / RawDat  ---> |  Gem5Adaptor    | --- ChiTxnItem ---> +--------------+
  (flit_export)         | flit -> txn     |                    | ChiLinkDriver|
                        +-----------------+                    |  ChiLinkBfm  |
                                                               +------+-------+
                                                                      | RXREQ / RXDAT
                                                                      v
                                                                  +-------+
                                                                  | c2xm  |
                                                                  | _top  |
                                                                  +---+---+
                                                         AXI AW/W/B/AR/R |
                                                                      v
                                                            +------------------+
                                          chi_mon  <------  | axi_mon / stub   |
                                          (analysis ports)  +------------------+
```

* **gem5_adaptor** converts each CHI flit into a transaction and pushes it to
  the driver's `item_fifo` through `item_port`.  A `RawReq` becomes
  `ChiReadNoSnpTxn` / `ChiWriteNoSnpTxn`; a `RawDat` becomes a
  `ChiWriteDataTxn` and is also attached to the live write transaction.  The
  transaction id is preserved, and unsupported opcodes are dropped with a
  warning.  `push_flit()` / `flit_export` is the injection point for a future
  gem5 co-simulation shim.
* **driver** (`ChiLinkBfm` + `ChiLinkDriver`) is the only component that drives
  the DUT's CHI link.  The BFM performs LINKACTIVE negotiation, RX credit
  accounting (4-deep RX adapters), TX credit granting (15 credits) and flit
  pacing; the UVM layer just turns transactions into flits.
* **chi_mon** samples RXREQ/RXDAT/TXRSP/TXDAT and publishes decoded
  `RawReq`/`RawDat`/`RawRsp` flits on four analysis ports.
* **axi_mon** samples AW/W/B/AR/R and publishes `AxiAwTxn`/`AxiWTxn`/`AxiBTxn`/
  `AxiArTxn`/`AxiRTxn`.
* **axi_slave_stub** is *not* part of the requested component list; it is a
  placeholder that answers the DUT's AXI port so the skeleton can run.  Set
  `C2xmEnv.enable_axi_slave_stub = False` and delete the file once a real AXI
  agent/slave model exists.

There is no reference model and no scoreboard, on purpose.  Both monitors
publish analysis ports, so a checker can be connected later without touching
the stimulus path.

## Setup & configuration

### Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| VCS | Q-2020.03-SP2 | compiles the RTL + TB (`simv`) |
| Python | 3.12 | shared by cocotb, gem5 and the helpers |
| cocotb | 2.1 | `pip install cocotb` |
| pyuvm | 5.0 | `pip install pyuvm` |
| scons, g++ | any recent | for the gem5 build (RISCV variant) |
| Verdi (optional) | R-2020.12-SP1 | only for FSDB waveform viewing |

### One-clone layout

This repo carries the gem5 tree as a submodule (branch `c2xm_cosim`,
which adds `ChiCosimBridge` — see its commit message):

```bash
git clone --recurse-submodules https://github.com/makenma/c2xm_cosim.git
cd c2xm_cosim
```

### Build gem5 (submodule, no prebuilt binary)

```bash
cd XS-DSU-GEM5
scons build/RISCV/gem5.opt -j 32      # ~30-60 min the first time
cd ..
```

The build must contain the `ChiCosimBridge` SimObject (it does on the
`c2xm_cosim` branch; check `build/RISCV/mem/cache/CHI/ChiCosimBridge.hh`
exists after the build).

### Point the TB at the RTL

The C2XM RTL is generated code and lives outside git.  The TB Makefile
resolves it through `C2XM_ROOT`:

```bash
export C2XM_ROOT=/path/to/c2xm_generated_dsl_core_20260916
```

Default when unset: `../c2xm_generated_dsl_core_20260916` relative to
this directory (i.e. the `c2xm_exp/` source-tree layout), so exporting is
only needed for a different placement.

### Workload

`run_cosim.sh` runs a XiangShan GCPT checkpoint by default
(`/nfs/home/majunhong/workloads/ready-to-run/coremark-2-iteration.bin`);
override with `C2XM_GCPT=/path/to/<workload>.bin`.

### Configuration knobs

`run_cosim.sh` wraps every tunable; the raw variables are listed here
(launcher name -> what it sets):

| Variable | Default | Meaning |
|---|---|---|
| `C2XM_SOCK` | `/tmp/c2xm_cosim.sock` | UNIX socket between gem5 and the TB |
| `C2XM_QUANTUM` | `100` | barrier period, in cycles (also sets gem5's `CHI_COSIM_QUANTUM`) |
| `C2XM_NUM_CPUS` | `1` | gem5 CPU count — **keep 1**: multicore needs difftest/golden-mem (see Known issues) |
| `C2XM_CLK_NS` | `0.334` | TB clock period in ns; keep 1:1 with gem5's system clock (3 GHz here). Must be representable in 1 ps and divisible by 2 |
| `C2XM_GCPT` | NFS coremark path | GCPT checkpoint to restore |
| `C2XM_WAVES` | `none` | `fsdb` (Verdi PLI, separate `sim_build_fsdb`) or `vcd` waveform dump |
| `C2XM_MAX_SYNCS` | `0` | stop after N barriers (0 = run to workload exit) — use this instead of gem5 `--max-insts`, which kills this GCPT workload |
| `C2XM_GEM5_ARGS` | – | extra gem5 CLI args |

Low-level variables (usually only set when driving the two sides
manually instead of via `run_cosim.sh`):

| Variable | Side | Meaning |
|---|---|---|
| `CHI_COSIM_SOCKET` | gem5 | socket path the bridge listens on |
| `CHI_COSIM_QUANTUM` | gem5 | barrier period (cycles) |
| `CHI_COSIM_BYPASS_ROUTE` | gem5 | `all` \| `dram_only` \| `none` — how RNF bypass traffic crosses the co-sim |
| `C2XM_COSIM_SOCK` | TB | socket path to connect to (test skips if unset) |
| `C2XM_COSIM_CLK_NS` | TB | TB clock period |
| `C2XM_COSIM_MAX_SYNCS` | TB | barrier budget |
| `C2XM_COSIM_FREE_RUN` | TB | `1` = keep the clock free-running (debug; breaks strict 1:1 lockstep) |
| `C2XM_COSIM_GEM5_CLK_NS` | TB | informational only (hello handshake) |

### Run

```bash
./run_cosim.sh                                   # whole coremark run (~5 min)
C2XM_WAVES=fsdb C2XM_MAX_SYNCS=800 ./run_cosim.sh   # short window + waveform
```

`run_cosim.sh` picks the gem5 tree from `$GEM5_ROOT`, else the
`XS-DSU-GEM5/` submodule, else the NFS source path; logs and any
waveform land in a fresh `/tmp/c2xm_cosim_run.*` directory.

### XLS Proc-IR co-simulation

The same gem5 bridge can run directly against optimized C2XM XLS Proc IR,
without VCS or generated RTL. The XLS peer executes the real Proc network,
proxies its AXI traffic into gem5's memory, and supports both full CoreMark and
write-path stress runs. See [XLS_COSIM.md](XLS_COSIM.md) for build, launch and
regression instructions.

## gem5 co-simulation

The end goal of this environment: replace the gem5 SN-F
(`Chi2ClassicMemBridge` at router (1,0) of the kmhv2 2x2 mesh) with the
C2XM RTL, and run gem5 and this testbench as **one synchronous
simulation** — gem5's CHI flits become RTL pin timing, the RTL's AXI side
is served from gem5's own DDR, and responses flow back as CHI flits.

```
 gem5 (gem5.opt)                                VCS + this testbench
 ───────────────                                ─────────────────────
 RNF ─ mesh ─ HNF ─ router(1,0)                 snf_agent ─ driver ─ DUT
                    │ ChiCosimBridge            (ABI adapter)     │ AXI
                    │  · flits g2t/t2g          ▲                 ▼
                    │  · quantum barrier        └── chi_mon   axi_ddr_proxy
                    ▼                              (TX flits)  (AR/AW/W →
             UNIX socket, newline JSON                          mem_read/write)
                    ▲                                            │
                    └──────── mem_data / mem_resp ◄──────────────┘
              (bridge issues classic packets on system.membus → DDR;
               RNF bypass traffic optionally crosses too: bypass_route
               = all | dram_only | none)
```

* **Protocol** (`cosim_protocol.py`): `hello/hello_ack`, `flit{req|dat|rsp}`,
  `mem_read/mem_data`, `mem_write/mem_resp`, `bypass_read/bypass_write/
  bypass_resp`, `sync/ack`, `bye`.  One JSON object per line.
* **Runtime sync**: the bridge sends `sync` every `quantum` gem5 cycles and
  blocks; the TB (`cosim_runtime.barrier_loop`) receives it, advances
  exactly `quantum` DUT cycles on a gated clock, answers `ack`.  Both
  sides therefore advance cycle-for-cycle (the TB clock is parked while
  gem5 works and vice versa).  TB clock period: `C2XM_COSIM_CLK_NS`
  (default 0.556ns ≈ gem5's 1.8 GHz system clock).
* **snf_agent** owns every field the two worlds disagree on (see the file
  docstring for the full table): WriteNoSnpFull `0x5c→0x1C`, byte-count
  size → log2, uint32 txnid → DUT pool slot (with an occupancy model:
  only writes and address-dependent reads take pool slots, independent
  reads bypass into the read-response queue), `AllowRetry=1` + local
  RetryAck/PCrdGrant absorption, one 64B write flit → two data_id 0/2
  beats; on the way back srcid→SNF id, txnid→gem5's, `dbid=slot+1`
  (DUT never checks the echoed dbid, gem5 needs non-zero + Comp echo),
  dataid 0/2→0/1 with beatOffset/last/byteEnable geometry, data trimmed
  to the request size.
* **axi_ddr_proxy** turns DUT AXI bursts into `mem_read/mem_write`
  round trips, so gem5's DDR is the single memory of the whole co-sim
  (no image preloading, no dual-image consistency problem).  W sampling
  is a separate coroutine from the DDR round trip so back-to-back beats
  are never lost.

Bring-up ladder (all green):

1. `make cosim-fake SCENARIO=all|mixed|bypass|write` — fake_gem5 checks
   the whole loop without gem5.
2. `tb_peer_sim.py` — software SN-F against the *real* gem5 bridge
   (coremark completes, m5_exit).
3. Full co-simulation via `../run_cosim.sh`:

   ```bash
   cd c2xm_exp && ./run_cosim.sh          # coremark GCPT, 1 CPU, quantum 100

   # with waveforms (FSDB for Verdi, lands in the run dir as waves.fsdb):
   C2XM_WAVES=fsdb C2XM_MAX_SYNCS=800 ./run_cosim.sh   # stop after 800 barriers
   #   C2XM_WAVES=vcd  -> plain waves.vcd (GTKWave-friendly)
   #   C2XM_MAX_SYNCS=0 -> run the whole workload (~10k barriers)
   # view: verdi -f ../c2xm_generated_dsl_core_20260916/filelist_rtl.f \
   #              -ssf waves.fsdb
   # (dump is plusarg-gated, depth 1 = TB-level CHI/AXI channels; the FSDB
   #  build lives in sim_build_fsdb because of the Verdi PLI link)
   ```

   Result (single core, kmhv2 2x2 mesh, C2XM at the SNF position):

   ```
   gem5:  Iterations: 2, crcfinal 0x72be, Exiting @ tick 333137862 (m5_exit)
   tb:    TESTS=1 PASS=1 FAIL=0
          chi={'rxreq': 228, 'txdat': 456}  axi={'ar': 228, 'r': 456}
          bypass=827  barriers=10004  dut_cycles=1000400  (~5 min wall)
   ```

   i.e. 228 HNF line fills went gem5 -> CHI -> socket -> SNF agent ->
   RXREQ -> **C2XM RTL** -> AXI AR -> back through the socket as
   mem_read -> **gem5's own DDR** -> R beats -> TXDAT CompData -> HNF ->
   CPU, the RNF bypass traffic crossed the socket as well, and both
   simulators advanced exactly 1:1 (100 barriers per 10k DUT cycles).
   The CoreMark CRC proves the data path byte-exact.

Caveats found on the way (worth knowing):

* Single core only for now: the tree's LSQ updates the difftest golden
  memory on every store completion, which is only allocated for
  `numCPUs > 1 && enableDifftest` -- multicore without a NEMU proxy
  segfaults before any co-sim traffic (pre-existing, unrelated to the
  bridge; `run_cosim.sh` defaults to 1 CPU).
* gem5's option parser rejects unknown --flags, so the cosim config
  reads `CHI_COSIM_SOCKET` / `CHI_COSIM_QUANTUM` / `CHI_COSIM_BYPASS_ROUTE`
  from the environment instead.
* cocotb imports test modules through pytest's rewriter: if you edit TB
  python files and behavior seems stale, `rm -rf __pycache__`.
* The TB clock must be representable in 1ps and divisible by two:
  0.334ns is used (gem5 sys clock 3GHz).

## Clocking convention

Everything in the testbench acts on the **falling** clock edge (mid cycle) and
samples through `ReadOnly`:

* the TB drives DUT inputs mid cycle, so the DUT gets a full setup cycle and
  latches them at the next rising edge;
* sampling mid cycle reads values that are stable after the previous rising
  edge, and `ReadOnly` guarantees the read happens after every other Python
  task's write in that time step.

One flit is therefore held for exactly one rising edge: `_send()` aligns to a
falling edge, asserts valid, waits for the next falling edge, and deasserts.
Do not drive DUT inputs without that alignment — a flit asserted in the same
time step as a rising edge can be missed by the DUT while still being visible
to a monitor.

## CHI subset implemented by the DUT

| Opcode | Meaning            | DUT behaviour                                            |
| ------ | ------------------ | -------------------------------------------------------- |
| `0x04` | ReadNoSnp          | AR on AXI, `CompData` (0x4) beats on TXDAT               |
| `0x1C` | WriteNoSnpFull     | `DBIDResp` (0x6), 2 RXDAT beats, AW/W/B, `Comp` (0x4)    |
| `0x1D` | WriteNoSnpPtl      | as above, one data beat                                   |
| any    | other              | rejected by the RTL (`is_read`/`is_write` are false)      |

`memattr` bit 0 (EWA) selects the write's initial response:
`CompDBIDResp` (0x5, completion implied) when set, `DBIDResp` (0x6) otherwise.
`qos == 4'hf` selects the RTL "hh" path, any other value the "m" path; tests
use `qos = 0`.

A 64-byte transfer is two 32-byte DAT beats: `data_id` 0 covers bits [255:0]
(``received_data_mask`` 0b0011) and `data_id` 2 covers bits [511:256] (0b1100),
which together satisfy the expected mask 0b1111.  For a shorter transfer the
single beat picks the half containing `addr[5]`.  See
`chi_txn.required_data_ids()`.

## Wire layouts

`chi_flit.py` holds the bit-exact link layouts, each annotated with the RTL
line it came from:

| Flit  | Width | Source                                                        |
| ----- | ----- | ------------------------------------------------------------- |
| RXREQ | 172   | `c2xm_core.sv` `lc_rxreqflt[...]` slices                      |
| RXDAT | 426   | `c2xm_core.sv` `lc2wb_rxdatflt[...]` slices                   |
| TXRSP | 75    | `external/c2xm_dsl_chi_txrsp_link.sv` + `core_txrsp_payload`  |
| TXDAT | 426   | `c2xm_core.sv` `rb2lc_txdatflt[...]` slices                   |

## Known issues found while building this

1. **CHI `txn_id` must equal the transaction-pool slot** (suspected RTL bug).
   `c2xm_core_receive_rxdat_operation.sv` builds its pool and log-buffer lookup
   key as `5'(rxdat.txn_id)`, while
   `c2xm_core_admit_and_allocate_pool_entry_operation.sv` allocates the
   *lowest free* slot (`onehot_first(free_candidates)`).  For a fresh pool the
   first request lands in slot 0, so a WriteNoSnp with `txn_id=2` writes its
   data into slot 2, which is still free: `received_data_mask` of the real
   entry stays 0, no AW is ever issued and the write hangs (observed: DBIDResp
   returned, RXDAT accepted on the link, `POOL[0] rmask=0000`, AXI silent for
   ever).  With `txn_id=0` the same test completes.  Either the allocator
   should index by `txn_id[4:0]`, or the RX DAT path should search the pool for
   the matching `txn_id`.  **Please confirm with the RTL owner.**
2. `memattr` bit 1..3 are not consumed by the DUT; only EWA changes behaviour.
3. Verdi/`Xvfb`-free hosts cannot run the interactive GUI; `make GUI=1` builds
   the KDB so the waveform/GUI can be opened elsewhere.

## Next steps

* AXI slave + memory model to replace `axi_slave_stub.py`.
* Scoreboard/subscriber on the analysis ports (read data compare, write
  completion check).
* Sequences on the driver's `seq_item_port` (currently only the adaptor feeds
  the driver), plus CHI retry (`RetryAck` / `PCrdGrant`) handling in
  `ChiLinkBfm.observe_tx`.
* Coverage: opcode/size/address/qos crosses, link credit extremes, back-to-back
  and concurrent transactions (mind issue 1 above).
