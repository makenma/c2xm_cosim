// -----------------------------------------------------------------------------
// c2xm_tb_top.sv -- cocotb/pyuvm testbench wrapper for c2xm_top
//
// Why a wrapper?
//   * c2xm_top has ~20 static configuration inputs (nodeid, starvation
//     thresholds, address cut tables).  They are compile/elaboration-time
//     constants for a testbench run, so they are tied off here instead of
//     being driven from Python on every reset.
//   * The only signals exposed to Python are the ones a test actually
//     cares about: clock/reset, the four CHI link channels, the link
//     handshake, and the AXI master port.
//
// Port direction convention below is from the TESTBENCH point of view:
//     input  -> driven by Python (a DUT input,  or a DUT output we snoop)
//     output -> sampled  by Python (a DUT output, or a DUT input we drive)
// i.e. every port is a DUT input or output seen "from outside"; Python may
// read or drive any of them, but the naming keeps the wiring obvious.
//
// Nothing in this file is generated; c2xm_top.sv stays untouched.
// -----------------------------------------------------------------------------

`include "c2xm_core_common_defines.sv"

module c2xm_tb_top #(
    // HNF physical-node-id table consumed by the TXRSP link leaf.
    parameter logic [(11 * `C2XM_CORE_HNF_NUM) - 1:0] HNF_NODE_IDS = `C2XM_CORE_HNF_NODE_IDS
) (
    input logic clk,
    input logic rstn,

    // ---------------- CHI link: RX request channel (TB -> DUT) ---------------
    input  logic                                                        rxreqfltv,
    input  logic [c2xm_core_types_pkg::IF_CHI_RXREQ_PAYLOAD_WIDTH-1:0]   rxreqflt,
    output logic                                                        rxreqcrdv,

    // ---------------- CHI link: RX data channel (TB -> DUT) ------------------
    input  logic                                                        rxdatfltv,
    input  logic [c2xm_core_types_pkg::IF_CHI_RXDAT_PAYLOAD_WIDTH-1:0]   rxdatflt,
    output logic                                                        rxdatcrdv,

    // ---------------- CHI link: TX response channel (DUT -> TB) --------------
    // The TXRSP link leaf always drives a 75-bit physical flit
    // (4 qos + 11 tgtid + 60 payload), independent of HNF_NUM_LOG2.
    output logic              txrspfltv,
    output logic [74:0]       txrspflt,
    input  logic              txrspcrdv,

    // ---------------- CHI link: TX data channel (DUT -> TB) ------------------
    output logic              txdatfltv,
    output logic [425:0]      txdatflt,
    input  logic              txdatcrdv,

    // ---------------- CHI link: LINKACTIVE handshake -------------------------
    input  logic disconnect,
    input  logic rx_linkactivereq,
    input  logic tx_linkactiveack,
    output logic tx_linkactivereq,
    output logic rx_linkactiveack,

    // ---------------- AXI write address channel ------------------------------
    output logic                                                     awvalid,
    input  logic                                                     awready,
    output logic [c2xm_core_types_pkg::AXI_AW_PAYLOAD_ADDRESS_WIDTH-1:0] awaddr,
    output logic [c2xm_core_types_pkg::AXI_AW_PAYLOAD_LENGTH_WIDTH-1:0]  awlen,
    output logic [c2xm_core_types_pkg::AXI_AW_PAYLOAD_SIZE_WIDTH-1:0]    awsize,
    output logic [c2xm_core_types_pkg::AXI_AW_PAYLOAD_BURST_WIDTH-1:0]   awburst,
    output logic [c2xm_core_types_pkg::AXI_AW_PAYLOAD_QOS_WIDTH-1:0]     awqos,
    output logic [c2xm_core_types_pkg::AXI_AW_PAYLOAD_CACHE_WIDTH-1:0]   awcache,
    output logic [c2xm_core_types_pkg::AXI_AW_PAYLOAD_PROTECTION_WIDTH-1:0] awprot,
    output logic [c2xm_core_types_pkg::AXI_AW_PAYLOAD_ID_WIDTH-1:0]      awid,
    output logic                                                     awlock,

    // ---------------- AXI write data channel ---------------------------------
    output logic                                                   wvalid,
    input  logic                                                   wready,
    output logic [c2xm_core_types_pkg::AXI_WPAYLOAD_DATA_WIDTH-1:0] wdata,
    output logic [c2xm_core_types_pkg::AXI_WPAYLOAD_STROBE_WIDTH-1:0] wstrb,
    output logic                                                   wlast,
    output logic [3:0]                                             wpoison,
    output logic [31:0]                                            wdatachk,

    // ---------------- AXI write response channel -----------------------------
    input  logic                                                  bvalid,
    output logic                                                  bready,
    input  logic [c2xm_core_types_pkg::AXI_BPAYLOAD_ID_WIDTH-1:0] bid,
    input  logic [c2xm_core_types_pkg::AXI_BPAYLOAD_RESPONSE_WIDTH-1:0] bresp,

    // ---------------- AXI read address channel -------------------------------
    output logic                                                     arvalid,
    input  logic                                                     arready,
    output logic [c2xm_core_types_pkg::AXI_AR_PAYLOAD_QOS_WIDTH-1:0]     arqos,
    output logic                                                     arlock,
    output logic [c2xm_core_types_pkg::AXI_AR_PAYLOAD_LENGTH_WIDTH-1:0]  arlen,
    output logic [c2xm_core_types_pkg::AXI_AR_PAYLOAD_ID_WIDTH-1:0]      arid,
    output logic [c2xm_core_types_pkg::AXI_AR_PAYLOAD_PROTECTION_WIDTH-1:0] arprot,
    output logic [c2xm_core_types_pkg::AXI_AR_PAYLOAD_CACHE_WIDTH-1:0]   arcache,
    output logic [c2xm_core_types_pkg::AXI_AR_PAYLOAD_BURST_WIDTH-1:0]   arburst,
    output logic [c2xm_core_types_pkg::AXI_AR_PAYLOAD_SIZE_WIDTH-1:0]    arsize,
    output logic [c2xm_core_types_pkg::AXI_AR_PAYLOAD_ADDRESS_WIDTH-1:0] araddr,
    output logic [3:0]                                               arsnoop,
    output logic [1:0]                                               ardomain,

    // ---------------- AXI read data channel ----------------------------------
    input  logic                                                   rvalid,
    output logic                                                   rready,
    input  logic [c2xm_core_types_pkg::AXI_RPAYLOAD_DATA_WIDTH-1:0] rdata,
    input  logic [c2xm_core_types_pkg::AXI_RPAYLOAD_RESPONSE_WIDTH-1:0] rresp,
    input  logic                                                   rlast,
    input  logic [c2xm_core_types_pkg::AXI_RPAYLOAD_ID_WIDTH-1:0]   rid,
    input  logic [31:0]                                            rdatachk,
    input  logic [3:0]                                             rpoison,
    input  logic                                                   rtrace,
    input  logic [3:0]                                             ruser,

    // ---------------- TB-only debug taps (NOT part of c2xm_top) --------------
    // Link-layer run gates inside c2xm_top; exposed so the testbench can tell
    // when LINKACTIVE negotiation has finished without guessing.
    output logic dbg_chi_rx_run,
    output logic dbg_chi_tx_run
);

    // ---------------------------------------------------------------------
    // Static configuration.  Values are placeholders: change them here (or
    // override with a plusarg-driven `initial` block) when a test needs a
    // different address map / starvation policy.
    // ---------------------------------------------------------------------
    localparam logic [c2xm_core_types_pkg::CONFIG_NODEID_WIDTH-1:0] NODEID = '0;

    localparam logic [c2xm_core_types_pkg::CONFIG_CFG_C2XM_AWID_STARVATION_THRESHOLD_WIDTH-1:0]
        CFG_AWID_STARVATION_THRESHOLD = 8'h10;
    localparam logic [c2xm_core_types_pkg::CONFIG_CFG_C2XM_DBBIDM_STARVATION_THRESHOLD_WIDTH-1:0]
        CFG_DBBIDM_STARVATION_THRESHOLD = 8'h10;
    localparam logic [c2xm_core_types_pkg::CONFIG_CFG_C2XM_RBANK_STARYATION_THRESHOLD_WIDTH-1:0]
        CFG_RBANK_STARVATION_THRESHOLD = 8'h10;
    localparam logic [c2xm_core_types_pkg::CONFIG_CFG_C2XM_ARM_STARVATION_THRESHOLD_WIDTH-1:0]
        CFG_ARM_STARVATION_THRESHOLD = 8'h10;

    localparam logic [c2xm_core_types_pkg::CONFIG_CFG_C2XM_ADDR_OFFSET_WIDTH-1:0]
        CFG_ADDR_OFFSET = '0;

    // Address-cut table: all six regions disabled, index 0.
    localparam logic       CFG_ADDR_CUT_ENABLE0 = 1'b0;
    localparam logic [7:0] CFG_ADDR_CUT_INDEX0  = 8'h00;
    localparam logic       CFG_ADDR_CUT_ENABLE1 = 1'b0;
    localparam logic [7:0] CFG_ADDR_CUT_INDEX1  = 8'h00;
    localparam logic       CFG_ADDR_CUT_ENABLE2 = 1'b0;
    localparam logic [7:0] CFG_ADDR_CUT_INDEX2  = 8'h00;
    localparam logic       CFG_ADDR_CUT_ENABLE3 = 1'b0;
    localparam logic [7:0] CFG_ADDR_CUT_INDEX3  = 8'h00;
    localparam logic       CFG_ADDR_CUT_ENABLE4 = 1'b0;
    localparam logic [7:0] CFG_ADDR_CUT_INDEX4  = 8'h00;
    localparam logic       CFG_ADDR_CUT_ENABLE5 = 1'b0;
    localparam logic [7:0] CFG_ADDR_CUT_INDEX5  = 8'h00;

    c2xm_top #(
        .hnf_node_ids(HNF_NODE_IDS)
    ) u_dut (
        .clk(clk),
        .rstn(rstn),
        .nodeid(NODEID),
        .cfg_c2xm_awid_starvation_threshold(CFG_AWID_STARVATION_THRESHOLD),
        .cfg_c2xm_dbbidm_starvation_threshold(CFG_DBBIDM_STARVATION_THRESHOLD),
        .cfg_c2xm_rbank_staryation_threshold(CFG_RBANK_STARVATION_THRESHOLD),
        .cfg_c2xm_arm_starvation_threshold(CFG_ARM_STARVATION_THRESHOLD),
        .cfg_c2xm_addr_offset(CFG_ADDR_OFFSET),
        .cfg_c2xm_addr_cut_enable0(CFG_ADDR_CUT_ENABLE0),
        .cfg_c2xm_addr_cut_index0(CFG_ADDR_CUT_INDEX0),
        .cfg_c2xm_addr_cut_enable1(CFG_ADDR_CUT_ENABLE1),
        .cfg_c2xm_addr_cut_index1(CFG_ADDR_CUT_INDEX1),
        .cfg_c2xm_addr_cut_enable2(CFG_ADDR_CUT_ENABLE2),
        .cfg_c2xm_addr_cut_index2(CFG_ADDR_CUT_INDEX2),
        .cfg_c2xm_addr_cut_enable3(CFG_ADDR_CUT_ENABLE3),
        .cfg_c2xm_addr_cut_index3(CFG_ADDR_CUT_INDEX3),
        .cfg_c2xm_addr_cut_enable4(CFG_ADDR_CUT_ENABLE4),
        .cfg_c2xm_addr_cut_index4(CFG_ADDR_CUT_INDEX4),
        .cfg_c2xm_addr_cut_enable5(CFG_ADDR_CUT_ENABLE5),
        .cfg_c2xm_addr_cut_index5(CFG_ADDR_CUT_INDEX5),

        // CHI link
        .rxreqfltv(rxreqfltv),
        .rxreqflt(rxreqflt),
        .rxreqcrdv(rxreqcrdv),
        .rxdatfltv(rxdatfltv),
        .rxdatflt(rxdatflt),
        .rxdatcrdv(rxdatcrdv),
        .txrspfltv(txrspfltv),
        .txrspflt(txrspflt),
        .txrspcrdv(txrspcrdv),
        .txdatfltv(txdatfltv),
        .txdatflt(txdatflt),
        .txdatcrdv(txdatcrdv),
        .disconnect(disconnect),
        .rx_linkactivereq(rx_linkactivereq),
        .tx_linkactiveack(tx_linkactiveack),
        .tx_linkactivereq(tx_linkactivereq),
        .rx_linkactiveack(rx_linkactiveack),

        // AXI master
        .awvalid(awvalid),
        .awready(awready),
        .awaddr(awaddr),
        .awlen(awlen),
        .awsize(awsize),
        .awburst(awburst),
        .awqos(awqos),
        .awcache(awcache),
        .awprot(awprot),
        .awid(awid),
        .awlock(awlock),
        .wvalid(wvalid),
        .wready(wready),
        .wdata(wdata),
        .wstrb(wstrb),
        .wlast(wlast),
        .wpoison(wpoison),
        .wdatachk(wdatachk),
        .bvalid(bvalid),
        .bready(bready),
        .bid(bid),
        .bresp(bresp),
        .arvalid(arvalid),
        .arready(arready),
        .arqos(arqos),
        .arlock(arlock),
        .arlen(arlen),
        .arid(arid),
        .arprot(arprot),
        .arcache(arcache),
        .arburst(arburst),
        .arsize(arsize),
        .araddr(araddr),
        .arsnoop(arsnoop),
        .ardomain(ardomain),
        .rvalid(rvalid),
        .rready(rready),
        .rdata(rdata),
        .rresp(rresp),
        .rlast(rlast),
        .rid(rid),
        .rdatachk(rdatachk),
        .rpoison(rpoison),
        .rtrace(rtrace),
        .ruser(ruser)
    );

`ifdef C2XM_DBG_POOL
    dbg_pool u_dbg_pool (.clk(clk), .rstn(rstn));
`endif

    // Link-layer run gates (hierarchical debug taps only).
    assign dbg_chi_rx_run = u_dut.chi_rx_run;
    assign dbg_chi_tx_run = u_dut.chi_tx_run;

    // ------------------------------------------------------------------
    // Waveform dumping, plusarg-gated so normal runs pay nothing.
    //   +C2XM_FSDB=<file>  FSDB for Verdi (compile with the Verdi PLI,
    //                      see Makefile WAVES=fsdb)
    //   +C2XM_VCD=<file>   plain VCD (no extra compile flags)
    // Depth 1 keeps it to the TB-level CHI/AXI channels; raise it if the
    // internals of u_dut are needed.
    // ------------------------------------------------------------------
    initial begin
        string dump_file;
        if ($value$plusargs("C2XM_FSDB=%s", dump_file)) begin
            $fsdbDumpfile(dump_file);
            $fsdbDumpvars(1, c2xm_tb_top);
        end else if ($value$plusargs("C2XM_VCD=%s", dump_file)) begin
            $dumpfile(dump_file);
            $dumpvars(1, c2xm_tb_top);
        end
    end

endmodule
