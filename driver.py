"""CHI link driver for the C2XM environment.

Two layers:

``ChiLinkBfm``
    Pin-level model of the *peer* (gem5's CHI requester) sitting on the far
    side of the C2XM CHI link.  It owns everything the DUT needs on its link
    inputs:

      * reset sequencing,
      * LINKACTIVE negotiation.  ``dsu_chib_dev_linkhandshake`` inside the DUT
        powers up in TXSTOP_RXSTOP with both run gates low and no pending
        traffic, so it will never start on its own: the peer must raise
        ``rx_linkactivereq`` (asking for the DUT RX path) and then acknowledge
        the DUT's ``tx_linkactivereq``,
      * link-layer credit accounting.  RX channels: the DUT returns one credit
        per flit its core consumes (``rxreqcrdv``/``rxdatcrdv``) and the RX
        adapter FIFO is only 4 deep, so at most 4 flits may be outstanding.
        TX channels: the peer grants credits with ``txrspcrdv``/``txdatcrdv``;
        the DUT counter saturates at MAX_CREDITS=15, so a constant grant is
        used,
      * flit transmission with credit pacing.

``ChiLinkDriver``
    The pyuvm driver: it pulls transactions from the gem5 adaptor and hands
    them to the BFM.  Request transactions become RXREQ flits, write-data
    transactions become RXDAT flits.

Clocking convention (shared by every TB component)
--------------------------------------------------
All testbench activity happens on the **falling** clock edge - mid cycle,
between the DUT's active rising edges:

  * the TB drives DUT inputs mid cycle, so the DUT samples them at the next
    rising edge with a full cycle of setup time,
  * the TB samples DUT outputs mid cycle, where every registered output has
    settled after the previous rising edge,

which makes handshake sampling race-free without relying on ReadOnly ordering.

Skeleton status
---------------
* RetryAck / PCrdGrant handling (the CHI retry protocol) is *not* implemented:
  those opcodes are recorded by :meth:`ChiLinkBfm.observe_tx` but no request is
  replayed and no request slot is released.
* Outstanding-transaction policy beyond the link credit limit is not enforced.
* The AXI slave side (B/R responses) lives in ``axi_slave_stub.py``.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import cocotb
from cocotb.triggers import FallingEdge
import pyuvm

from chi_flit import pack_rxdat, pack_rxreq, unpack_txrsp
from chi_txn import ChiReadNoSnpTxn, ChiTxnItem, ChiWriteDataTxn, ChiWriteNoSnpTxn
from tb_config import CLK_PERIOD_NS, LINK_UP_TIMEOUT_NS, DBID_TIMEOUT_NS
from tb_signals import read_bit as _bit

#: RX adapter depth (c2xm_top.sv: DEPTH=4 on both RX channels).
RXREQ_CREDITS = 4
RXDAT_CREDITS = 4
#: TXRSP/TXDAT link MAX_CREDITS (c2xm_top.sv).
TX_CREDITS = 15

#: Opcodes the DUT uses for the initial write response.
OPC_COMPDBIDRESP = 0x5
OPC_DBIDRESP = 0x6


class ChiLinkBfm:
    """Pin-level gem5-side CHI link peer."""

    def __init__(self, top, clk=None, logger=None):
        self.top = top
        self.clk = clk if clk is not None else top.clk
        self.log = logger

        self.req_credits = 0
        self.dat_credits = 0
        self.txrsp_sent = 0
        self.txdat_sent = 0
        #: dbid from DBIDResp/CompDBIDResp, keyed by CHI txn id.
        self.dbid_by_txn: Dict[int, int] = {}
        #: TX flits observed, for debug and future checkers.
        self.txrsp_history: List[dict] = []
        self.txdat_history: List[dict] = []
        self.link_up = False
        self._reset_stable = 0

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------
    def drive_link_idle(self) -> None:
        self.top.rxreqfltv.value = 0
        self.top.rxreqflt.value = 0
        self.top.rxdatfltv.value = 0
        self.top.rxdatflt.value = 0
        self.top.txrspcrdv.value = 0
        self.top.txdatcrdv.value = 0
        self.top.disconnect.value = 0
        self.top.rx_linkactivereq.value = 0
        self.top.tx_linkactiveack.value = 0

    async def wait_reset_released(self, stable_edges: int = 4) -> None:
        """Block until ``rstn`` has been high for ``stable_edges`` cycles."""
        self.drive_link_idle()
        self._reset_stable = 0
        while self._reset_stable < stable_edges:
            await FallingEdge(self.clk)
            if _bit(self.top.rstn) == 1:
                self._reset_stable += 1
            else:
                self._reset_stable = 0

    # ------------------------------------------------------------------
    # link activation
    # ------------------------------------------------------------------
    async def link_activation_task(self) -> None:
        """Peer half of the CHI LINKACTIVE handshake.

        Raise ``rx_linkactivereq`` so the DUT's RX side activates (this also
        pulls up the DUT's own ``tx_req`` inside the handshake FSM), then
        acknowledge ``tx_linkactivereq`` and hold both high to stay in
        TXRUN_RXRUN.
        """
        await self.wait_reset_released()
        self.top.rx_linkactivereq.value = 1
        self.top.tx_linkactiveack.value = 0
        acked = False
        while True:
            await FallingEdge(self.clk)
            if not acked and _bit(self.top.tx_linkactivereq) == 1:
                self.top.tx_linkactiveack.value = 1
                acked = True
                if self.log:
                    self.log.info("link: asserted tx_linkactiveack")

    async def wait_link_up(self, timeout_ns: float = LINK_UP_TIMEOUT_NS) -> None:
        """Wait until both DUT link-layer run gates are asserted."""
        waited = 0.0
        while waited < timeout_ns:
            await FallingEdge(self.clk)
            if (
                _bit(self.top.dbg_chi_rx_run) == 1
                and _bit(self.top.dbg_chi_tx_run) == 1
            ):
                self.link_up = True
                if self.log:
                    self.log.info(f"link: up after ~{waited:.0f}ns")
                return
            waited += CLK_PERIOD_NS
        raise TimeoutError(f"CHI link did not come up within {timeout_ns}ns")

    # ------------------------------------------------------------------
    # credits
    # ------------------------------------------------------------------
    async def rx_credit_task(self) -> None:
        """Count the credits the DUT returns for flits we sent."""
        while True:
            await FallingEdge(self.clk)
            if _bit(self.top.rxreqcrdv) == 1:
                self.req_credits += 1
            if _bit(self.top.rxdatcrdv) == 1:
                self.dat_credits += 1

    async def tx_credit_task(self) -> None:
        """Grant DUT TX credits once its transmit path is running.

        The DUT credit counters saturate at ``TX_CREDITS``, so a permanent
        grant is equivalent to a peer with an always-drained receive buffer.
        Replace with a finite buffer model to exercise back-pressure.
        """
        await self.wait_link_up()
        self.top.txrspcrdv.value = 1
        self.top.txdatcrdv.value = 1

    async def _wait_credit(self, channel: str, timeout_ns: float = 5000.0) -> None:
        waited = 0.0
        while True:
            credits = self.req_credits if channel == "req" else self.dat_credits
            if credits > 0:
                if channel == "req":
                    self.req_credits -= 1
                else:
                    self.dat_credits -= 1
                return
            if waited >= timeout_ns:
                raise TimeoutError(
                    f"no RX {channel} credit returned within {timeout_ns}ns"
                )
            await FallingEdge(self.clk)
            waited += CLK_PERIOD_NS

    # ------------------------------------------------------------------
    # flit transmission
    # ------------------------------------------------------------------
    async def _send(self, valid_sig, payload_sig, payload: int) -> None:
        """Hold one flit on the link for exactly one DUT sampling edge."""
        payload_sig.value = payload
        valid_sig.value = 1
        await FallingEdge(self.clk)
        valid_sig.value = 0
        payload_sig.value = 0

    async def send_req_flit(self, payload: int) -> None:
        """Drive one RXREQ flit into the DUT (172 bits)."""
        await self._wait_credit("req")
        await self._send(self.top.rxreqfltv, self.top.rxreqflt, payload)

    async def send_dat_flit(self, payload: int) -> None:
        """Drive one RXDAT flit into the DUT (426 bits)."""
        await self._wait_credit("dat")
        await self._send(self.top.rxdatfltv, self.top.rxdatflt, payload)

    # ------------------------------------------------------------------
    # TX observation
    # ------------------------------------------------------------------
    async def observe_tx(self) -> None:
        """Record TXRSP/TXDAT flits and the DBIDs they carry.

        Hook for the retry protocol: on RetryAck the peer should re-issue the
        request, on PCrdGrant it should release a request slot.  Recorded only.
        """
        while True:
            await FallingEdge(self.clk)
            if _bit(self.top.txrspfltv) == 1:
                flit = unpack_txrsp(int(self.top.txrspflt.value))
                self.txrsp_sent += 1
                self.txrsp_history.append(
                    {
                        "opcode": flit.opcode,
                        "dbid": flit.dbid,
                        "txnid": flit.txnid,
                        "resp": flit.resp,
                    }
                )
                if flit.opcode in (OPC_COMPDBIDRESP, OPC_DBIDRESP):
                    self.dbid_by_txn[flit.txnid] = flit.dbid
            if _bit(self.top.txdatfltv) == 1:
                self.txdat_sent += 1
                self.txdat_history.append({"payload": int(self.top.txdatflt.value)})

    async def wait_dbid(
        self, txn_id: int, timeout_ns: float = DBID_TIMEOUT_NS
    ) -> Optional[int]:
        """Wait for the DBID the DUT assigns to a write transaction."""
        waited = 0.0
        while waited < timeout_ns:
            if txn_id in self.dbid_by_txn:
                return self.dbid_by_txn[txn_id]
            await FallingEdge(self.clk)
            waited += CLK_PERIOD_NS
        return None


class ChiLinkDriver(pyuvm.uvm_driver):
    """Drives C2XM's CHI request/data channels from adaptor transactions."""

    def __init__(self, name, parent):
        super().__init__(name, parent)
        self.bfm: Optional[ChiLinkBfm] = None
        #: Wait for DBIDResp before pushing a write's data beats (real gem5
        #: ordering).  Set False to send request and data back to back.
        self.wait_for_dbid = True

    def build_phase(self):
        #: Transactions handed over by the gem5 adaptor.
        self.item_fifo = pyuvm.uvm_tlm_fifo("item_fifo", self, size=64)
        self.item_export = self.item_fifo.put_export
        #: Emitted for every transaction driven onto the link.
        self.ap = pyuvm.uvm_analysis_port("ap", self)

    async def run_phase(self):
        top = cocotb.top
        self.bfm = ChiLinkBfm(top, top.clk, logger=self.logger)

        await self.bfm.wait_reset_released()
        cocotb.start_soon(self.bfm.link_activation_task())
        cocotb.start_soon(self.bfm.rx_credit_task())
        cocotb.start_soon(self.bfm.observe_tx())
        cocotb.start_soon(self.bfm.tx_credit_task())
        await self.bfm.wait_link_up()

        while True:
            item = await self.item_fifo.get()
            txn = item.txn if isinstance(item, ChiTxnItem) else item
            await self.drive(txn)

    async def drive(self, txn) -> None:
        """Turn one transaction into link traffic."""
        if isinstance(txn, (ChiReadNoSnpTxn, ChiWriteNoSnpTxn)):
            await self.bfm.send_req_flit(pack_rxreq(txn.raw_flit))
            self.logger.info(f"drove RXREQ   {txn.describe()}")
            self.ap.write(txn)
            return

        if isinstance(txn, ChiWriteDataTxn):
            if self.wait_for_dbid:
                dbid = await self.bfm.wait_dbid(txn.txn_id)
                if dbid is None:
                    self.logger.warning(
                        f"no DBIDResp for txn {txn.txn_id} within"
                        f" {DBID_TIMEOUT_NS}ns, sending write data anyway"
                    )
                else:
                    for flit in txn.data:
                        flit.dbid = dbid
            for flit in txn.data:
                await self.bfm.send_dat_flit(pack_rxdat(flit))
            self.logger.info(f"drove RXDAT   {txn.describe()}")
            self.ap.write(txn)
            return

        self.logger.warning(f"driver: nothing to drive for {txn!r}")

    def final_phase(self):
        if self.bfm is None:
            return
        self.logger.info(
            f"link counters: rxreq_credit_left={self.bfm.req_credits}"
            f" rxdat_credit_left={self.bfm.dat_credits}"
            f" txrsp_sent={self.bfm.txrsp_sent}"
            f" txdat_sent={self.bfm.txdat_sent}"
            f" dbids={self.bfm.dbid_by_txn}"
        )
