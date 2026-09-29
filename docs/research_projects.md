# Performance Research Projects

You have a working streaming pipeline: **producers → Kafka → Spark Structured Streaming → S3 (SeaweedFS)**, monitored by Prometheus and Grafana. Your project is to act as the engineer who must find its **performance limits** and **optimize** it, the same work done before sizing a real cloud deployment:

| This stack | Cloud equivalent |
|---|---|
| Kafka | Amazon MSK, Kinesis, Confluent Cloud, Azure Event Hubs |
| Spark standalone cluster | EMR, Dataproc, Databricks, Spark on Kubernetes |
| SeaweedFS (S3 API) | Amazon S3, Google Cloud Storage, Azure Blob |
| Prometheus + Grafana + cAdvisor | CloudWatch, Cloud Monitoring, Datadog |

**Pick one research avenue (A–E) and change one setting**, comparing it against a run with the default settings. The goal is to practice doing research and reporting it the way professionals do, as an **executive summary** and an **abstract** (see [What to submit](#what-to-submit)). Every experiment is run with `python setup.py …` commands. You never edit configuration files, and each run writes its own results folder.

## How the project works

1. **Install** (about 15 minutes):
   ```
   python setup.py doctor
   python setup.py install
   ```
2. **Write down your question and hypothesis,** before you run anything: what do you expect your one setting to change, and why?
3. **Run your avenue's scenario twice:** once with the defaults, once with your one setting changed. Then restore the defaults.
   ```
   python setup.py bench C_small_files --label default
   python setup.py bench C_small_files --trigger 30s --label changed
   python setup.py config --reset
   ```
   Use `--quick` (30-second steps) only for a trial run; report results from full-length runs.
4. **Compare the two `report.md` files** and write your executive summary and abstract.

Optional: `python setup.py bench baseline` is a 5-minute end-to-end check of your machine.

Each `bench` run produces `results/<date>_<scenario>_<label>/`:

| File | Contents |
|---|---|
| `report.md` | Configuration, per-step results table, sustainable throughput, latency percentiles, data accounting, resource usage, Grafana link |
| `steps.csv` | Every number, one row per step (open in Excel or pandas for your charts) |
| `config.json` | The exact settings, machine specs and project version, for reproducibility |
| `audit.json` | Row counts, duplicates, file sizes and latency per step |
| `events.json` | Injected faults and recovery times (avenue E) |

Grafana shows each step and fault as a marker on the dashboards. `report.md` includes a link to the exact time range of the run.

## Commands you'll use

| Command | What it does |
|---|---|
| `python setup.py bench --list` | List the scenarios |
| `python setup.py bench <scenario> [options]` | Run an experiment (options below) |
| `python setup.py config` / `config --reset` | Show all current settings / restore the defaults |
| `python setup.py spark --trigger 10s --max-offsets 5000 --cores 2 --worker-cores 4 --worker-cpus 1` | Change Spark (saved; consumers restart automatically) |
| `python setup.py topics --partitions 4` | Add Kafka partitions (can only increase) |
| `python setup.py producers start --impl ck --rate 50 --threads 4 --acks 1 --compression lz4` | Run producers by hand |
| `python setup.py audit` | Count what reached S3 vs Kafka (exactly-once check), file sizes and latency |
| `python setup.py chaos kill-consumer json` | Inject a fault and measure recovery (avenue E) |
| `python setup.py compact --sink parquet` | Merge one hour of small files and compare (avenue C) |

**Options for `bench`** (all optional; the scenario provides defaults):

- **Steps:** `--steps 25,50,100` · `--vary rate|threads` · `--step-seconds 60` · `--warmup 20` · `--quick` · `--label cores2` (names the results folder)
- **Producers:** `--impl kp|ck|both` · `--rate λ` · `--threads N` · `--acks 0|1|all` · `--idempotence true|false` · `--linger-ms` · `--batch-size` · `--compression` · `--payload-bytes N` · `--burst`
- **Spark and Kafka:** `--trigger` · `--max-offsets` · `--cores` · `--worker-cores` · `--worker-cpus` · `--worker-memory` · `--executor-memory` · `--partitions`. These are saved like `setup.py spark`, so use `config --reset` to undo them.

## Reading the results

- **Offered load** = λ × producer threads, per topic. **Produced/s** is what the brokers acknowledged. Offered > produced means the *producer* is the bottleneck.
- **Sustained ✅:** the sink kept up during the step. Its Kafka backlog (messages not yet written to S3) didn't grow and stayed small, or was shrinking. The **sustainable throughput** is the highest rate that was still sustained.
- **Batch ms:** how long each Spark micro-batch took. Rising batch time at a constant rate means the sink is struggling.
- **Latency** is measured per message from the producer's event timestamp:
  - *queue* = until Spark's micro-batch started
  - *end-to-end* = until the micro-batch was committed to S3
  Reported as p50/p95/p99. The container-vs-host clock offset is shown too, usually a few ms.
- **Data accounting PASS** = every message in Kafka is in S3 exactly once (no loss, no duplicates).
- **Resources:** CPU cores and memory per container. The component near its limit is your **bottleneck**. (On Docker Desktop, cAdvisor sees containers by ID, and `bench` maps them to names for you.)

---

**The avenues below list several possible settings and example runs, as ideas. Your assignment needs only one setting, compared against the default run.** The discussion questions help you interpret your results and write your abstract.

## A. Ingestion throughput and producer tuning

**Question:** Where does ingestion saturate, and which producer settings move that ceiling?

**Why it matters:** Ingestion clients are the first bottleneck in most streaming systems. Batching, compression and acknowledgment settings trade throughput against latency and durability. The same settings exist for MSK and Kinesis producers.

**Scenarios:**
- `A_ingestion`: Poisson load stepped up.
- `A_ingestion_burst`: maximum throughput vs number of producer threads.

**Independent variables:**
- Client library: `--impl kp` (kafka-python, pure Python) vs `--impl ck` (confluent-kafka, C library)
- `--threads`, `--acks 0|1|all`, `--linger-ms`, `--batch-size`, `--compression`, `--payload-bytes`

**Measure:**
- Produced/s vs offered
- Producer failures
- Broker msgs/s and bytes/s
- Kafka and host CPU

**Example runs:**
```
python setup.py bench A_ingestion --impl ck --label ck
python setup.py bench A_ingestion --impl kp --label kp
python setup.py bench A_ingestion_burst --label burst-ck
python setup.py bench A_ingestion_burst --acks all --idempotence true --label burst-acks-all
python setup.py bench A_ingestion --compression lz4 --payload-bytes 1000 --label lz4-1k
```

**Discussion:**
- Why does throughput stop scaling with more producer threads? (Hint: look at how Python runs threads.)
- What do `acks=all` and idempotence cost here, and what do they buy on a real multi-broker cluster?
- How does message size change messages/s vs bytes/s?

---

## B. Stream-processing scalability and backpressure

**Question:** What input rate can each Spark sink sustain, and how do cores, partitions and triggers change it?

**Why it matters:** This is the core cluster-sizing question: how many executors and partitions do we need for a given rate, and what happens when we're under-provisioned (backpressure, growing lag)?

**Scenario:** `B_spark_scaling`, with the rate stepped until a sink falls behind.

**Independent variables:**
- `--worker-cpus` (the "instance size"; 0 = unlimited)
- `--cores` per app and `--worker-cores`
- `--partitions`
- `--trigger`, `--max-offsets` (rate limiting)

**Measure:**
- Sustainable throughput
- Batch ms
- Backlog growth
- Worker CPU

**Example runs:**
```
python setup.py bench B_spark_scaling --worker-cpus 0.5 --label cpu0.5
python setup.py bench B_spark_scaling --worker-cpus 1 --label cpu1
python setup.py bench B_spark_scaling --worker-cpus 1 --cores 2 --worker-cores 4 --partitions 4 --label cpu1-2cores-4parts
python setup.py bench B_spark_scaling --worker-cpus 0.5 --max-offsets 2000 --label cpu0.5-ratelimited
python setup.py config --reset
```

**Tips:**
- Powerful laptops may never saturate Spark at full CPU. That's what `--worker-cpus` is for.
- Small CPU limits make Spark start slowly; `bench` waits for it and reports the start-up time.

**Discussion:**
- Does throughput scale linearly with CPU? With cores? Why do partitions matter for parallelism?
- What does `--max-offsets` do to latency and backlog when the input exceeds capacity?
- Using your numbers, how many "instances" would you need for 10× the load?

---

## C. Storage layout and the small-files problem

**Question:** How do the trigger interval and file format change the number and size of files written to S3, and what does compaction gain?

**Why it matters:** Streaming writes create many small objects. That costs S3 requests (money), slows every later query, and is why table formats like Delta, Iceberg and Hudi exist.

**Scenario:** `C_small_files`, a steady load, with objects per step reported.

**Independent variables:**
- `--trigger off|5s|30s|1m`
- Sink format (JSON vs Parquet, both written in every run)
- `--payload-bytes`
- Compaction

**Measure:**
- New files per step
- Average file KiB
- Bytes per row (JSON vs Parquet)
- End-to-end latency (the trade-off)
- `compact`: files, size and read time before vs after

**Example runs:**
```
python setup.py bench C_small_files --trigger off --label trigger-off
python setup.py bench C_small_files --trigger 30s --label trigger-30s
python setup.py bench C_small_files --trigger 1m  --label trigger-1m
python setup.py compact --sink parquet
python setup.py compact --sink json --files 2
python setup.py config --reset
```

**Discussion:**
- Files per hour vs trigger interval: what would this cost on S3 at $0.005 per 1,000 PUTs?
- Why is Parquet much smaller than JSON, and why do small Parquet files lose much of that advantage?
- Compaction made reads faster. When would you run it, and what does it cost?

---

## D. Latency vs throughput

**Question:** How does end-to-end latency (event → committed in S3) change with load and batching?

**Why it matters:** Real-time use cases (fraud, monitoring) have latency SLOs. You must understand the latency distribution (p95/p99, not just averages) and what drives it.

**Scenario:** `D_latency`, stepping the rate up, with latency percentiles per step from the audit.

**Independent variables:**
- Rate (the steps)
- `--trigger`
- Producer `--linger-ms`
- `--worker-cpus`

**Measure:**
- Queue and end-to-end p50/p95/p99 per step
- Batch ms

**Example runs:**
```
python setup.py bench D_latency --label default
python setup.py bench D_latency --trigger 10s --label trigger-10s
python setup.py bench D_latency --linger-ms 50 --label linger50
python setup.py bench D_latency --worker-cpus 0.5 --label cpu0.5
python setup.py config --reset
```

**Discussion:**
- Why might latency *decrease* as load increases at first, then rise sharply?
- Break the end-to-end latency into its parts: producer batching, queue wait, batch processing and commit.
- Which setting would you choose for a 1-second p99 SLO, and what does it cost in throughput or files?

---

## E. Fault tolerance and exactly-once delivery

**Question:** Is any data lost or duplicated when components fail under load, and how long does recovery take?

**Why it matters:** Distributed systems fail constantly. Exactly-once processing (Kafka offsets plus Spark checkpoints plus an idempotent file sink) is what makes results trustworthy.

**Scenario:** `E_faults`, a steady load with four injected faults:
- kill the JSON consumer
- pause Kafka
- restart S3
- stop the Spark worker

**Independent variables:**
- Fault type and duration (`python setup.py chaos … --seconds N`)
- Producer `--acks` and `--idempotence`
- Load level

**Measure:**
- Recovery time
- Backlog growth during the fault
- Data accounting (PASS/FAIL)
- Producer failures

**Example runs:**
```
python setup.py bench E_faults --label safe
python setup.py bench E_faults --acks 0 --idempotence false --label acks0
python setup.py producers start --rate 50
python setup.py chaos kill-consumer parquet --seconds 30
python setup.py chaos pause-kafka --seconds 60
python setup.py producers stop
python setup.py audit
```

**Discussion:**
- Why is nothing duplicated even when a consumer is killed mid-batch? (Look at the `_spark_metadata` folder in S3 and the checkpoints.)
- With `--acks 0`, can the pipeline still guarantee no loss end to end? Where exactly could data be lost?
- Which failure took longest to recover from, and what would reduce it in production (replicas, standby executors, multi-AZ)?

---

## What to submit

| Part | Reader | Contents | Length |
|---|---|---|---|
| **Executive summary** | A manager who reads nothing else | Your answer in the first sentence; 2–4 bullets with your key numbers and what they mean (capacity, cost, risk); one clear recommendation. Plain language. | About half a page |
| **Abstract** | A technical reader deciding whether to read on | Like a published article's abstract: the context and question; what you changed and how you measured it; the results with specific numbers; the conclusion and why it matters. | At most 3 paragraphs (about 150–300 words) |
| **Results ZIP** | Your instructor | The two `results/` folders (default and changed runs). Every number you report must come from them. | Two folders |

[report_examples_and_rubric.md](report_examples_and_rubric.md) has a worked example of both documents and the **grading rubric**.

## Tips

- **Change only one setting,** and label the two runs (`--label default`, `--label changed`).
- **Restore defaults** with `python setup.py config --reset` before switching experiments.
- **Close heavy applications while measuring.** Your laptop is the whole cluster.
- **The stack needs about 6 GB of RAM** under load. Give Docker Desktop at least 8 GB and 4 CPUs.
- **To start completely fresh:** `python setup.py clean --volumes`, then `python setup.py up`. This deletes all data.
