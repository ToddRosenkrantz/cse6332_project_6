"""
Poisson Kafka producer (kafka-python) with Prometheus metrics.

Started/stopped by `python setup.py producers start|stop --impl kp`, which
sends SIGTERM (Linux/macOS) or CTRL_BREAK_EVENT (Windows) to stop it; the
handler below flushes and closes the producer before exiting.

Direct use:
    python producers/poisson_kafka_producer.py topic-json 9108
    python producers/poisson_kafka_producer.py topic-parq 9109 --bootstrap localhost:9092

Live control (read every 2 s):
    lambda_<topic>.txt   float; Poisson lambda (msgs/sec)
"""
import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from kafka import KafkaProducer
from kafka.errors import KafkaError
from prometheus_client import start_http_server, Counter, Gauge

# === Defaults ===
DEFAULT_BROKER = os.environ.get("KAFKA_BOOTSTRAP_HOST", "127.0.0.1:9092")
DEFAULT_LAMBDA = 5
LOG_INTERVAL = 10  # seconds

parser = argparse.ArgumentParser(description="Poisson Kafka producer (kafka-python)")
parser.add_argument("topic", help="Kafka topic, e.g. topic-json or topic-parq")
parser.add_argument("prom_port", type=int, help="Prometheus metrics port, e.g. 9108")
parser.add_argument("--bootstrap", default=DEFAULT_BROKER, help="Kafka bootstrap servers")
# Tuning knobs (see docs/producer_tuning.md)
parser.add_argument("--acks", default="1", choices=["0", "1", "all"], help="Producer acks (default 1)")
parser.add_argument("--linger-ms", type=int, default=0, help="Batching delay in ms (default 0)")
parser.add_argument("--batch-size", type=int, default=16384, help="Max batch size in bytes (default 16384)")
parser.add_argument("--idempotence", default="false", choices=["true", "false"],
                    help="Idempotent producer (default false; kafka-python 3.x defaults it on, which adds a "
                         "~30 s producer-id handshake at startup on this single-broker stack). Requires --acks all")
parser.add_argument("--compression", default="none", choices=["none", "gzip"],
                    help="Compression (default none; gzip needs no extra packages)")
parser.add_argument("--payload-bytes", type=int, default=0,
                    help="Extra filler bytes per message, to study message size (default 0)")
parser.add_argument("--burst", action="store_true",
                    help="Send as fast as possible (ignore lambda) to find the throughput ceiling")
args = parser.parse_args()

TOPIC = args.topic
PROM_PORT = args.prom_port

now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
run_id = 1
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

while True:
    log_file_candidate = log_dir / f"{TOPIC}_run{run_id}_{now_str}.csv"
    if not log_file_candidate.exists():
        break
    run_id += 1

LOG_FILE = str(log_file_candidate)

# === Prometheus Setup ===
start_http_server(PROM_PORT)

# Shared metrics with labels
lambda_gauge = Gauge('kafka_producer_lambda', 'Current Poisson lambda', ['topic'])
message_rate_gauge = Gauge('kafka_producer_message_rate', 'Messages per second', ['topic'])
dropped_messages_gauge = Gauge('kafka_producer_dropped_messages', 'Number of dropped messages', ['topic'])
error_rate_gauge = Gauge('kafka_producer_error_rate', 'Errors per second', ['topic'])
average_delay_gauge = Gauge('kafka_producer_avg_delay', 'Average message delay (s)', ['topic'])
# Cumulative counters (exported as *_total) used for data accounting
sent_counter = Counter('kafka_producer_sent', 'Messages acknowledged by the broker', ['topic'])
failed_counter = Counter('kafka_producer_failed', 'Messages the broker did not acknowledge', ['topic'])

lambda_gauge.labels(topic=TOPIC).set(DEFAULT_LAMBDA)

# === Logging Setup ===
LOG_TEXT_FILE = log_dir / f"{TOPIC}_run{run_id}_{now_str}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_TEXT_FILE, mode='w')
    ]
)

sys.excepthook = lambda t, v, tb: logging.error(f"Unhandled exception: {v}")

# === Stop signal (sent by setup.py) ===
stop_event = threading.Event()


def pause(seconds):
    """Wait between messages. Short gaps use time.sleep: on Windows, Event.wait rounds up to the
    ~15.6 ms timer tick (capping a sender near 64 msgs/s) while time.sleep is precise (Python 3.11+)."""
    if seconds < 0.2:
        time.sleep(seconds)
    else:
        stop_event.wait(seconds)


def request_stop(signum, frame):
    logging.info(f"Received signal {signum}; stopping producer for '{TOPIC}'")
    stop_event.set()


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
if hasattr(signal, "SIGBREAK"):  # Windows: CTRL_BREAK_EVENT
    signal.signal(signal.SIGBREAK, request_stop)

