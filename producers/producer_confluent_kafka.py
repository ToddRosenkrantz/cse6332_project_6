#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kafka Producer (confluent-kafka/librdkafka) with live tuning and Prometheus metrics.

FEATURES
- One process per topic; one Prometheus HTTP port per process/topic.
- Dynamic scaling of simulated publishers (producer threads) via producers_<TOPIC>.txt.
- Dynamic control of Poisson λ via lambda_<TOPIC>.txt.
- Idempotence toggle (--idempotence true|false). If true, we auto-enforce acks=all.
- Per-producer Prometheus gauges labeled by {topic, client_id} so Grafana can split lines.

Started/stopped by `python setup.py producers start|stop --impl ck`, which
sends SIGTERM (Linux/macOS) or CTRL_BREAK_EVENT (Windows); the handler below
stops every producer thread and flushes before exiting.

USAGE
  python3 producer_confluent_kafka.py <topic> <prom_port>
  python3 producer_confluent_kafka.py topic-json 9108
  python3 producer_confluent_kafka.py topic-parq 9109 --bootstrap localhost:9092 --init-producers 3

IMPORTANT FILES (auto-created on first run if missing)
  lambda_<TOPIC>.txt       # float; Poisson λ (messages spaced by Exp(λ))
  producers_<TOPIC>.txt    # int; number of producer threads to run

EXAMPLES
  # Start process per topic
  python3 producer_confluent_kafka.py topic-json 9108 --init-producers 2
  python3 producer_confluent_kafka.py topic-parq 9109 --init-producers 3

  # Scale producers live (no restart)
  echo 8  > producers_topic-json.txt
  echo 12 > lambda_topic-json.txt

  # Idempotence & acks behavior:
  # - Default: idempotence=true, acks=all
  # - If user passes --idempotence true and --acks 1, we warn and force acks=all to avoid crash.
  # - If user wants acks=1, run with --idempotence false.

REQUIREMENTS (requirements.txt)
  confluent-kafka
  prometheus-client

GRAFANA HINTS
  - Query producer rate per client:
      kafka_producer_message_rate{topic="topic-json"}
    Legend: {{client_id}}
  - Aggregate per topic:
      sum by (topic) (kafka_producer_message_rate)
