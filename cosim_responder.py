"""Bridge from the CHI monitor's TX analysis ports to the SNF agent.

The DUT's TXRSP/TXDAT flits are the co-simulation's return path to gem5.
``ChiMonitor`` already decodes them into ``chi_flit.RawRsp`` / ``RawDat``
with the *wire* field values (txnid == pool slot, srcid == nodeid, ...);
the SNF agent owns the gem5-side field fixups, so this component only
forwards.  The env's connect_phase binds ``snf_agent`` and makes the two
``connect`` calls (child connect_phases run before the env can set the
attributes).
"""

from __future__ import annotations

import pyuvm

from chi_flit import RawDat, RawRsp


class _TxSubscriber(pyuvm.uvm_subscriber):
    """Analysis sink forwarding writes to a bound callable."""

    def __init__(self, name, parent, what: str):
        super().__init__(name, parent)
        self.sink = None
        self.what = what
        self.count = 0

    def write(self, t):
        self.count += 1
        if self.sink is not None:
            self.sink(t)

    def final_phase(self):
        self.logger.info(f"responder {self.what}: forwarded {self.count}")


class CosimResponder(pyuvm.uvm_component):
    """Publishes DUT TX flits toward gem5 via the SNF agent."""

    def __init__(self, name, parent):
        super().__init__(name, parent)
        self.snf_agent = None
        self.chi_mon = None
        self.txrsp_sub = None
        self.txdat_sub = None

    def build_phase(self):
        self.txrsp_sub = _TxSubscriber("txrsp_sub", self, "txrsp")
        self.txdat_sub = _TxSubscriber("txdat_sub", self, "txdat")

    def connect_phase(self):
        # chi_mon/snf_agent are bound by the env's connect_phase, which runs
        # after this one; only remember the subscribers there.
        pass

    def bind_and_connect(self, chi_mon, snf_agent):
        """Called from the env's connect_phase with both handles."""
        self.chi_mon = chi_mon
        self.snf_agent = snf_agent
        self.txrsp_sub.sink = snf_agent.on_txrsp
        self.txdat_sub.sink = snf_agent.on_txdat
        chi_mon.txrsp_ap.connect(self.txrsp_sub.analysis_export)
        chi_mon.txdat_ap.connect(self.txdat_sub.analysis_export)
