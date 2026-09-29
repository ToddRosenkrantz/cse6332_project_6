# Assignment Framework: Kafka Producer Tuning & Comparison

Compare **kafka-python** and **confluent-kafka** under controlled settings, and find the configuration that gives the best throughput and latency without errors. This is avenue A of [research_projects.md](research_projects.md); use `python setup.py bench A_ingestion` for scripted runs.

See [producers_dual_stack.md](producers_dual_stack.md) for how both producers run side by side.

## A. Side-by-side differences

| Aspect | kafka-python (`--impl kp`) | confluent-kafka (`--impl ck`) |
|---|---|---|
| Send API | `producer.send(topic, value)` returns a future | `producer.produce(topic, value, on_delivery=cb)` |
| IO / callbacks | Background thread, no polling needed | Must call `producer.poll()` regularly |
| Backpressure | Blocks, then raises `KafkaTimeoutError` | Raises `BufferError` when the local queue is full |
| Idempotent produce | Supported (3.x). Off by default here, see pitfalls | `enable.idempotence=true` (default here) |
| Throughput | Pure Python | librdkafka C core |
| Compression here | none, gzip | none, gzip, snappy, lz4, zstd |
| Delivery status | Future result / exceptions | `on_delivery(err, msg)` callback |
| Scaling in this project | One process per topic | `set-prods` changes the thread count live |

## B. Baseline configurations (the defaults you start from)

| Setting | kafka-python default here | confluent-kafka default here | Option to change it |
|---|---|---|---|
| acks | `1` | `all` | `--acks 0\|1\|all` |
| idempotence | `false` | `true` | `--idempotence true\|false` (true requires `acks=all`) |
| linger (ms) | `0` | `10` | `--linger-ms N` |
| batch size (bytes) | `16384` | `131072` | `--batch-size N` |
| compression | `none` | `zstd` | `--compression …` |
| producer threads | 1 | 2 | `--init-producers N` (ck), or `producers set-prods` |
| Poisson λ (msgs/s per thread) | 5 | 5 | `producers set-lambda <topic> <λ>` |
| partitions per topic | 1 | 1 | `KAFKA_PARTITIONS` in `.env`, or `--alter` (see G) |

Set them as `setup.py` options. There are no files to edit:

```
python setup.py producers start --impl ck --acks 1 --idempotence false --compression lz4 --linger-ms 20
python setup.py producers start --impl kp --acks all --linger-ms 20 --batch-size 65536 --compression gzip
python setup.py bench A_ingestion --impl ck --acks 1 --compression lz4 --label ck-lz4
```

The options apply to every producer that command starts; use `--topic` to tune one topic. `python setup.py producers status` shows each producer's options. For scripted, repeatable runs with auto-generated results, use `bench` (see [research_projects.md](research_projects.md), avenue A). `--payload-bytes N` and `--burst` (send as fast as possible) are also available.

## C. Tuning knobs to explore

- **Partitions:** more partitions allow more parallelism in the broker and in Spark.
- **linger / batch size:** larger batches mean fewer requests and higher throughput, at the cost of per-message latency.
- **compression:** fewer bytes on the wire and on disk, at the cost of CPU.
- **acks:** `0` never waits, `1` waits for the leader, `all` waits for all in-sync replicas.
- **idempotence:** no duplicates on retry, but it requires `acks=all`.
- **Message size:** edit `generate_iot_data()` to add a payload field.
- **Producer count:** `set-prods` for ck; for kp, run both topics or both implementations.
- **Keying:** send with a key (e.g. `device_id`) to keep ordering per device across partitions.

## D. What to measure

| Source | Where | Metrics |
|---|---|---|
| Producers | Kafka Producers dashboard | `kafka_producer_message_rate`, `_avg_delay`, `_error_rate`, `_dropped_messages`, `_lambda` |
| Broker | Pipeline Overview, section 2 | `kafka_server_brokertopicmetrics_messagesinpersec_total`, bytes in/out (JMX exporter) |
| Topic offsets | Kafka Exporter Overview | `kafka_topic_partition_current_offset` |
| Consumers | Pipeline Overview, section 3 | Spark input vs processing rate, micro-batch latency |
| Resources | Containers (cAdvisor) | CPU and memory of `kafka` and the Spark containers |

