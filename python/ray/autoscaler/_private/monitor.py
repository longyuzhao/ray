"""Autoscaler monitoring loop daemon."""

import argparse
import logging
import logging.handlers
import os
import sys
import signal
import time
import traceback
import json
from multiprocessing.synchronize import Event
from typing import Optional

try:
    import prometheus_client
except ImportError:
    prometheus_client = None

import ray
from ray.autoscaler._private.autoscaler import StandardAutoscaler
from ray.autoscaler._private.commands import teardown_cluster
from ray.autoscaler._private.constants import AUTOSCALER_UPDATE_INTERVAL_S, \
    AUTOSCALER_METRIC_PORT
from ray.autoscaler._private.event_summarizer import EventSummarizer
from ray.autoscaler._private.prom_metrics import AutoscalerPrometheusMetrics
from ray.autoscaler._private.load_metrics import LoadMetrics
from ray.autoscaler._private.constants import \
    AUTOSCALER_MAX_RESOURCE_DEMAND_VECTOR_SIZE
from ray.autoscaler._private.fake_multi_node.node_provider import \
    FAKE_HEAD_NODE_ID
from ray.autoscaler._private.util import DEBUG_AUTOSCALING_STATUS, \
    DEBUG_AUTOSCALING_ERROR, format_readonly_node_type

from ray.core.generated import gcs_service_pb2, gcs_service_pb2_grpc
import ray.ray_constants as ray_constants
from ray._private.ray_logging import setup_component_logger
from ray.experimental.internal_kv import _internal_kv_put, \
    _internal_kv_initialized, _internal_kv_get, _internal_kv_del
from ray._raylet import connect_to_gcs, disconnect_from_gcs
import ray._private.utils

logger = logging.getLogger(__name__)


def parse_resource_demands(resource_load_by_shape):
    """Handle the message.resource_load_by_shape protobuf for the demand
    based autoscaling. Catch and log all exceptions so this doesn't
    interfere with the utilization based autoscaler until we're confident
    this is stable. Worker queue backlogs are added to the appropriate
    resource demand vector.

    Args:
        resource_load_by_shape (pb2.gcs.ResourceLoad): The resource demands
            in protobuf form or None.

    Returns:
        List[ResourceDict]: Waiting bundles (ready and feasible).
        List[ResourceDict]: Infeasible bundles.
    """
    waiting_bundles, infeasible_bundles = [], []
    try:
        for resource_demand_pb in list(
                resource_load_by_shape.resource_demands):
            request_shape = dict(resource_demand_pb.shape)
            for _ in range(resource_demand_pb.num_ready_requests_queued):
                waiting_bundles.append(request_shape)
            for _ in range(resource_demand_pb.num_infeasible_requests_queued):
                infeasible_bundles.append(request_shape)

            # Infeasible and ready states for tasks are (logically)
            # mutually exclusive.
            if resource_demand_pb.num_infeasible_requests_queued > 0:
                backlog_queue = infeasible_bundles
            else:
                backlog_queue = waiting_bundles
            for _ in range(resource_demand_pb.backlog_size):
                backlog_queue.append(request_shape)
            if len(waiting_bundles+infeasible_bundles) > \
                    AUTOSCALER_MAX_RESOURCE_DEMAND_VECTOR_SIZE:
                break
    except Exception:
        logger.exception("Failed to parse resource demands.")

    return waiting_bundles, infeasible_bundles


# Readonly provider config (e.g., for laptop mode, manually setup clusters).
BASE_READONLY_CONFIG = {
    "cluster_name": "default",
    "max_workers": 0,
    "upscaling_speed": 1.0,
    "docker": {},
    "idle_timeout_minutes": 0,
    "provider": {
        "type": "readonly",
        "use_node_id_as_ip": True,  # For emulated multi-node on laptop.
    },
    "auth": {},
    "available_node_types": {
        "ray.head.default": {
            "resources": {},
            "node_config": {},
            "max_workers": 0
        }
    },
    "head_node_type": "ray.head.default",
    "file_mounts": {},
    "cluster_synced_files": [],
    "file_mounts_sync_continuously": False,
    "rsync_exclude": [],
    "rsync_filter": [],
    "initialization_commands": [],
    "setup_commands": [],
    "head_setup_commands": [],
    "worker_setup_commands": [],
    "head_start_ray_commands": [],
    "worker_start_ray_commands": [],
    "head_node": {},
    "worker_nodes": {}
}


