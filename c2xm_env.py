"""C2XM pyuvm environment.

    C2xmEnv (cosim_mode=False -- standalone tests, hand-made flits)
      +-- gem5_adaptor  Gem5Adaptor   gem5 CHI flit -> ChiTxn
      +-- driver        ChiLinkDriver ChiTxn -> RXREQ/RXDAT link flits
      +-- chi_mon       ChiMonitor    CHI link flits -> analysis ports
      +-- axi_mon       AxiMonitor    AXI beats      -> analysis ports
      +-- axi_slave     AxiSlaveStub  placeholder B/R responder (delete me)

    C2xmEnv (cosim_mode=True -- co-simulation with gem5/fake_gem5)
      +-- snf_agent     SnfAgent       gem5 SNF ABI <-> DUT wire ABI
      +-- driver        ChiLinkDriver  (unchanged)
      +-- chi_mon / axi_mon            (unchanged)
      +-- axi_slave     AxiDdrProxy    AXI bursts proxied to gem5 DDR
      +-- responder     CosimResponder chi_mon TX ports -> snf_agent

Connections:

    gem5_adaptor.item_port / snf_agent.item_port --> driver.item_export
    chi_mon.txrsp_ap, txdat_ap --> responder --> snf_agent.on_txrsp/on_txdat

Set ``env.cosim_mode = True`` on the freshly constructed env (before
build_phase runs).  The runtime (cosim_runtime.CosimRuntime) is created by
the co-sim test and reached through ``env.runtime``.
"""

from __future__ import annotations

import pyuvm

from axi_ddr_proxy import AxiDdrProxy
from axi_mon import AxiMonitor
from axi_slave_stub import AxiSlaveStub
from chi_mon import ChiMonitor
from cosim_responder import CosimResponder
from driver import ChiLinkDriver
from gem5_adaptor import Gem5Adaptor
from snf_agent import SnfAgent


class C2xmEnv(pyuvm.uvm_env):
    """Top-level verification environment for C2XM."""

    def __init__(self, name, parent):
        super().__init__(name, parent)
        #: Set False to leave the AXI B/R inputs undriven for a real AXI agent.
        self.enable_axi_slave_stub = True
        #: Switch to the co-simulation component set (see module docstring).
        self.cosim_mode = False
        #: Bound by the co-sim test (CosimRuntime instance).
        self.runtime = None

    def build_phase(self):
        if self.cosim_mode:
            self.snf_agent = SnfAgent("snf_agent", self)
            self.axi_slave = AxiDdrProxy("axi_slave", self)
            self.responder = CosimResponder("responder", self)
        else:
            self.gem5_adaptor = Gem5Adaptor("gem5_adaptor", self)
            if self.enable_axi_slave_stub:
                self.axi_slave = AxiSlaveStub("axi_slave", self)
        self.driver = ChiLinkDriver("driver", self)
        self.chi_mon = ChiMonitor("chi_mon", self)
        self.axi_mon = AxiMonitor("axi_mon", self)

    def connect_phase(self):
        if self.cosim_mode:
            # gem5 flits -> SNF ABI adaptation -> link driver.
            self.snf_agent.item_port.connect(self.driver.item_export)
            # DUT TX flits -> gem5-bound responses (bound here because
            # child connect_phases run before the env's own).
            self.responder.bind_and_connect(self.chi_mon, self.snf_agent)
        else:
            # gem5 flits -> transactions -> link driver.
            self.gem5_adaptor.item_port.connect(self.driver.item_export)

    # ------------------------------------------------------------------
    # convenience API for tests
    # ------------------------------------------------------------------
    async def send_flit(self, flit) -> None:
        """Push a gem5 CHI flit into the adaptor (test-facing helper)."""
        await self.gem5_adaptor.push_flit(flit)

    @property
    def chi_counts(self) -> dict:
        return self.chi_mon.counts

    @property
    def axi_counts(self) -> dict:
        return self.axi_mon.counts
