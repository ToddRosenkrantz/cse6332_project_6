# Running Both Kafka Producer Types in the Same Stack

This project ships two producers that write the same IoT JSON events to the same topics:

| Implementation | Script | `--impl` | Metrics ports (topic-json / topic-parq) | Prometheus label |
|---|---|---|---|---|
| kafka-python | `producers/poisson_kafka_producer.py` | `kp` | 9108 / 9109 | `impl="kp"` |
| confluent-kafka (librdkafka) | `producers/producer_confluent_kafka.py` | `ck` | 9118 / 9119 | `impl="ckafka"` |

Both run on the **host** and are scraped by Prometheus in Docker. They're already wired up, so no Prometheus or Grafana edits are needed.

---

## 1. Running them

```
python setup.py producers start                          # all four: kp + ck, both topics
python setup.py producers start --impl kp                # kafka-python only
python setup.py producers start --impl ck --topic topic-json
python setup.py producers status
python setup.py producers stop                           # setup.py signals each one to flush and exit
```

Live controls, re-read by the producers every 2 seconds:

```
python setup.py producers set-lambda topic-json 20       # Poisson rate (msgs/s per producer thread)
python setup.py producers set-prods  topic-json 4        # confluent-kafka thread count
python setup.py producers ramp topic-json --from 5 --to 50 --step 5 --interval 60
```

Both implementations read the same `lambda_<topic>.txt`, so a lambda change applies to whichever producers are running on that topic.

---

## 2. How Prometheus scrapes them (already configured)

`docker-compose.yaml` gives Prometheus a route to the host:

```yaml
  prometheus:
    extra_hosts:
      - "host.docker.internal:host-gateway"   # needed on native Linux; Docker Desktop resolves it already
```

`prometheus.yml` has one job per producer, labelled by implementation:

```yaml
  - job_name: 'kafka-producer-json'          # kafka-python
    static_configs:
      - targets: ['host.docker.internal:9108']
        labels: {impl: 'kp'}
  - job_name: 'kafka-producer-json-ckafka'   # confluent-kafka
    static_configs:
      - targets: ['host.docker.internal:9118']
        labels: {impl: 'ckafka'}
  # ...and the same pair for topic-parq on 9109 / 9119
```

After editing `prometheus.yml`, apply it with `python setup.py restart prometheus`.

`python setup.py status` reports producer targets that aren't running as **"producer not started"** rather than as failures.

---

## 3. Grafana

Two provisioned dashboards already split the producers by implementation:

- **Kafka Producers (custom):** λ, message rate, average delay, dropped messages and error rate. Legends read `topic impl client_id`, where `client_id` distinguishes confluent-kafka threads.
- **Pipeline Overview → 1. Producers:** `sum by (topic, impl) (kafka_producer_message_rate)`.

Useful queries for your own panels:

```
kafka_producer_message_rate{topic="topic-json", impl=~"kp|ckafka"}      # per producer
sum by (topic, impl) (kafka_producer_message_rate)                     # per topic and implementation
sum by (impl) (kafka_producer_message_rate{topic="topic-json"})        # compare implementations
kafka_producer_error_rate
```

To filter interactively, add dashboard variables (Settings → Variables):

- `topic`: `label_values(kafka_producer_message_rate, topic)`
- `impl`: `label_values(kafka_producer_message_rate, impl)`

Then use `{topic="$topic", impl=~"$impl"}` in the queries. Save UI changes back to the repo with `python setup.py grafana-export`.

---

## 4. Checklist

- `python setup.py producers status` shows the producers you expect as running.
- `python setup.py status` shows their Prometheus targets as `up`.
- The Kafka Producers dashboard shows separate `kp` and `ckafka` series.
- The broker sees the combined load: **Pipeline Overview → 2. Kafka broker → Messages in per second**.

## Why this matters

Because every series carries an `impl` label, you can run kafka-python only, confluent-kafka only, or both at once, and compare them side by side on the same broker, topics and consumers. That comparison is the basis for the tuning exercise in [producer_tuning.md](producer_tuning.md).