class Monitor:
    """Autoscaling monitor.

    This process periodically collects stats from the GCS and triggers
    autoscaler updates.

    Attributes:
        redis: A connection to the Redis server.
    """

    def __init__(self,
                 redis_address,
                 autoscaling_config,
                 redis_password=None,
                 prefix_cluster_info=False,
                 monitor_ip=None,
                 stop_event: Optional[Event] = None):
        # Initialize the Redis clients.
        ray.state.state._initialize_global_state(
            redis_address, redis_password=redis_password)
        self.redis = ray._private.services.create_redis_client(
            redis_address, password=redis_password)
        if monitor_ip:
            self.redis.set("AutoscalerMetricsAddress",
                           f"{monitor_ip}:{AUTOSCALER_METRIC_PORT}")
        (ip, port) = redis_address.split(":")
        self.gcs_client = connect_to_gcs(ip, int(port), redis_password)
        # Initialize the gcs stub for getting all node resource usage.
        gcs_address = self.redis.get("GcsServerAddress").decode("utf-8")
        options = (("grpc.enable_http_proxy", 0), )
        gcs_channel = ray._private.utils.init_grpc_channel(
            gcs_address, options)
        self.gcs_node_resources_stub = \
            gcs_service_pb2_grpc.NodeResourceInfoGcsServiceStub(gcs_channel)

        # Set the redis client and mode so _internal_kv works for autoscaler.
        worker = ray.worker.global_worker
        worker.redis_client = self.redis
        worker.gcs_client = self.gcs_client
        worker.mode = 0
        head_node_ip = redis_address.split(":")[0]
        self.redis_address = redis_address
        self.redis_password = redis_password
        if os.environ.get("RAY_FAKE_CLUSTER"):
            self.load_metrics = LoadMetrics(local_ip=FAKE_HEAD_NODE_ID)
        else:
            self.load_metrics = LoadMetrics(local_ip=head_node_ip)
        self.last_avail_resources = None
        self.event_summarizer = EventSummarizer()
        self.prefix_cluster_info = prefix_cluster_info
        # Can be used to signal graceful exit from monitor loop.
        self.stop_event = stop_event  # type: Optional[Event]
        self.autoscaling_config = autoscaling_config
        self.autoscaler = None
        # If set, we are in a manually created cluster (non-autoscaling) and
        # simply mirroring what the GCS tells us the cluster node types are.
        self.readonly_config = None

        self.prom_metrics = AutoscalerPrometheusMetrics()
        if monitor_ip and prometheus_client:
            # If monitor_ip wasn't passed in, then don't attempt to start the
            # metric server to keep behavior identical to before metrics were
            # introduced
            try:
                logger.info(
                    "Starting autoscaler metrics server on port {}".format(
                        AUTOSCALER_METRIC_PORT))
                prometheus_client.start_http_server(
                    port=AUTOSCALER_METRIC_PORT,
                    addr="127.0.0.1" if head_node_ip == "127.0.0.1" else "",
                    registry=self.prom_metrics.registry)
            except Exception:
                logger.exception(
                    "An exception occurred while starting the metrics server.")
        elif not prometheus_client:
            logger.warning("`prometheus_client` not found, so metrics will "
                           "not be exported.")

        logger.info("Monitor: Started")

    def __del__(self):
        disconnect_from_gcs(self.gcs_client)

    def _initialize_autoscaler(self):
        if self.autoscaling_config:
            autoscaling_config = self.autoscaling_config
        else:
            # This config mirrors the current setup of the manually created
            # cluster. Each node gets its own unique node type.
            self.readonly_config = BASE_READONLY_CONFIG

            # Note that the "available_node_types" of the config can change.
            def get_latest_readonly_config():
                return self.readonly_config

            autoscaling_config = get_latest_readonly_config
        self.autoscaler = StandardAutoscaler(
            autoscaling_config,
            self.load_metrics,
            prefix_cluster_info=self.prefix_cluster_info,
            event_summarizer=self.event_summarizer,
            prom_metrics=self.prom_metrics)

    def update_load_metrics(self):
        """Fetches resource usage data from GCS and updates load metrics."""

        request = gcs_service_pb2.GetAllResourceUsageRequest()
        response = self.gcs_node_resources_stub.GetAllResourceUsage(
            request, timeout=60)
        resources_batch_data = response.resource_usage_data

        # Tell the readonly node provider what nodes to report.
        if self.readonly_config:
            new_nodes = []
            for msg in list(resources_batch_data.batch):
                node_id = msg.node_id.hex()
                new_nodes.append((node_id, msg.node_manager_address))
            self.autoscaler.provider._set_nodes(new_nodes)

        mirror_node_types = {}
        cluster_full = False
        for resource_message in resources_batch_data.batch:
            # Generate node type config based on GCS reported node list.
            if self.readonly_config:
                # Keep prefix in sync with ReadonlyNodeProvider.
                node_type = format_readonly_node_type(
                    resource_message.node_id.hex())
                resources = {}
                for k, v in resource_message.resources_total.items():
                    resources[k] = v
                mirror_node_types[node_type] = {
                    "resources": resources,
                    "node_config": {},
                    "max_workers": 1,
                }
            if (hasattr(resource_message, "cluster_full_of_actors_detected")
                    and resource_message.cluster_full_of_actors_detected):
                # Aggregate this flag across all batches.
                cluster_full = True
            resource_load = dict(resource_message.resource_load)
            total_resources = dict(resource_message.resources_total)
            available_resources = dict(resource_message.resources_available)

            waiting_bundles, infeasible_bundles = parse_resource_demands(
                resources_batch_data.resource_load_by_shape)

            pending_placement_groups = list(
                resources_batch_data.placement_group_load.placement_group_data)

            use_node_id_as_ip = (self.autoscaler is not None
                                 and self.autoscaler.config["provider"].get(
                                     "use_node_id_as_ip", False))
            if use_node_id_as_ip:
                peloton_id = total_resources.get("NODE_ID_AS_RESOURCE")
                # Legacy support https://github.com/ray-project/ray/pull/17312
                if peloton_id is not None:
                    ip = str(int(peloton_id))
                else:
                    ip = resource_message.node_id.hex()
            else:
                ip = resource_message.node_manager_address
            self.load_metrics.update(ip, total_resources, available_resources,
                                     resource_load, waiting_bundles,
                                     infeasible_bundles,
                                     pending_placement_groups, cluster_full)
        if self.readonly_config:
            self.readonly_config["available_node_types"].update(
                mirror_node_types)

    def update_resource_requests(self):
        """Fetches resource requests from the internal KV and updates load."""
        if not _internal_kv_initialized():
            return
        data = _internal_kv_get(
            ray.ray_constants.AUTOSCALER_RESOURCE_REQUEST_CHANNEL)
        if data:
            try:
                resource_request = json.loads(data)
                self.load_metrics.set_resource_requests(resource_request)
            except Exception:
                logger.exception("Error parsing resource requests")

    def _run(self):
        """Run the monitor loop."""
        while True:
            if self.stop_event and self.stop_event.is_set():
                break
            self.update_load_metrics()
            self.update_resource_requests()
            self.update_event_summary()
            status = {
                "load_metrics_report": self.load_metrics.summary()._asdict(),
                "time": time.time(),
                "monitor_pid": os.getpid()
            }

            # Process autoscaling actions
            if self.autoscaler:
                # Only used to update the load metrics for the autoscaler.
                self.autoscaler.update()
                status[
                    "autoscaler_report"] = self.autoscaler.summary()._asdict()

                for msg in self.event_summarizer.summary():
                    logger.info("{}{}".format(
                        ray_constants.LOG_PREFIX_EVENT_SUMMARY, msg))
                self.event_summarizer.clear()

            as_json = json.dumps(status)
            if _internal_kv_initialized():
                _internal_kv_put(
                    DEBUG_AUTOSCALING_STATUS, as_json, overwrite=True)

            # Wait for a autoscaler update interval before processing the next
            # round of messages.
            time.sleep(AUTOSCALER_UPDATE_INTERVAL_S)

    def update_event_summary(self):
        """Report the current size of the cluster.

        To avoid log spam, only cluster size changes (CPU or GPU count change)
        are reported to the event summarizer. The event summarizer will report
        only the latest cluster size per batch.
        """
        avail_resources = self.load_metrics.resources_avail_summary()
        if (not self.readonly_config
                and avail_resources != self.last_avail_resources):
            self.event_summarizer.add(
                "Resized to {}.",  # e.g., Resized to 100 CPUs, 4 GPUs.
                quantity=avail_resources,
                aggregate=lambda old, new: new)
            self.last_avail_resources = avail_resources

    def destroy_autoscaler_workers(self):
        """Cleanup the autoscaler, in case of an exception in the run() method.

        We kill the worker nodes, but retain the head node in order to keep
        logs around, keeping costs minimal. This monitor process runs on the
        head node anyway, so this is more reliable."""

        if self.autoscaler is None:
            return  # Nothing to clean up.

        if self.autoscaling_config is None:
            # This is a logic error in the program. Can't do anything.
            logger.error(
                "Monitor: Cleanup failed due to lack of autoscaler config.")
            return

        logger.info("Monitor: Exception caught. Taking down workers...")
        clean = False
        while not clean:
            try:
                teardown_cluster(
                    config_file=self.autoscaling_config,
                    yes=True,  # Non-interactive.
                    workers_only=True,  # Retain head node for logs.
                    override_cluster_name=None,
                    keep_min_workers=True,  # Retain minimal amount of workers.
                )
                clean = True
                logger.info("Monitor: Workers taken down.")
            except Exception:
                logger.error("Monitor: Cleanup exception. Trying again...")
                time.sleep(2)

    def _handle_failure(self, error):
        logger.exception("Error in monitor loop")
        if self.autoscaler is not None and \
           os.environ.get("RAY_AUTOSCALER_FATESHARE_WORKERS", "") == "1":
            self.autoscaler.kill_workers()
            # Take down autoscaler workers if necessary.
            self.destroy_autoscaler_workers()

        # Something went wrong, so push an error to all current and future
        # drivers.
        message = f"The autoscaler failed with the following error:\n{error}"
        if _internal_kv_initialized():
            _internal_kv_put(DEBUG_AUTOSCALING_ERROR, message, overwrite=True)
        redis_client = ray._private.services.create_redis_client(
            self.redis_address, password=self.redis_password)
        from ray._private.utils import push_error_to_driver_through_redis
        push_error_to_driver_through_redis(
            redis_client, ray_constants.MONITOR_DIED_ERROR, message)

    def _signal_handler(self, sig, frame):
        self._handle_failure(f"Terminated with signal {sig}\n" +
                             "".join(traceback.format_stack(frame)))
        sys.exit(sig + 128)

    def run(self):
        # Register signal handlers for autoscaler termination.
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        try:
            if _internal_kv_initialized():
                # Delete any previous autoscaling errors.
                _internal_kv_del(DEBUG_AUTOSCALING_ERROR)
            self._initialize_autoscaler()
            self._run()
        except Exception:
            self._handle_failure(traceback.format_exc())
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Parse Redis server for the "
                     "monitor to connect to."))
    parser.add_argument(
        "--redis-address",
        required=True,
        type=str,
        help="the address to use for Redis")
    parser.add_argument(
        "--autoscaling-config",
        required=False,
        type=str,
        help="the path to the autoscaling config file")
    parser.add_argument(
        "--redis-password",
        required=False,
        type=str,
        default=None,
        help="the password to use for Redis")
    parser.add_argument(
        "--logging-level",
        required=False,
        type=str,
        default=ray_constants.LOGGER_LEVEL,
        choices=ray_constants.LOGGER_LEVEL_CHOICES,
        help=ray_constants.LOGGER_LEVEL_HELP)
    parser.add_argument(
        "--logging-format",
        required=False,
        type=str,
        default=ray_constants.LOGGER_FORMAT,
        help=ray_constants.LOGGER_FORMAT_HELP)
    parser.add_argument(
        "--logging-filename",
        required=False,
        type=str,
        default=ray_constants.MONITOR_LOG_FILE_NAME,
        help="Specify the name of log file, "
        "log to stdout if set empty, default is "
        f"\"{ray_constants.MONITOR_LOG_FILE_NAME}\"")
    parser.add_argument(
        "--logs-dir",
        required=True,
        type=str,
        help="Specify the path of the temporary directory used by Ray "
        "processes.")
    parser.add_argument(
        "--logging-rotate-bytes",
        required=False,
        type=int,
        default=ray_constants.LOGGING_ROTATE_BYTES,
        help="Specify the max bytes for rotating "
        "log file, default is "
        f"{ray_constants.LOGGING_ROTATE_BYTES} bytes.")
    parser.add_argument(
        "--logging-rotate-backup-count",
        required=False,
        type=int,
        default=ray_constants.LOGGING_ROTATE_BACKUP_COUNT,
        help="Specify the backup count of rotated log file, default is "
        f"{ray_constants.LOGGING_ROTATE_BACKUP_COUNT}.")
    parser.add_argument(
        "--monitor-ip",
        required=False,
        type=str,
        default=None,
        help="The IP address of the machine hosting the monitor process.")
    args = parser.parse_args()
    setup_component_logger(
        logging_level=args.logging_level,
        logging_format=args.logging_format,
        log_dir=args.logs_dir,
        filename=args.logging_filename,
        max_bytes=args.logging_rotate_bytes,
        backup_count=args.logging_rotate_backup_count)

    logger.info(f"Starting monitor using ray installation: {ray.__file__}")
    logger.info(f"Ray version: {ray.__version__}")
    logger.info(f"Ray commit: {ray.__commit__}")
    logger.info(f"Monitor started with command: {sys.argv}")

    if args.autoscaling_config:
        autoscaling_config = os.path.expanduser(args.autoscaling_config)
    else:
        autoscaling_config = None

    monitor = Monitor(
        args.redis_address,
        autoscaling_config,
        redis_password=args.redis_password,
        monitor_ip=args.monitor_ip)

    monitor.run()