# === Kafka Setup ===
producer = KafkaProducer(
    bootstrap_servers=args.bootstrap,
    acks=args.acks if args.acks == "all" else int(args.acks),
    enable_idempotence=args.idempotence == "true",
    linger_ms=args.linger_ms,
    batch_size=args.batch_size,
    compression_type=None if args.compression == "none" else args.compression,
    retries=5,
    retry_backoff_ms=30000,
    max_block_ms=60000,
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

msg_count = 0
error_count = 0
dropped_count = 0
total_delay = 0.0
start_time = time.time()
absolute_start = start_time
total_sent = 0

# Write CSV header
with open(LOG_FILE, 'w') as f:
    f.write("elapsed_seconds,rate,msg_count,error_count,dropped_count,avg_delay\n")

logging.info(f"Starting Poisson message producer to topic '{TOPIC}' on port {PROM_PORT} (bootstrap={args.bootstrap}, "
             f"acks={args.acks}, idempotence={args.idempotence}, linger_ms={args.linger_ms}, batch_size={args.batch_size}, "
             f"compression={args.compression}, payload_bytes={args.payload_bytes}, burst={args.burst})")

# Unique id per message (run prefix + sequence) so duplicates can be detected downstream
MSG_PREFIX = f"kp-{TOPIC}-{os.getpid()}-{int(time.time())}"
PAYLOAD = "x" * args.payload_bytes if args.payload_bytes > 0 else None
msg_seq = 0


def on_delivered(_metadata):
    sent_counter.labels(topic=TOPIC).inc()


def on_failed(exc):
    failed_counter.labels(topic=TOPIC).inc()
    logging.debug(f"Delivery failed: {exc}")


# === IoT Data Generator ===
def generate_iot_data():
    global msg_seq
    msg_seq += 1
    data = {
        "msg_id": f"{MSG_PREFIX}-{msg_seq}",
        "device_id": f"iot_device_{random.randint(1, 50)}",
        "battery_level": random.randint(20, 100),
        "motion_detected": random.choice([True, False]),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }
    if PAYLOAD:
        data["payload"] = PAYLOAD
    return data


# === Background thread to monitor lambda_<topic>.txt ===
lambda_lock = threading.Lock()
current_lambda = [DEFAULT_LAMBDA]


def lambda_watcher():
    while not stop_event.is_set():
        try:
            with open(f"lambda_{TOPIC}.txt", "r") as f:
                value = float(f.read().strip())
                with lambda_lock:
                    current_lambda[0] = value
                    lambda_gauge.labels(topic=TOPIC).set(value)
        except Exception as e:
            logging.debug(f"Lambda watcher error: {e}")
        stop_event.wait(2)


threading.Thread(target=lambda_watcher, daemon=True).start()

try:
    while not stop_event.is_set():
        message = generate_iot_data()
        try:
            producer.send(TOPIC, value=message).add_callback(on_delivered).add_errback(on_failed)
            total_sent += 1
        except KafkaError as e:
            logging.error(f"Failed to send message: {e}")
            error_count += 1
            dropped_count += 1

        msg_count += 1

        now = time.time()
        with lambda_lock:
            lam = current_lambda[0]
        if args.burst:
            delay = 0.0
        else:
            delay = random.expovariate(lam) if lam > 0 else 1.0
        total_delay += delay

        if now - start_time >= LOG_INTERVAL:
            elapsed = now - start_time
            total_elapsed = now - absolute_start
            rate = msg_count / elapsed
            avg_delay = total_delay / msg_count if msg_count else 0
            error_rate = error_count / elapsed if elapsed > 0 else 0

            logging.info(f"Sent {msg_count} messages in {elapsed:.2f}s ({rate:.2f} msg/sec), errors={error_count}, dropped={dropped_count}, avg_delay={avg_delay:.3f}s")

            message_rate_gauge.labels(topic=TOPIC).set(rate)
            dropped_messages_gauge.labels(topic=TOPIC).set(dropped_count)
            error_rate_gauge.labels(topic=TOPIC).set(error_rate)
            average_delay_gauge.labels(topic=TOPIC).set(avg_delay)

            with open(LOG_FILE, 'a') as f:
                f.write(f"{total_elapsed:.2f},{rate:.2f},{msg_count},{error_count},{dropped_count},{avg_delay:.4f}\n")

            msg_count = 0
            error_count = 0
            dropped_count = 0
            total_delay = 0.0
            start_time = now

        # wait() returns early when a stop signal arrives
        if delay > 0:
            pause(delay)

finally:
    producer.flush(timeout=10)
    producer.close(timeout=10)
    message_rate_gauge.labels(topic=TOPIC).set(0)
    logging.info(f"Producer for '{TOPIC}' flushed/closed after {total_sent} messages.")
