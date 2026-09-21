# ---------------------------------------------------------------------------
# pyuvm + cocotb environment for c2xm_top (CHI ReadNoSnp/WriteNoSnp -> AXI)
#
#   make            compile and run every test in c2xm_test.py
#   make TESTCASE=C2xmReadNoSnpTest   run one test
#   make GUI=1      rebuild with -gui -kdb (Verdi/DVE)
#   make clean      remove sim_build
#
# The RTL is compiled straight out of c2xm_generated_dsl_core_20260916 using
# the same order as its filelist_rtl.f; override C2XM_ROOT to point at a
# different generated tree.
# ---------------------------------------------------------------------------

SIM ?= vcs
TOPLEVEL_LANG = verilog

C2XM_ROOT ?= $(abspath $(PWD)/../c2xm_generated_dsl_core_20260916)
RTL_DIR := $(C2XM_ROOT)/rtl

VERILOG_INCLUDE_DIRS = $(RTL_DIR)

# Same order as $(C2XM_ROOT)/filelist_rtl.f, plus the testbench wrapper last.
VERILOG_SOURCES = \
    $(RTL_DIR)/c2xm_core_common_defines.sv \
    $(RTL_DIR)/c2xm_core_types_pkg.sv \
    $(RTL_DIR)/c2xm_dsl_fifo.sv \
    $(RTL_DIR)/external/c2xm_dsl_vc_rx_adapter.sv \
    $(RTL_DIR)/external/c2xm_dsl_chi_txrsp_link.sv \
    $(RTL_DIR)/external/c2xm_dsl_vc_tx_adapter.sv \
    $(RTL_DIR)/external/dsu_chib_dev_linkhandshake.sv \
    $(RTL_DIR)/external/c2xm_link_handshake_wrapper.sv \
    $(RTL_DIR)/c2xm_core_transaction_pool_resource.sv \
    $(RTL_DIR)/c2xm_core_log_buffer_resource.sv \
    $(RTL_DIR)/c2xm_core_read_response_queue_resource.sv \
    $(RTL_DIR)/c2xm_core_retry_bank_resource.sv \
    $(RTL_DIR)/c2xm_core_admit_and_allocate_pool_entry_operation.sv \
    $(RTL_DIR)/c2xm_core_receive_rxdat_operation.sv \
    $(RTL_DIR)/c2xm_core_receive_b_operation.sv \
    $(RTL_DIR)/c2xm_core_release_pool_transaction_operation.sv \
    $(RTL_DIR)/c2xm_core_issue_aw_operation.sv \
    $(RTL_DIR)/c2xm_core_issue_wdata_operation.sv \
    $(RTL_DIR)/c2xm_core_issue_write_completion_operation.sv \
    $(RTL_DIR)/c2xm_core_issue_initial_write_response_operation.sv \
    $(RTL_DIR)/c2xm_core_issue_txrsp_operation.sv \
    $(RTL_DIR)/c2xm_core_issue_ar_from_pool_operation.sv \
    $(RTL_DIR)/c2xm_core_receive_axi_r_and_emit_chi_data_operation.sv \
    $(RTL_DIR)/c2xm_core.sv \
    $(RTL_DIR)/c2xm_top.sv \
    $(PWD)/tb/c2xm_tb_top.sv

COCOTB_TOPLEVEL = c2xm_tb_top
COCOTB_TEST_MODULES = c2xm_test
COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

# Keep simulation logs in one place.
COCOTB_LOG_LEVEL ?= INFO

# make DBG_POOL=1 dumps the C2XM transaction pool from tb/dbg_pool.sv.
DBG_POOL ?= 0
ifeq ($(DBG_POOL),1)
    VERILOG_SOURCES += $(PWD)/tb/dbg_pool.sv
    COMPILE_ARGS += +define+C2XM_DBG_POOL
endif

# make WAVES=fsdb  dump FSDB for Verdi (links the Verdi PLI at compile;
#                   NOVAS_HOME must be set), WAVES=vcd for a plain VCD.
#                   The file lands in the run directory as waves.fsdb/vcd.
WAVES ?= none
ifeq ($(WAVES),fsdb)
    NOVAS ?= $(NOVAS_HOME)
    # separate build dir: the Verdi PLI changes the compile command and
    # the plain sim_build cache would not notice.
    SIM_BUILD ?= sim_build_fsdb
    COMPILE_ARGS += -P $(NOVAS)/share/PLI/VCS/LINUX64/novas.tab \
                    $(NOVAS)/share/PLI/VCS/LINUX64/pli.a
    PLUSARGS += +C2XM_FSDB=waves.fsdb
else ifeq ($(WAVES),vcd)
    PLUSARGS += +C2XM_VCD=waves.vcd
endif

# ---------------------------------------------------------------------------
# Co-simulation (C2xmCosimTest <-> gem5/fake_gem5 over a UNIX socket)
#
#   make cosim SOCKET=/path/to.sock          # peer must already be listening
#   make cosim-fake SCENARIO=all             # one-shot with fake_gem5.py
#
# SCENARIO: read | write | mixed | bypass | all (fake_gem5 only)
# ---------------------------------------------------------------------------
SOCKET ?= /tmp/c2xm_cosim.sock
SCENARIO ?= all
COSIM_CLK_NS ?= 0.556

# The cosim targets below precede the cocotb include, so pin the default
# goal back to the included Makefile.sim's run target.
.DEFAULT_GOAL := sim

.PHONY: cosim cosim-fake
cosim:
	C2XM_COSIM_SOCK=$(SOCKET) C2XM_COSIM_CLK_NS=$(COSIM_CLK_NS) \
	    $(MAKE) TESTCASE=C2xmCosimTest sim

cosim-fake:
	rm -f $(SOCKET) fake_gem5.log
	python3 fake_gem5.py --socket $(SOCKET) --scenario $(SCENARIO) \
	    > fake_gem5.log 2>&1 & \
	    fakepid=$$!; sleep 1; \
	    C2XM_COSIM_SOCK=$(SOCKET) C2XM_COSIM_CLK_NS=$(COSIM_CLK_NS) \
	    $(MAKE) TESTCASE=C2xmCosimTest sim; rc=$$?; \
	    wait $$fakepid; frc=$$?; \
	    echo "--- fake_gem5.log ---"; cat fake_gem5.log; \
	    if [ $$rc -eq 0 ] && [ $$frc -ne 0 ]; then rc=$$frc; fi; \
	    echo "make_rc=$$rc fake_gem5_rc=$$frc"; exit $$rc

include $(shell cocotb-config --makefiles)/Makefile.sim