Spark Structured Streaming tracks its offsets in its checkpoint, not in a Kafka consumer group, so "consumer lag" appears as **input rate > processing rate** and rising **micro-batch latency**.

Raw numbers are also written by the producers to `logs/<topic>_run<N>_<timestamp>.csv` (kafka-python) and to `logs/<impl>_<topic>.out`.

## E. Grafana panels to use or build

- Per-producer rate: `kafka_producer_message_rate{topic="topic-json"}`, legend `{{impl}} {{client_id}}`
- Aggregate per topic and implementation: `sum by (topic, impl) (kafka_producer_message_rate)`
- Errors: `kafka_producer_error_rate`
- Broker perspective: `sum by (topic) (rate(kafka_server_brokertopicmetrics_messagesinpersec_total{topic=~"topic-.*"}[1m]))`

## F. Experiment design

1. **Fix the environment:** same machine, the same Docker CPU and RAM allocation, and nothing else heavy running. Record `python setup.py doctor`.
2. **Choose a small matrix:** e.g. {kp, ck} × acks {1, all} × linger {0, 20} × compression {none, gzip/zstd}.
3. **Warm up and measure:** run each configuration for 1 minute of warm-up, then 3 minutes of measurement at a fixed λ, or use a ramp to find the saturation point.
4. **Isolate each run:** `python setup.py producers stop` between runs, and run only the implementation under test.
5. **Record and plot** the numbers from Grafana (panel → Inspect → Data → CSV) or from the producer CSV logs.

## G. Run commands

```
# one configuration, one implementation, one topic
python setup.py producers start --impl ck --topic topic-json --acks 1 --idempotence false --compression lz4 --linger-ms 20
python setup.py producers set-lambda topic-json 50
python setup.py producers set-prods  topic-json 4
# ... measure ...
python setup.py producers stop

# find the saturation point: lambda 10 → 200 in steps of 10, 60 s each
python setup.py producers start --impl kp --topic topic-parq
python setup.py producers ramp topic-parq --from 10 --to 200 --step 10 --interval 60
python setup.py producers stop            # also stops the ramp

# more partitions on an existing topic (partitions can only be increased)
docker exec -e KAFKA_OPTS= kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:29092 --alter --topic topic-json --partitions 4
# or, for new installs, set KAFKA_PARTITIONS=4 in .env before `python setup.py install`

# check the pipeline end to end
python setup.py monitor
```

## H. Reporting template

- **Environment:** OS, CPU/RAM given to Docker, output of `python setup.py doctor`
- **Topic config:** partitions, replication factor (1)
- **Workload:** λ, producer threads, message size, run length
- **Tuning:** acks, idempotence, linger, batch size, compression, per implementation
- **Results:** a table of msgs/s, average delay, errors and broker bytes in for each configuration, plus graphs
- **Conclusion:** which settings won, why, and the trade-offs you observed

## I. Suggested starting "best settings"

- **confluent-kafka:** `--linger-ms 10..20 --batch-size 131072..262144 --compression zstd`, with `--acks 1 --idempotence false` for raw throughput or `--acks all` (idempotent) for safety.
- **kafka-python:** similar linger and batch settings with `--compression gzip`. Expect lower peak throughput than librdkafka.

## J. Common pitfalls

- **One partition:** throughput is capped by a single partition and a single Spark task.
- **Forgetting `poll()`** in confluent-kafka: delivery callbacks never fire and the queue fills up (the provided producer polls).
- **Linger set too high:** throughput looks fine but per-message latency balloons.
- **`acks=all` on a single broker** gives no extra durability here (replication factor 1), only extra latency.
- **kafka-python idempotence:** with `--idempotence true`, the first send waits about 30 s while the broker assigns a producer ID. That's why it's off by default. Exclude the warm-up period from measurements if you enable it.
- **Windows:** keep `KAFKA_BOOTSTRAP_HOST=127.0.0.1:9092`, since `localhost` may resolve to IPv6 and time out.
