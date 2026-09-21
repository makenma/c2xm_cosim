"""Testbench configuration shared by every C2XM pyuvm component.

Values here are the ones a test needs to know about the environment rather
than about the RTL: clocking, node ids used on the CHI link, and the timeouts
that keep a skeleton run from hanging forever.
"""

from __future__ import annotations

#: Clock period driven by the test (cocotb's ``Clock`` takes a full period).
CLK_PERIOD_NS = 4.0
#: Reset assertion time.
RESET_HOLD_NS = 100.0

#: CHI node id C2XM itself presents.  Must match the ``nodeid`` input of
#: c2xm_top (tied to 0 in tb/c2xm_tb_top.sv).
C2XM_NODEID = 0
#: CHI node id the test uses as the gem5 requester (RN).
GEM5_SRCID = 1
#: HNF id list default from c2xm_core_common_defines.sv entry 0 (352'd2050).
C2XM_HNF_NODEID0 = 2050

#: CHI QoS bit field.  In the RTL, ``qos == 4'hf`` selects the "hh" handling
#: path and any other value the "m" path, so tests default to 0.
DEFAULT_QOS = 0
QOS_HH = 15

#: Timeouts (ns) that turn a deadlocked skeleton run into a clear failure.
LINK_UP_TIMEOUT_NS = 4000.0
DBID_TIMEOUT_NS = 1000.0
#: Time the test waits after its last stimulus before ending the phase.
DRAIN_NS = 2000.0

# ---------------------------------------------------------------------------
# Co-simulation (C2xmCosimTest).  Most values are overridable through the
# C2XM_COSIM_* environment variables read by cosim_runtime; the node ids
# live here because they are topology constants of the kmhv2 2x2 mesh
# (configs/common/CacheConfig.py: _chi_node_id(1,0)=SNF, (1,1)=HNF).
# ---------------------------------------------------------------------------
#: SN-F node id the TB presents to gem5 (chi_node_id(1,0) = 0x80).
COSIM_SNF_ID = 0x80
#: HN-F node id gem5 routes SNF traffic through (chi_node_id(1,1) = 0x90).
COSIM_HNF_ID = 0x90
#: gem5 system clock the CHI components run at (1.8 GHz in kmhv2).
COSIM_GEM5_CLK_NS = 0.556
#: DUT clock in co-sim mode: matches gem5 1:1 by default.
COSIM_CLK_NS = 0.556
#: Barrier quantum in cycles.
COSIM_QUANTUM = 100
