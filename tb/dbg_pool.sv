// TEMPORARY debug tap - dumps transaction pool entries for the write path.
// Compiled only when +define+C2XM_DBG_POOL is passed.
module dbg_pool (
  input logic clk,
  input logic rstn
);
  int unsigned cyc;
  always_ff @(posedge clk) begin
    if (!rstn) begin
      cyc <= 0;
    end else begin
      cyc <= cyc + 1;
      if (cyc % 20 == 0) begin
        for (int unsigned q = 0; q < 6; q++) begin
          if (c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].control.admission_state != 2'd0)
            $display("[%0t] POOL[%0d] st=%0d isw=%0b isr=%0b rmask=%b emask=%b disp=%0b dep=%0b logb=%0b kind=%0d txn=%0d addr=%h",
              $time, q,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].control.admission_state,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].content.is_write,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].content.is_read,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].control.received_data_mask,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].control.expected_data_mask,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].control.dispatched,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].control.dependency_valid,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].control.logbuf_binding_valid,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].control.initial_response_kind,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].content.txn_id,
              c2xm_tb_top.u_dut.u_core.u_resource_transaction_pool.entries_q[q].content.axi_address);
        end
      end
    end
  end
endmodule
