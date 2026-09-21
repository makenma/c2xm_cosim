// Persistent, line-oriented RPC wrapper around the XLS Proc JIT runtime.
//
// The process owns one Proc network for its whole lifetime.  Clients can push
// typed values into boundary channels, advance the network by a fixed number
// of logical Proc ticks, and drain boundary outputs without lowering the
// design to RTL.

#include <cstdint>
#include <iostream>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/match.h"
#include "absl/strings/numbers.h"
#include "absl/strings/str_cat.h"
#include "absl/strings/str_split.h"
#include "xls/common/file/filesystem.h"
#include "xls/common/init_xls.h"
#include "xls/interpreter/channel_queue.h"
#include "xls/interpreter/evaluator_options.h"
#include "xls/interpreter/serial_proc_runtime.h"
#include "xls/ir/ir_parser.h"
#include "xls/ir/package.h"
#include "xls/ir/value.h"
#include "xls/jit/jit_evaluator_options.h"
#include "xls/jit/jit_proc_runtime.h"

namespace {

constexpr std::string_view kUsage = R"(Persistent C2XM XLS Proc RPC runtime.

Usage: xls_proc_rpc_main <optimized-ir-file>

Commands on stdin (tab separated):
  write <channel> <value>  enqueue a value (typed or human XLS syntax)
  read  <channel>          dequeue one value, or return EMPTY
  size  <channel>          return the current queue depth
  tick  [count]            advance by count logical Proc ticks (default: 1)
  reset                    recreate the runtime, clearing state and queues
  quit                     exit
)";

void ReplyOk(std::string_view payload = "") {
  if (payload.empty()) {
    std::cout << "OK\n";
  } else {
    std::cout << "OK\t" << payload << "\n";
  }
  std::cout.flush();
}

void ReplyError(const absl::Status& status) {
  std::string message(status.message());
  for (char& c : message) {
    if (c == '\n' || c == '\r' || c == '\t') {
      c = ' ';
    }
  }
  std::cout << "ERR\t" << static_cast<int>(status.code()) << "\t" << message
            << "\n";
  std::cout.flush();
}

absl::Status Run(std::string_view ir_path) {
  absl::StatusOr<std::string> ir_text = xls::GetFileContents(ir_path);
  if (!ir_text.ok()) {
    return ir_text.status();
  }
  absl::StatusOr<std::unique_ptr<xls::Package>> package_or =
      xls::Parser::ParsePackage(*ir_text, std::string(ir_path));
  if (!package_or.ok()) {
    return package_or.status();
  }
  std::unique_ptr<xls::Package> package = std::move(*package_or);

  xls::EvaluatorOptions evaluator_options;
  xls::JitEvaluatorOptions jit_options;
  auto create_runtime = [&]()
      -> absl::StatusOr<std::unique_ptr<xls::SerialProcRuntime>> {
    if (package->ChannelsAreProcScoped()) {
      absl::StatusOr<xls::Proc*> top = package->GetTopAsProc();
      if (!top.ok()) {
        return top.status();
      }
      return xls::CreateJitSerialProcRuntime(*top, evaluator_options,
                                             jit_options);
    }
    return xls::CreateJitSerialProcRuntime(package.get(), evaluator_options,
                                           jit_options);
  };
  absl::StatusOr<std::unique_ptr<xls::SerialProcRuntime>> runtime_or =
      create_runtime();
  if (!runtime_or.ok()) {
    return runtime_or.status();
  }
  std::unique_ptr<xls::SerialProcRuntime> runtime = std::move(*runtime_or);
  xls::ChannelQueueManager* queues = &runtime->queue_manager();

  std::cout << "READY\t" << package->name() << "\n";
  std::cout.flush();

  std::string line;
  while (std::getline(std::cin, line)) {
    std::vector<std::string_view> fields = absl::StrSplit(line, '\t');
    if (fields.empty()) {
      ReplyError(absl::InvalidArgumentError("empty command"));
      continue;
    }
    const std::string_view command = fields[0];
    if (command == "quit") {
      ReplyOk();
      return absl::OkStatus();
    }
    if (command == "reset") {
      runtime.reset();
      runtime_or = create_runtime();
      if (!runtime_or.ok()) {
        ReplyError(runtime_or.status());
        return runtime_or.status();
      }
      runtime = std::move(*runtime_or);
      queues = &runtime->queue_manager();
      ReplyOk();
      continue;
    }
    if (command == "tick") {
      int64_t requested_ticks = 1;
      if (fields.size() >= 2 &&
          !absl::SimpleAtoi(fields[1], &requested_ticks)) {
        ReplyError(absl::InvalidArgumentError("invalid tick count"));
        continue;
      }
      if (requested_ticks < 0) {
        ReplyError(absl::InvalidArgumentError("tick count must be nonnegative"));
        continue;
      }
      int64_t active_ticks = 0;
      absl::Status tick_status;
      for (; active_ticks < requested_ticks; ++active_ticks) {
        tick_status = runtime->Tick();
        if (!tick_status.ok()) {
          // With no boundary input, an otherwise healthy Proc network is
          // reported as deadlocked. Remaining requested ticks are idle.
          if (tick_status.code() == absl::StatusCode::kInternal &&
              absl::StartsWith(tick_status.message(),
                               "Proc network is deadlocked.")) {
            break;
          }
          ReplyError(tick_status);
          break;
        }
      }
      if (tick_status.ok() ||
          (tick_status.code() == absl::StatusCode::kInternal &&
           absl::StartsWith(tick_status.message(),
                            "Proc network is deadlocked."))) {
        ReplyOk(absl::StrCat(requested_ticks, "\t", active_ticks));
      }
      continue;
    }
    if (fields.size() < 2) {
      ReplyError(absl::InvalidArgumentError("channel name is required"));
      continue;
    }
    absl::StatusOr<xls::ChannelQueue*> queue_or =
        queues->GetBoundaryQueueByName(fields[1]);
    if (!queue_or.ok()) {
      ReplyError(queue_or.status());
      continue;
    }
    xls::ChannelQueue* queue = *queue_or;
    if (command == "size") {
      ReplyOk(absl::StrCat(queue->GetSize()));
      continue;
    }
    if (command == "read") {
      std::optional<xls::Value> value = queue->Read();
      if (!value.has_value()) {
        std::cout << "EMPTY\n";
      } else {
        std::cout << "VALUE\t"
                  << value->ToString(xls::FormatPreference::kHex) << "\n";
      }
      std::cout.flush();
      continue;
    }
    if (command == "write") {
      if (fields.size() < 3) {
        ReplyError(absl::InvalidArgumentError("value is required"));
        continue;
      }
      absl::StatusOr<xls::Value> value =
          xls::Parser::ParseValue(fields[2], queue->channel()->type());
      if (!value.ok()) {
        ReplyError(value.status());
        continue;
      }
      absl::Status status = queue->Write(*value);
      if (!status.ok()) {
        ReplyError(status);
      } else {
        ReplyOk();
      }
      continue;
    }
    ReplyError(absl::InvalidArgumentError(
        absl::StrCat("unknown command: ", command)));
  }
  return absl::OkStatus();
}

}  // namespace

int main(int argc, char* argv[]) {
  std::vector<std::string_view> args = xls::InitXls(kUsage, argc, argv);
  if (args.size() != 1) {
    std::cerr << kUsage;
    return 2;
  }
  absl::Status status = Run(args[0]);
  if (!status.ok()) {
    std::cerr << status << "\n";
    return 1;
  }
  return 0;
}
