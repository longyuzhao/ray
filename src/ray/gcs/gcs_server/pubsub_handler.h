// Copyright 2021 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include "ray/gcs/pubsub/gcs_pub_sub.h"
#include "ray/rpc/gcs_server/gcs_rpc_server.h"

namespace ray {
namespace gcs {

/// This is the implementation class of `InternalPubsubHandler`.
/// It supports subscribing updates from GCS with long poll, and registering /
/// de-registering subscribers.
class InternalPubSubHandler : public rpc::InternalPubSubHandler {
 public:
  explicit InternalPubSubHandler(const std::shared_ptr<gcs::GcsPublisher> &gcs_publisher)
      : gcs_publisher_(gcs_publisher) {}

  void HandleGcsSubscriberPoll(const rpc::GcsSubscriberPollRequest &request,
                               rpc::GcsSubscriberPollReply *reply,
                               rpc::SendReplyCallback send_reply_callback) final;

  void HandleGcsSubscriberCommandBatch(
      const rpc::GcsSubscriberCommandBatchRequest &request,
      rpc::GcsSubscriberCommandBatchReply *reply,
      rpc::SendReplyCallback send_reply_callback) final;

  std::string DebugString() const;

 private:
  std::shared_ptr<gcs::GcsPublisher> gcs_publisher_;
};

}  // namespace gcs
}  // namespace ray