"""

import argparse
import itertools
import json
import logging
import os
import random
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from confluent_kafka import Producer
from prometheus_client import start_http_server, Counter, Gauge


# --------------------------- Defaults / constants ---------------------------

DEFAULT_LAMBDA = 5.0       # Poisson rate parameter (per thread)
LOG_INTERVAL = 10.0        # seconds between metric/log updates

# ------------------------------- CLI parsing --------------------------------

parser = argparse.ArgumentParser(description="Confluent-Kafka producer with live scaling and Prometheus metrics.")
parser.add_argument("topic", help="Kafka topic name")
parser.add_argument("prom_port", type=int, help="Prometheus metrics HTTP port (one per topic/process)")

parser.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP_HOST", "127.0.0.1:9092"), help="Kafka bootstrap servers (host:port[,host:port])")
parser.add_argument("--acks", default="all", help="Producer acks (all|1|0). With idempotence=true, acks must be 'all'.")
parser.add_argument("--compression", default="zstd", help="Compression: none|gzip|snappy|lz4|zstd")
parser.add_argument("--linger-ms", type=int, default=10, help="linger.ms batching delay (default 10)")
parser.add_argument("--batch-size", type=int, default=131072, help="batch.size in bytes (default 131072)")
parser.add_argument("--payload-bytes", type=int, default=0,
                    help="Extra filler bytes per message, to study message size (default 0)")
parser.add_argument("--burst", action="store_true",
                    help="Send as fast as possible (ignore lambda) to find the throughput ceiling")
parser.add_argument("--init-producers", type=int, default=2, help="Initial number of producer threads if control file missing")

# Idempotence toggle: student-proof with auto-fix for acks
parser.add_argument("--idempotence", choices=["true", "false"], default="true",
                    help="Enable idempotent producer (dedupe on retries). Requires acks=all when true.")

args = parser.parse_args()

TOPIC = args.topic
PROM_PORT = args.prom_port
BOOTSTRAP = args.bootstrap
ACKS = args.acks.lower()
COMPRESSION = "none" if args.compression.lower() == "none" else args.compression.lower()
ENABLE_IDEMPOTENCE = (args.idempotence.lower() == "true")

# Auto-fix acks if idempotence is on
if ENABLE_IDEMPOTENCE and ACKS != "all":
    logging.warning("`--idempotence true` requires `--acks all`; overriding acks=%s -> all", ACKS)
    ACKS = "all"

# ---------------------------- Control file paths ----------------------------

lambda_file = Path(f"lambda_{TOPIC}.txt")
producers_file = Path(f"producers_{TOPIC}.txt")

if not lambda_file.exists():
    lambda_file.write_text(str(DEFAULT_LAMBDA))
if not producers_file.exists():
    producers_file.write_text(str(args.init_producers))

# ----------------------------- Prometheus setup -----------------------------

start_http_server(PROM_PORT)

lambda_g = Gauge("kafka_producer_lambda", "Current Poisson lambda", ["topic"])
rate_g   = Gauge("kafka_producer_message_rate", "Msgs/sec over window", ["topic", "client_id"])
drop_g   = Gauge("kafka_producer_dropped_messages", "Dropped messages (window)", ["topic", "client_id"])
err_g    = Gauge("kafka_producer_error_rate", "Errors/sec over window", ["topic", "client_id"])
avgd_g   = Gauge("kafka_producer_avg_delay", "Average message delay (s) over window", ["topic", "client_id"])

# Cumulative counters (exported as *_total) used for data accounting
sent_c   = Counter("kafka_producer_sent", "Messages acknowledged by the broker", ["topic"])
failed_c = Counter("kafka_producer_failed", "Messages the broker did not acknowledge", ["topic"])

lambda_g.labels(topic=TOPIC).set(DEFAULT_LAMBDA)
PAYLOAD = "x" * args.payload_bytes if args.payload_bytes > 0 else None
RUN_PREFIX = f"ck-{TOPIC}-{os.getpid()}-{int(time.time())}"

# ---------------------------- Shared runtime state --------------------------

lambda_lock = threading.Lock()
current_lambda = [DEFAULT_LAMBDA]

stop_all = threading.Event()
producer_registry_lock = threading.Lock()
producer_threads = {}              # client_id -> (thread, stop_event)
id_counter = itertools.count(1)    # unique suffixes for client_id

def pause(seconds, stop_event):
    """Wait between messages. Short gaps use time.sleep: on Windows, Event.wait rounds up to the
    ~15.6 ms timer tick (capping a thread near 64 msgs/s) while time.sleep is precise (Python 3.11+)."""
    if seconds < 0.2:
        time.sleep(seconds)
    else:
        stop_event.wait(seconds)

# ------------------------------ Data generator ------------------------------

def generate_iot_data(msg_id):
    """Example payload; adjust for your workload."""
    data = {
        "msg_id": msg_id,
        "device_id": f"iot_device_{random.randint(1, 50)}",
        "battery_level": random.randint(20, 100),
        "motion_detected": random.choice([True, False]),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if PAYLOAD:
        data["payload"] = PAYLOAD
    return data

# ----------------------------- Producer worker ------------------------------

def run_producer(client_id: str, stop_event: threading.Event):
    """
    Producer thread: emits messages spaced by Exp(lambda), reports metrics per window,
    and flushes on shutdown. Uses confluent-kafka/librdkafka.
    """
    conf = {
        "bootstrap.servers": BOOTSTRAP,
        "client.id": client_id,
        "enable.idempotence": ENABLE_IDEMPOTENCE,
        "acks": ACKS,
        "compression.type": COMPRESSION,
        "linger.ms": args.linger_ms,     # increase for bigger batches / more throughput
        "batch.size": args.batch_size,   # default ~128 KiB
        "message.timeout.ms": 60000,     # total delivery timeout
    }

    # Recommended when idempotent to keep ordering with retries:
    if ENABLE_IDEMPOTENCE:
        # librdkafka sets good defaults, but you can uncomment to be explicit:
        # conf["retries"] = 1000000
        # conf["max.in.flight.requests.per.connection"] = 5
        pass

    producer = Producer(conf)

    msg_count = err_count = drop_count = 0
    total_delay = 0.0
    window_start = time.time()
    abs_start = window_start

    def on_delivery(err, msg):
        nonlocal err_count
        if err is not None:
            err_count += 1
            failed_c.labels(TOPIC).inc()
        else:
            sent_c.labels(TOPIC).inc()

    logging.info(f"[{client_id}] started (bootstrap={BOOTSTRAP}, acks={ACKS}, idempotence={ENABLE_IDEMPOTENCE}, "
                 f"compression={COMPRESSION}, linger.ms={args.linger_ms}, batch.size={args.batch_size}, "
                 f"payload_bytes={args.payload_bytes}, burst={args.burst})")
    seq = 0

    try:
        while not stop_event.is_set():
            seq += 1
            payload = generate_iot_data(f"{RUN_PREFIX}-{client_id}-{seq}")
            try:
                producer.produce(TOPIC, value=json.dumps(payload), key=None, on_delivery=on_delivery)
            except BufferError:
                # Queue full: allow producer to make progress
                producer.poll(0.1)
                continue

            msg_count += 1
            # Drive IO & callbacks
            producer.poll(0)

            # Pacing via Exp(lambda)
            with lambda_lock:
                _lambda = current_lambda[0]
            if args.burst:
                delay = 0.0
            else:
                delay = random.expovariate(_lambda) if _lambda > 0 else 1.0
            total_delay += delay

            now = time.time()
            if now - window_start >= LOG_INTERVAL:
                elapsed = now - window_start
                rate = (msg_count / elapsed) if elapsed > 0 else 0.0
                avg_delay = (total_delay / msg_count) if msg_count else 0.0
                err_rate = (err_count / elapsed) if elapsed > 0 else 0.0

                logging.info(
                    f"[{client_id}] {msg_count} msgs / {elapsed:.1f}s  "
                    f"rate={rate:.1f}/s  errors={err_count}  dropped={drop_count}  avg_delay={avg_delay:.3f}s"
                )

                # Export window metrics per client_id
                rate_g.labels(TOPIC, client_id).set(rate)
                drop_g.labels(TOPIC, client_id).set(drop_count)
                err_g.labels(TOPIC, client_id).set(err_rate)
                avgd_g.labels(TOPIC, client_id).set(avg_delay)

                # reset window
                msg_count = err_count = drop_count = 0
                total_delay = 0.0
                window_start = now

            if delay > 0:
                pause(delay, stop_event)

    finally:
        # Drain delivery reports & flush
        for _ in range(10):
            producer.poll(0.1)
        producer.flush(5)
        # Optional: zero-out gauges so panels don't look stale
        rate_g.labels(TOPIC, client_id).set(0)
        err_g.labels(TOPIC, client_id).set(0)
        avgd_g.labels(TOPIC, client_id).set(0)
        logging.info(f"[{client_id}] stopped")

# ------------------------------- File watchers ------------------------------

def lambda_watcher():
    """Continuously read lambda_<TOPIC>.txt and update current_lambda + gauge."""
    while not stop_all.is_set():
        try:
            val = float(lambda_file.read_text().strip())
            with lambda_lock:
                current_lambda[0] = max(0.0, val)
            lambda_g.labels(topic=TOPIC).set(current_lambda[0])
        except Exception:
            # Ignore parse or read errors this cycle
            pass
        stop_all.wait(2)

def producers_watcher():
    """
    Scale producer threads to match producers_<TOPIC>.txt.
    Create new threads as needed; stop extras by signaling their stop_event.
    """
    current = 0
    while not stop_all.is_set():
        try:
            target = int(producers_file.read_text().strip())
            if target < 0:
                target = 0
        except Exception:
            target = current  # keep as-is on parse error

        # scale out
        while current < target:
            cid = f"{TOPIC}-p{next(id_counter)}"
            ev = threading.Event()
            th = threading.Thread(target=run_producer, args=(cid, ev), daemon=True)
            with producer_registry_lock:
                producer_threads[cid] = (th, ev)
            th.start()
            current += 1

        # scale in
        while current > target:
            with producer_registry_lock:
                # pop arbitrary one to stop
                cid, (th, ev) = producer_threads.popitem()
            ev.set()
            current -= 1

        stop_all.wait(2)

# --------------------------------- Main -------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
    # Initialize lambda gauge with current file value if present
    try:
        val = float(lambda_file.read_text().strip())
        with lambda_lock:
            current_lambda[0] = max(0.0, val)
        lambda_g.labels(topic=TOPIC).set(current_lambda[0])
    except Exception:
        pass

    threading.Thread(target=lambda_watcher, daemon=True).start()
    threading.Thread(target=producers_watcher, daemon=True).start()

    logging.info(f"[main] topic={TOPIC} prom_port={PROM_PORT} bootstrap={BOOTSTRAP} "
                 f"idempotence={ENABLE_IDEMPOTENCE} acks={ACKS} compression={COMPRESSION}")

    def request_stop(signum, frame):
        logging.info(f"[main] received signal {signum}; stopping all producer threads")
        stop_all.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):  # Windows: CTRL_BREAK_EVENT
        signal.signal(signal.SIGBREAK, request_stop)

    # Short waits keep the main thread responsive to signals on every OS
    while not stop_all.is_set():
        stop_all.wait(1)

    with producer_registry_lock:
        threads = list(producer_threads.values())
        for _, ev in threads:
            ev.set()
    # Each thread flushes in its finally block
    for th, _ in threads:
        th.join(timeout=10)
    logging.info(f"[main] {TOPIC}: all producer threads flushed/closed")

if __name__ == "__main__":
    main()
