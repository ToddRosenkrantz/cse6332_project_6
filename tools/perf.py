"""
Performance research tooling for setup.py: bench, audit, chaos and compact.

Kept separate from setup.py to keep that file readable; it reuses setup.py's
helpers through the `cli` module passed to each entry point.
"""
import argparse
import base64
import csv
import json
import platform
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "bench"
RESULTS_DIR = ROOT / "results"
SINKS = {"json": ("topic-json", "spark-consumer-json", "topic-json_sink"),
         "parquet": ("topic-parq", "spark-consumer-parquet", "topic-parq_sink")}
SECRET_MARKERS = ("PASSWORD", "SECRET", "_KEY")

cli = None  # setup.py module, set by each entry point


# ============================================================================== helpers
def env(key, default=""):
    return cli.ENV.get(key, default)


def prom_base():
    return f"http://{cli.HOST}:{env('PROMETHEUS_PORT', '9090')}"


def prom_instant(query, at=None):
    params = {"query": query}
    if at is not None:
        params["time"] = f"{at:.3f}"
    url = f"{prom_base()}/api/v1/query?" + urllib.parse.urlencode(params)
    try:
        return cli.http_json(url, timeout=15)["data"]["result"]
    except Exception:
        return []


def prom_range(query, start, end, step=5):
    url = f"{prom_base()}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": step})
    try:
        return cli.http_json(url, timeout=30)["data"]["result"]
    except Exception:
        return []


def series_stats(values):
    nums = sorted(float(v) for _, v in values if v not in ("NaN", "+Inf", "-Inf"))
    if not nums:
        return None
    p95 = nums[min(len(nums) - 1, int(round(0.95 * (len(nums) - 1))))]
    return {"mean": statistics.fmean(nums), "p95": p95, "max": nums[-1]}


def grafana_annotate(text, tags, start_ms, end_ms=None):
    body = {"time": int(start_ms), "tags": ["cse6332", *tags], "text": text}
    if end_ms:
        body["timeEnd"] = int(end_ms)
    token = base64.b64encode(f"{env('GRAFANA_ADMIN_USER')}:{env('GRAFANA_ADMIN_PASSWORD')}".encode()).decode()
    req = urllib.request.Request(f"http://{cli.HOST}:{env('GRAFANA_PORT', '3000')}/api/annotations",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # annotations are a convenience; never fail an experiment over them


def docker(*args, capture=True):
    return cli.run_command(["docker", *args], capture=capture, fatal=False)


def kafka_end_offsets():
    """{topic: total end offset across partitions}"""
    result = cli.run_command(cli.kafka_cli("/opt/kafka/bin/kafka-get-offsets.sh", "--bootstrap-server",
                                           "localhost:29092", "--topic", ",".join(t for t, _, _ in SINKS.values())),
                             capture=True, fatal=False)
    totals = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(":")
        if len(parts) == 3 and parts[2].isdigit():
            totals[parts[0]] = totals.get(parts[0], 0) + int(parts[2])
    return totals


def committed_offsets(sink):
    """Kafka offset Spark has fully written for a sink (sum over partitions), or None."""
    topic, container, query = SINKS[sink]
    script = (f"d=/opt/spark/work-dir/checkpoints/{query}; "
              "c=$(ls $d/commits 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1); "
              "[ -n \"$c\" ] && tail -1 $d/offsets/$c")
    result = docker("exec", container, "sh", "-c", script)
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
        return sum(int(v) for v in data.get(topic, {}).values())
    except (ValueError, IndexError, AttributeError):
        return None


def backlog():
    """{sink: messages in Kafka not yet written to S3}"""
    ends = kafka_end_offsets()
    out = {}
    for sink, (topic, _, _) in SINKS.items():
        done = committed_offsets(sink)
        out[sink] = None if done is None or topic not in ends else max(0, ends[topic] - done)
    return out


def s3_snapshot():
    """{sink: (objects, bytes)} of committed data files (excludes _spark_metadata)."""
    out = {}
    for sink in SINKS:
        try:
            objs = cli.list_objects(f"{sink}/")
            out[sink] = (len(objs), sum(o["Size"] for o in objs))
        except Exception:
            out[sink] = (None, None)
    return out


def container_names():
    result = docker("ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}")
    return dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)


def container_of(series_id, names):
    """Map a cAdvisor cgroup id (/docker/<id> or docker-<id>.scope) to a container name."""
    for cid, name in names.items():
        if cid in series_id:
            return name
    return None


def machine_facts():
    info = docker("info", "--format", "{{.NCPU}}|{{.MemTotal}}|{{.ServerVersion}}|{{.OperatingSystem}}")
    ncpu, mem, version, osname = (info.stdout.strip().split("|") + ["", "", "", ""])[:4]
    git = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=ROOT,
                         capture_output=True, text=True)
    return {
        "host_os": f"{platform.system()} {platform.release()}", "host_arch": cli.host_arch(),
        "python": sys.version.split()[0], "docker_cpus": ncpu,
        "docker_memory_gib": round(int(mem) / 2**30, 1) if mem.isdigit() else mem,
        "docker_version": version, "docker_os": osname,
        "project_version": git.stdout.strip() if git.returncode == 0 else "unknown (no git)",
    }


def safe_env():
    return {k: v for k, v in cli.ENV.items() if not any(m in k for m in SECRET_MARKERS)}


_COMMITS_SCRIPT = r"""
import json, os, sys
d = "/opt/spark/work-dir/checkpoints/" + sys.argv[1]
out = []
for n in os.listdir(d + "/commits"):
    if not n.isdigit():
        continue
    try:
        with open(f"{d}/offsets/{n}") as f:
            meta = json.loads(f.read().splitlines()[1])
        out.append([meta["batchTimestampMs"], int(os.path.getmtime(f"{d}/commits/{n}") * 1000)])
    except (OSError, ValueError, KeyError, IndexError):
        pass
print(json.dumps(out))
"""


def export_commit_times():
    """Copy each sink's (batch start, batch commit) times into spark-master for the audit job."""
    counts = {}
    for sink, (_, container, query) in SINKS.items():
        result = docker("exec", container, "python3", "-c", _COMMITS_SCRIPT, query)
        data = result.stdout.strip() if result.returncode == 0 else "[]"
        cli.run_command(["docker", "exec", "-i", "spark-master", "sh", "-c", f"cat > /tmp/commits_{sink}.json"],
                        capture=True, fatal=False, input_text=data or "[]")
        try:
            counts[sink] = len(json.loads(data or "[]"))
        except ValueError:
            counts[sink] = 0
    return counts


def clock_offset_ms():
    """Container clock minus host clock (ms). Event times come from the host, batch times from containers."""
    samples = []
    for _ in range(3):
        t0 = time.time()
        r = docker("exec", "spark-master", "python3", "-c", "import time; print(time.time())")
        t1 = time.time()
        try:
            samples.append((float(r.stdout.strip()) - (t0 + t1) / 2) * 1000)
        except ValueError:
            pass
    return round(statistics.median(samples), 1) if samples else None


def run_spark_batch(job, job_args, label):
    """Run a batch job from ./jobs in spark-master, in local mode (no cluster cores needed)."""
    cmd = ["docker", "exec",
           "-e", f"S3_ACCESS_KEY={env('S3_ACCESS_KEY')}", "-e", f"S3_SECRET_KEY={env('S3_SECRET_KEY')}",
           "-e", f"S3_BUCKET={env('S3_BUCKET', 'spark-output')}", "-e", "S3_ENDPOINT=http://seaweedfs:8333",
           "-e", f"TZ={env('TZ', 'UTC')}",
           "spark-master", "/opt/spark/bin/spark-submit", "--master", "local[2]", "--driver-memory", "1g",
           "--jars", "/opt/spark/custom-jars/*", "--conf", "spark.ui.enabled=false",
           "--conf", "spark.sql.shuffle.partitions=4", f"/opt/spark/jobs/{job}", *job_args]
    print(f"  ▶ {label} (Spark batch job, ~30-60 s)…")
    result = cli.run_command(cmd, capture=True, fatal=False)
    marker = job.split(".")[0].upper() + "_JSON "
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    print(f"  ❌ {label} failed:")
    print("\n".join((result.stderr or result.stdout).strip().splitlines()[-15:]))
    return None


def require_stack():
    if not cli.stack_running():
        print("❌ The stack is not running. Start it first: python setup.py up")
        sys.exit(1)


# ============================================================================== audit
def audit_data(windows=None):
    """Run the audit job and compare with Kafka. Returns a dict (also used by bench)."""
    ends = kafka_end_offsets()
    batches = export_commit_times()
    result = run_spark_batch("audit.py", ["--windows", json.dumps(windows or [])], "Auditing S3 output")
    if result is None:
        return None
    result["kafka_messages"] = ends
    checks = []
    for sink, (topic, _, _) in SINKS.items():
        s = result.get(sink, {})
        in_kafka = ends.get(topic)
        stored = s.get("rows") if s.get("exists") else 0
        checks.append({"sink": sink, "topic": topic, "in_kafka": in_kafka, "stored": stored,
                       "duplicates": s.get("duplicates", 0),
                       "ok": in_kafka is not None and stored == in_kafka and s.get("duplicates", 0) == 0})
    result["accounting"] = checks
    result["pass"] = all(c["ok"] for c in checks)
    result["batches_timed"] = batches
    result["clock_offset_ms"] = clock_offset_ms()
    return result


def fmt_lat(lat):
    if not lat:
        return "n/a"
    return f"{lat['p50_ms']:.0f} / {lat['p95_ms']:.0f} / {lat['p99_ms']:.0f} ms"


def print_audit(result):
    cli.print_banner("DATA ACCOUNTING (Kafka vs S3)")
    for c in result["accounting"]:
        mark = "✅" if c["ok"] else "❌"
        print(f"  {mark} {c['sink']:<8} Kafka {c['topic']}: {c['in_kafka']:>9}   stored in S3: {c['stored']:>9}"
              f"   duplicates: {c['duplicates']}")
    print(f"  {'PASS' if result['pass'] else 'FAIL'}: every message in Kafka is stored exactly once"
          if result["pass"] else
          "  FAIL: counts differ. If producers or consumers are still running, stop producers and wait for the"
          " sinks to catch up (python setup.py monitor), then audit again.")
    cli.print_banner("FILES AND LATENCY (p50 / p95 / p99)")
    for sink in SINKS:
        s = result.get(sink, {})
        if not s.get("exists"):
            print(f"  {sink:<8} no output yet")
            continue
        print(f"  {sink:<8} {s['files']} files, {s['bytes'] / 2**20:.1f} MiB, avg {s['avg_file_kb']} KiB/file")
        print(f"           queue (event → micro-batch): {fmt_lat(s['latency']['queue'])}")
        print(f"           end-to-end (event → committed in S3): {fmt_lat(s['latency']['end_to_end'])}")
    if result.get("clock_offset_ms") is not None:
        print(f"  Container clock − host clock: {result['clock_offset_ms']} ms (latencies include this offset)")


def audit_cmd(module, args):
    global cli
    cli = module
    require_stack()
    result = audit_data()
    if result is None:
        sys.exit(1)
    print_audit(result)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))
        print(f"\n  💾 Saved {args.json}")


# ============================================================================== chaos
def wait_until(check, timeout, interval=1.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if check():
                return time.time() - start
        except Exception:
            pass
        time.sleep(interval)
    return None


def consumer_progressing(sink, since_offset):
    done = committed_offsets(sink)
    return done is not None and since_offset is not None and done > since_offset


def chaos_action(action, target=None, seconds=None, quiet=False):
    """Inject a fault, restore it, and measure recovery: the time from restoring the component
    until the affected sinks commit new data beyond what they had at that moment."""
    say = (lambda *a: None) if quiet else print
    if action == "restart-s3":
        seconds = None  # a restart has no chosen duration
    elif seconds is None:
        seconds = 15
    sinks = [target or "json"] if action == "kill-consumer" else list(SINKS)
    ev = {"action": action, "target": target if action == "kill-consumer" else None, "seconds": seconds,
          "start": time.time()}
    # read inside the consumer containers, so take it while they are still up
    before_fault = {s: committed_offsets(s) for s in sinks}
    if action == "kill-consumer":
        container = SINKS[sinks[0]][1]
        say(f"  💥 Killing {container}; restarting it after {seconds:g}s")
        docker("kill", container)
        time.sleep(seconds)
        restore = lambda: docker("start", container)
    elif action == "pause-kafka":
        say(f"  ⏸️  Pausing Kafka for {seconds:g}s")
        docker("pause", "kafka")
        time.sleep(seconds)
        restore = lambda: docker("unpause", "kafka")
    elif action == "restart-s3":
        say("  🔁 Restarting SeaweedFS (S3)")
        restore = lambda: docker("restart", "seaweedfs")
    elif action == "stop-worker":
        say(f"  🛑 Stopping the Spark worker for {seconds:g}s")
        docker("stop", "spark-worker")
        time.sleep(seconds)
        restore = lambda: docker("start", "spark-worker")
    else:
        raise ValueError(f"unknown chaos action {action}")
    at_restore = {}
    for s in sinks:
        now = committed_offsets(s)  # None while that consumer is down
        at_restore[s] = now if now is not None else before_fault[s]
    ev["restored"] = time.time()
    restore()
    rec = wait_until(lambda: all(consumer_progressing(s, at_restore[s] if at_restore[s] is not None else -1)
                                 for s in sinks), 600, 1)
    ev["recovery_s"] = None if rec is None else round(time.time() - ev["restored"], 1)
    ev["end"] = time.time()
    what = f"{action} {ev['target'] or ''}".strip()
    grafana_annotate(f"chaos: {what} (recovery {ev['recovery_s']}s)", ["chaos"], ev["start"] * 1000, ev["end"] * 1000)
    say(f"  ✅ Sinks processing again {ev['recovery_s']}s after the fault was removed" if ev["recovery_s"] is not None
        else "  ❌ Sinks did not resume within 10 minutes (check: python setup.py status / logs)")
    cli.LOG_DIR.mkdir(exist_ok=True)
    with open(cli.LOG_DIR / "chaos.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")
    return ev


def producers_running():
    return any(cli.live_process(f"{impl}_{topic}", cli.PRODUCER_SCRIPTS[impl].name)
               for impl in ("kp", "ck") for topic in cli.DATA_TOPICS)


def chaos_cmd(module, args):
    global cli
    cli = module
    require_stack()
    if not producers_running():
        print("❌ Start some load first, so recovery can be measured: python setup.py producers start")
        sys.exit(1)
    cli.print_banner(f"CHAOS: {args.action}")
    chaos_action(args.action, args.target, args.seconds)
    print("  Check for lost or duplicated data afterwards with: python setup.py audit")


# ============================================================================== compact
def latest_busy_partition(sink):
    counts = {}
    for obj in cli.list_objects(f"{sink}/"):
        parts = dict(p.split("=", 1) for p in obj["Key"].split("/") if "=" in p)
        if "date" in parts and "hour" in parts:
            key = (parts["date"], int(parts["hour"]))
            counts[key] = counts.get(key, 0) + 1
    return max(counts, key=lambda k: (counts[k], k)) if counts else None


def compact_cmd(module, args):
    global cli
    cli = module
    require_stack()
    date, hour = args.date, args.hour
    if date is None or hour is None:
        found = latest_busy_partition(args.sink)
        if not found:
            print(f"❌ No {args.sink} output in S3 yet; run producers first.")
            sys.exit(1)
        date, hour = found
    cli.print_banner(f"COMPACTING {args.sink} date={date}/hour={hour} → {args.files} file(s)")
    result = run_spark_batch("compact.py", ["--sink", args.sink, "--date", str(date), "--hour", str(hour),
                                            "--files", str(args.files)], "Compacting")
    if not result:
        sys.exit(1)
    b, a = result["before"], result["after"]
    print(f"  {'':<10} {'files':>8} {'MiB':>9} {'rows':>9} {'read time':>10}")
    for name, d in (("before", b), ("after", a)):
        print(f"  {name:<10} {d['files']:>8} {d['bytes'] / 2**20:>9.2f} {d['rows']:>9} {d['read_seconds']:>9.2f}s")
    print(f"  Compaction took {result['compaction_seconds']:.1f}s; output: {result['target']}")
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"compact_{args.sink}_{date}_h{hour}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"  💾 Saved {out.relative_to(ROOT)}")


# ============================================================================== bench
# Counters: average rate over exactly the step window = increase(window) / window
COUNTER_QUERIES = {
    "producer_msgs_s": ('sum by (topic) (increase(kafka_producer_sent_total[{w}s]))', "topic"),
    "producer_failed_s": ('sum(increase(kafka_producer_failed_total[{w}s]))', None),
    "broker_msgs_in_s": ('sum by (topic) (increase(kafka_server_brokertopicmetrics_messagesinpersec_total'
                         '{{topic=~"topic-json|topic-parq"}}[{w}s]))', "topic"),
    "broker_bytes_in_s": ('sum(increase(kafka_server_brokertopicmetrics_bytesinpersec_total'
                          '{{topic=~"topic-json|topic-parq"}}[{w}s]))', None),
}
# Gauges: sampled every 5 s over the step window
STEP_QUERIES = {
    "spark_input_s": ('sum by (sink) ({__name__=~"metrics_.*_driver_spark_streaming_.*_inputRate_total_Value"})', "sink"),
    "spark_processed_s": ('sum by (sink) ({__name__=~"metrics_.*_driver_spark_streaming_.*_processingRate_total_Value"})',
                          "sink"),
    "spark_batch_ms": ('max by (sink) ({__name__=~"metrics_.*_driver_spark_streaming_.*_latency_Value"})', "sink"),
}
CONTAINER_QUERIES = {
    "cpu_cores": 'rate(container_cpu_usage_seconds_total{id=~"/docker/.+|/system.slice/docker-.+"}[30s])',
    "mem_mib": 'container_memory_working_set_bytes{id=~"/docker/.+|/system.slice/docker-.+"} / 1048576',
}
TOPIC_TO_SINK = {topic: sink for sink, (topic, _, _) in SINKS.items()}


def load_scenario(name):
    path = Path(name)
    if not path.exists():
        path = BENCH_DIR / (name if name.endswith(".json") else f"{name}.json")
    if not path.exists():
        print(f"❌ Unknown scenario '{name}'. Available:")
        for p in sorted(BENCH_DIR.glob("*.json")):
            desc = json.loads(p.read_text()).get("title", "")
            print(f"   {p.stem:<22} {desc}")
        sys.exit(1)
    return json.loads(path.read_text())


def apply_overrides(sc, args):
    sc = json.loads(json.dumps(sc))  # deep copy
    prod = sc.setdefault("producers", {})
    for key in ("impl", "threads", "acks", "idempotence", "linger_ms", "batch_size", "compression",
                "payload_bytes"):
        value = getattr(args, key, None)
        if value is not None:
            prod[key] = value
    if getattr(args, "burst", False):
        prod["burst"] = True
    if getattr(args, "rate", None) is not None:
        prod["rate"] = args.rate
    if getattr(args, "threads", None) is not None:
        prod["threads"] = args.threads
    if args.steps:
        sc["steps"] = [float(v) for v in args.steps.split(",")]
    if getattr(args, "vary", None):
        sc["vary"] = args.vary
    if args.step_seconds:
        sc["step_seconds"] = args.step_seconds
    if args.warmup is not None:
        sc["warmup"] = args.warmup
    if args.quick:
        sc["step_seconds"] = min(sc.get("step_seconds", 60), 30)
        sc["warmup"] = min(sc.get("warmup", 20), 10)
    spark = sc.setdefault("spark", {})
    for key in ("trigger", "max_offsets", "cores", "worker_cores", "worker_cpus", "worker_memory", "executor_memory"):
        value = getattr(args, key, None)
        if value is not None:
            spark[key] = value
    if args.partitions:
        sc["partitions"] = args.partitions
    return sc


def producer_namespace(prod, rate=None, threads=None):
    ns = argparse.Namespace(impl=prod.get("impl", "ck"), topic=None, producer_args=[],
                            rate=rate, threads=threads if prod.get("impl", "ck") != "kp" else None,
                            acks=prod.get("acks"), idempotence=prod.get("idempotence"),
                            linger_ms=prod.get("linger_ms"), batch_size=prod.get("batch_size"),
                            compression=prod.get("compression"), payload_bytes=prod.get("payload_bytes"),
                            burst=bool(prod.get("burst")))
    return ns


def senders_per_topic(prod, threads):
    impl = prod.get("impl", "ck")
    return (1 if impl in ("kp", "both") else 0) + ((threads or 0) if impl in ("ck", "both") else 0)


def configure_spark(spark_cfg):
    if not spark_cfg:
        return
    ns = argparse.Namespace(trigger=spark_cfg.get("trigger"), max_offsets=spark_cfg.get("max_offsets"),
                            cores=spark_cfg.get("cores"), worker_cores=spark_cfg.get("worker_cores"),
                            worker_cpus=spark_cfg.get("worker_cpus"),
                            worker_memory=spark_cfg.get("worker_memory"),
                            executor_memory=spark_cfg.get("executor_memory"),
                            driver_memory=None, consumer_memory=None)
    if any(v is not None for v in vars(ns).values()):
        ns.max_offsets = None if ns.max_offsets is None else str(ns.max_offsets)
        cli.spark_cmd(ns)
        print("  ℹ️ These Spark settings are saved in .env and stay in effect after the benchmark "
              "(python setup.py config --reset restores the defaults).")


def collect_step_metrics(step, names):
    t0, t1 = step["window"]
    metrics = {}
    width = max(10, int(t1 - t0))
    for key, (query, label) in COUNTER_QUERIES.items():
        for series in prom_instant(query.format(w=width), at=t1):
            tag = series["metric"].get(label, "all") if label else "all"
            if label == "topic":
                tag = TOPIC_TO_SINK.get(tag, tag)
            try:
                metrics[f"{key}.{tag}"] = {"mean": float(series["value"][1]) / width, "p95": None, "max": None}
            except (ValueError, KeyError, IndexError):
                pass
    for key, (query, label) in STEP_QUERIES.items():
        for series in prom_range(query, t0, t1):
            stats = series_stats(series["values"])
            if not stats:
                continue
            tag = series["metric"].get(label, "all") if label else "all"
            if label == "topic":
                tag = TOPIC_TO_SINK.get(tag, tag)
            metrics[f"{key}.{tag}"] = stats
    for key, query in CONTAINER_QUERIES.items():
        for series in prom_range(query, t0, t1):
            name = container_of(series["metric"].get("id", ""), names) or series["metric"].get("name")
            stats = series_stats(series["values"])
            if name and stats:
                metrics[f"{key}.{name}"] = stats
    return metrics


def mean(metrics, key):
    v = metrics.get(key)
    return v["mean"] if v else None


def fmt(value, digits=0, suffix=""):
    if value is None:
        return "–"
    return f"{value:,.{digits}f}{suffix}"


def bench_cmd(module, args):
    global cli
    cli = module
    require_stack()
    sc = apply_overrides(load_scenario(args.scenario), args)
    prod = sc.get("producers", {})
    vary = sc.get("vary", "rate")
    steps = sc["steps"]
    step_s = float(sc.get("step_seconds", 60))
    warmup = float(sc.get("warmup", 20))
    base_rate = float(prod.get("rate", 20))
    base_threads = int(prod.get("threads", 2))
    if vary == "threads" and prod.get("impl", "ck") == "kp":
        print("❌ vary=threads needs confluent-kafka (--impl ck or both)")
        sys.exit(1)
    chaos_plan = sc.get("chaos", [])
    est = warmup + len(steps) * step_s + sum(c.get("seconds", 15) for c in chaos_plan)
    label = f"_{args.label}" if args.label else ""
    run_id = f"{datetime.now():%Y%m%d_%H%M%S}_{sc.get('name', args.scenario)}{label}"
    out_dir = RESULTS_DIR / run_id

    cli.print_banner(f"BENCHMARK {sc.get('name', args.scenario)}: {sc.get('title', '')}")
    print(f"  Steps ({vary}): {', '.join(f'{v:g}' for v in steps)}  •  {step_s:g}s each  •  warm-up {warmup:g}s")
    print(f"  Producers: {prod}")
    print(f"  Estimated time: ~{(est + 120) / 60:.0f} min (including drain and audit)")

    # ---- prepare
    cli.stop_producers(["kp", "ck"], cli.DATA_TOPICS, include_ramps=True)
    configure_spark(sc.get("spark"))
    if sc.get("partitions"):
        cli.topics_cmd(argparse.Namespace(partitions=int(sc["partitions"]), topic=None))
    names = container_names()
    facts = machine_facts()
    run_start = time.time()
    start_offsets = kafka_end_offsets()

    committed_at_start = {s: committed_offsets(s) for s in SINKS}
    first = steps[0]
    rate = first if vary == "rate" else base_rate
    threads = int(first) if vary == "threads" else base_threads
    cli.start_producers(producer_namespace(prod, rate=rate, threads=threads))
    grafana_annotate(f"bench {run_id}: start", ["bench"], run_start * 1000)

    events, results = [], []
    try:
        if warmup > 0:
            print(f"\n  ⏳ Warm-up {warmup:g}s")
            time.sleep(warmup)
        # Readiness gate: steps only start once both sinks process the new data and have caught up,
        # so restarts or slow executor start-up (e.g. a small --worker-cpus) don't pollute step 1.
        print("  ⏳ Waiting until both Spark sinks are processing live data…")

        def processing():
            for sink in SINKS:
                done = committed_offsets(sink)
                if done is None or (committed_at_start[sink] is not None and done <= committed_at_start[sink]):
                    return False
            return True
        ready_s = wait_until(processing, 600, 3)
        if ready_s is None:
            print("  ❌ The Spark sinks did not start processing within 10 minutes. The Spark settings may be too")
            print("     constrained (see: python setup.py logs spark-consumer-json). Stopping the benchmark.")
            sys.exit(1)
        first_offered = None if prod.get("burst") else rate * senders_per_topic(prod, threads)
        limit = max(500, 5 * (first_offered or 1000))
        catchup_s = wait_until(lambda: all((b or 0) <= limit for b in backlog().values()), 300, 3)
        startup_s = time.time() - run_start
        print(f"  ✅ Sinks ready after {startup_s:.0f}s"
              + ("" if catchup_s is not None else " (backlog still high; step 1 starts anyway)"))
        for i, value in enumerate(steps, start=1):
            if vary == "rate":
                cli.set_lambda(argparse.Namespace(topic="topic-json", value=value))
                cli.set_lambda(argparse.Namespace(topic="topic-parq", value=value))
                rate = value
            else:
                cli.set_prods(argparse.Namespace(topic="topic-json", count=int(value)))
                cli.set_prods(argparse.Namespace(topic="topic-parq", count=int(value)))
                threads = int(value)
            t_start = time.time()
            s3_before, backlog_before = s3_snapshot(), backlog()
            offered = None if prod.get("burst") else rate * senders_per_topic(prod, threads)
            print(f"\n  ▶ Step {i}/{len(steps)}: {vary}={value:g}"
                  + (f"  (offered ≈ {offered:g} msgs/s per topic)" if offered else "  (burst)"))
            planned = sorted((c for c in chaos_plan if int(c.get("step", 0)) == i), key=lambda c: c.get("at", 0))
            for c in planned:
                wait = t_start + float(c.get("at", 0)) - time.time()
                if wait > 0:
                    time.sleep(wait)
                ev = chaos_action(c["action"], c.get("target"), c.get("seconds"))
                ev["step"] = i
                events.append(ev)
            remaining = t_start + step_s - time.time()
            if remaining > 0:
                time.sleep(remaining)
            t_end = time.time()
            s3_after, backlog_after = s3_snapshot(), backlog()
            settle = min(15.0, step_s / 4)
            grafana_annotate(f"step {i}: {vary}={value:g}", ["bench", "step"], t_start * 1000, t_end * 1000)
            results.append({"step": i, "value": value, "offered_per_topic": offered,
                            "window": [t_start + settle, t_end], "start": t_start, "end": t_end,
                            "s3_before": s3_before, "s3_after": s3_after,
                            "backlog_before": backlog_before, "backlog_after": backlog_after})
    finally:
        # always stop load, even on Ctrl-C or an error
        cli.stop_producers(["kp", "ck"], cli.DATA_TOPICS, include_ramps=True)

    load_end = time.time()
    print("\n  ⏳ Waiting for the sinks to write everything already in Kafka (up to 5 min)…")
    drain_s = wait_until(lambda: all(v == 0 for v in backlog().values()), 300, 3)
    print(f"  {'✅ Drained in ' + str(round(drain_s)) + 's' if drain_s is not None else '⚠️ Not fully drained after 5 min'}")
    end_offsets = kafka_end_offsets()

    print("\n  📊 Collecting metrics from Prometheus…")
    names = {**names, **container_names()}
    for step in results:
        step["metrics"] = collect_step_metrics(step, names)
    run_metrics = collect_step_metrics({"window": [run_start, load_end]}, names)

    audit = None
    if not args.no_audit:
        windows = [{"name": f"step{s['step']}", "start": s["start"], "end": s["end"]} for s in results]
        audit = audit_data(windows)

    grafana_annotate(f"bench {run_id}: end", ["bench"], load_end * 1000)
    write_results(out_dir, sc, args, facts, results, events, run_metrics, audit,
                  {"start": run_start, "load_end": load_end, "drain_s": drain_s, "startup_s": startup_s,
                   "start_offsets": start_offsets, "end_offsets": end_offsets})


def sustained(step, sink):
    """Kept up = the Kafka backlog did not grow during the step and stays small (< ~10 s of input)."""
    after, before = step["backlog_after"].get(sink), step["backlog_before"].get(sink)
    if after is None:
        return None
    rate = mean(step["metrics"], f"producer_msgs_s.{sink}") or 0
    produced = rate * (step["end"] - step["start"])
    growth = after - (before or 0)
    # keeping up = backlog not growing, and either small or shrinking (still catching up is fine)
    return growth <= max(200, 0.02 * produced) and (after <= max(500, 10 * rate) or growth < 0)


def write_results(out_dir, sc, args, facts, results, events, run_metrics, audit, run):
    out_dir.mkdir(parents=True, exist_ok=True)
    rel = out_dir.relative_to(ROOT)
    config = {"scenario": sc, "command_line": sys.argv[1:], "machine": facts, "settings": safe_env(),
              "topic_partitions": cli.topic_partitions(), "run": run}
    (out_dir / "config.json").write_text(json.dumps(config, indent=2, default=str))
    (out_dir / "events.json").write_text(json.dumps(events, indent=2))
    if audit:
        (out_dir / "audit.json").write_text(json.dumps(audit, indent=2))

    # ---- steps.csv (one row per step, wide)
    containers = sorted({k.split(".", 1)[1] for s in results for k in s["metrics"] if k.startswith("cpu_cores.")})
    fields = ["step", sc.get("vary", "rate"), "offered_per_topic"]
    for sink in SINKS:
        fields += [f"{sink}_producer_msgs_s", f"{sink}_broker_msgs_s", f"{sink}_spark_input_s",
                   f"{sink}_spark_processed_s", f"{sink}_batch_ms_mean", f"{sink}_batch_ms_p95",
                   f"{sink}_backlog_growth", f"{sink}_sustained", f"{sink}_new_files", f"{sink}_avg_new_file_kb",
                   f"{sink}_latency_e2e_p50_ms", f"{sink}_latency_e2e_p95_ms", f"{sink}_latency_queue_p95_ms"]
    fields += ["broker_bytes_in_s", "producer_failed_s"]
    fields += [f"cpu_{c}" for c in containers] + [f"mem_mib_max_{c}" for c in containers]
    rows = []
    for s in results:
        m = s["metrics"]
        row = {"step": s["step"], sc.get("vary", "rate"): s["value"], "offered_per_topic": s["offered_per_topic"]}
        for sink in SINKS:
            (n0, b0), (n1, b1) = s["s3_before"][sink], s["s3_after"][sink]
            new_files = (n1 - n0) if None not in (n0, n1) else None
            win = ((audit or {}).get(sink, {}).get("windows", {}) or {}).get(f"step{s['step']}", {}) if audit else {}
            e2e, queue = (win or {}).get("end_to_end") or {}, (win or {}).get("queue") or {}
            row.update({
                f"{sink}_producer_msgs_s": mean(m, f"producer_msgs_s.{sink}"),
                f"{sink}_broker_msgs_s": mean(m, f"broker_msgs_in_s.{sink}"),
                f"{sink}_spark_input_s": mean(m, f"spark_input_s.{sink}"),
                f"{sink}_spark_processed_s": mean(m, f"spark_processed_s.{sink}"),
                f"{sink}_batch_ms_mean": mean(m, f"spark_batch_ms.{sink}"),
                f"{sink}_batch_ms_p95": (m.get(f"spark_batch_ms.{sink}") or {}).get("p95"),
                f"{sink}_backlog_growth": None if s["backlog_after"].get(sink) is None else
                (s["backlog_after"][sink] - (s["backlog_before"].get(sink) or 0)),
                f"{sink}_sustained": sustained(s, sink),
                f"{sink}_new_files": new_files,
                f"{sink}_avg_new_file_kb": round((b1 - b0) / new_files / 1024, 2) if new_files else None,
                f"{sink}_latency_e2e_p50_ms": e2e.get("p50_ms"), f"{sink}_latency_e2e_p95_ms": e2e.get("p95_ms"),
                f"{sink}_latency_queue_p95_ms": queue.get("p95_ms"),
            })
        row["broker_bytes_in_s"] = mean(m, "broker_bytes_in_s.all")
        row["producer_failed_s"] = mean(m, "producer_failed_s.all")
        for c in containers:
            row[f"cpu_{c}"] = mean(m, f"cpu_cores.{c}")
            row[f"mem_mib_max_{c}"] = (m.get(f"mem_mib.{c}") or {}).get("max")
        rows.append(row)
    with open(out_dir / "steps.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()})

    # ---- report.md
    vary = sc.get("vary", "rate")
    grafana = (f"http://localhost:{env('GRAFANA_PORT', '3000')}/d/pipeline-overview?from="
               f"{int(run['start'] * 1000)}&to={int((run['load_end'] + (run['drain_s'] or 0) + 30) * 1000)}")
    L = [f"# Benchmark `{sc.get('name')}`: {sc.get('title', '')}", "",
         f"*Run {out_dir.name}* · project {facts['project_version']} · "
         f"{facts['host_os']} ({facts['host_arch']}), Docker {facts['docker_cpus']} CPUs / {facts['docker_memory_gib']} GiB", "",
         f"**Question:** {sc.get('question', '')}", "",
         f"**Command:** `python setup.py {' '.join(sys.argv[1:])}`", "", "## Configuration", "",
         "| Setting | Value |", "|---|---|"]
    prod = sc.get("producers", {})
    for key, value in prod.items():
        L.append(f"| producer {key} | {value} |")
    for key in ("SPARK_TRIGGER_INTERVAL", "SPARK_MAX_OFFSETS_PER_TRIGGER", "SPARK_APP_CORES", "SPARK_WORKER_CORES",
                "SPARK_WORKER_CPUS", "SPARK_EXECUTOR_MEMORY", "SPARK_WORKER_MEMORY"):
        L.append(f"| {key} | {env(key) or '(default)'} |")
    L.append(f"| topic partitions | {config['topic_partitions']} |")
    step_values = ", ".join(f"{r['value']:g}" for r in results)
    L.append(f"| steps | {vary} = {step_values}, {sc.get('step_seconds')}s each |")

    L += ["", "## Results per step", "",
          f"| Step | {vary} | Offered/topic | Sink | Produced/s | Spark in/s | Spark processed/s | Batch ms (p95) | "
          "Backlog Δ | Sustained | New files | Avg file KiB |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        for sink in SINKS:
            ok = r[f"{sink}_sustained"]
            L.append(f"| {r['step']} | {r[vary]:g} | {fmt(r['offered_per_topic'])} | {sink} | "
                     f"{fmt(r[f'{sink}_producer_msgs_s'], 1)} | {fmt(r[f'{sink}_spark_input_s'], 1)} | "
                     f"{fmt(r[f'{sink}_spark_processed_s'], 1)} | {fmt(r[f'{sink}_batch_ms_mean'])} "
                     f"({fmt(r[f'{sink}_batch_ms_p95'])}) | {fmt(r[f'{sink}_backlog_growth'])} | "
                     f"{'✅' if ok else ('❌' if ok is False else '–')} | {fmt(r[f'{sink}_new_files'])} | "
                     f"{fmt(r[f'{sink}_avg_new_file_kb'], 1)} |")

    L += ["", "## Sustainable throughput", "",
          "A step is *sustained* when the sink keeps up: its Kafka backlog (messages not yet written to S3) does not "
          "grow during the step and stays under ~10 s of input. Spark's in/processed rates are per-batch gauges and "
          "only indicative.", ""]
    for sink in SINKS:
        ok_steps = [r for r in rows if r[f"{sink}_sustained"]]
        bad_steps = [r for r in rows if r[f"{sink}_sustained"] is False]
        best = max((r[f"{sink}_producer_msgs_s"] or 0 for r in ok_steps), default=None)
        line = f"- **{sink}:** sustained up to **{fmt(best, 1)} msgs/s**" if ok_steps else f"- **{sink}:** no sustained step"
        if bad_steps:
            line += f"; first falls behind at step {bad_steps[0]['step']} ({vary}={bad_steps[0][vary]:g})"
        L.append(line)

    if audit:
        L += ["", "## Latency (from `audit`; p50 / p95 / p99)", "",
              "Queue = event → micro-batch start; end-to-end = event → micro-batch committed to S3. "
              f"Container clock − host clock: {audit.get('clock_offset_ms')} ms.", "",
              "| Step | Sink | Queue | End-to-end |", "|---|---|---|---|"]
        for r in results:
            for sink in SINKS:
                w = (audit.get(sink, {}).get("windows") or {}).get(f"step{r['step']}") or {}
                L.append(f"| {r['step']} | {sink} | {fmt_lat(w.get('queue'))} | {fmt_lat(w.get('end_to_end'))} |")
        L += ["", "## Data accounting", "", "| Sink | In Kafka | Stored in S3 | Duplicates | OK |", "|---|---|---|---|---|"]
        for c in audit["accounting"]:
            L.append(f"| {c['sink']} | {c['in_kafka']} | {c['stored']} | {c['duplicates']} | {'✅' if c['ok'] else '❌'} |")
        L.append("")
        L.append(f"**{'PASS' if audit['pass'] else 'FAIL'}**: every message in Kafka stored exactly once"
                 if audit["pass"] else "**FAIL**: counts differ (see audit.json; was the backlog fully drained?)")
        for sink in SINKS:
            s = audit.get(sink, {})
            if s.get("exists"):
                L.append(f"- {sink}: {s['files']} files, {s['bytes'] / 2**20:.1f} MiB total, "
                         f"avg {s['avg_file_kb']} KiB per file")

    L += ["", "## Resources (whole run)", "", "| Container | CPU cores (mean) | CPU cores (p95) | Memory max MiB |",
          "|---|---|---|---|"]
    cpu = sorted(((k.split(".", 1)[1], v) for k, v in run_metrics.items() if k.startswith("cpu_cores.")),
                 key=lambda kv: -kv[1]["mean"])
    for name, v in cpu:
        memv = run_metrics.get(f"mem_mib.{name}") or {}
        L.append(f"| {name} | {v['mean']:.2f} | {v['p95']:.2f} | {fmt(memv.get('max'))} |")
    if not cpu:
        L.append("| (cAdvisor disabled: no container metrics) | | | |")

    if events:
        L += ["", "## Injected faults", "", "| Step | Fault | Outage | Sinks processing again after restore |", "|---|---|---|---|"]
        for ev in events:
            dur = f"{ev['seconds']:g}s" if ev.get("seconds") is not None else "–"
            L.append(f"| {ev.get('step')} | {ev['action']} {ev.get('target') or ''} | {dur} | "
                     f"{fmt(ev.get('recovery_s'), 1, 's') if ev.get('recovery_s') is not None else 'not recovered'} |")

    produced = {TOPIC_TO_SINK.get(t, t): run["end_offsets"].get(t, 0) - run["start_offsets"].get(t, 0)
                for t in run["end_offsets"]}
    L += ["", "## Run", "",
          f"- Messages added to Kafka during the run: {produced}",
          f"- Start-up until the sinks processed live data: {fmt(run.get('startup_s'), 0, 's')}",
          f"- Drain after load stopped: {fmt(run['drain_s'], 0, 's') if run['drain_s'] is not None else 'did not finish'}",
          f"- Grafana for this run: {grafana}",
          f"- Files: `steps.csv` (all numbers), `config.json` (settings, machine), `audit.json`, `events.json`", "",
          "## Your analysis", "", "_Explain what the numbers show, the bottleneck, and what you would change._", ""]
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")

    cli.print_banner("BENCHMARK COMPLETE")
    for line in L[L.index("## Sustainable throughput") + 4:]:
        if line.startswith("## "):
            break
        if line:
            print("  " + line.replace("**", ""))
    if audit:
        print(f"  Data accounting: {'PASS' if audit['pass'] else 'FAIL'}")
    print(f"\n  📁 Results: {rel}/  (report.md, steps.csv, config.json{', audit.json' if audit else ''})")
    print(f"  📈 Grafana: {grafana}")


def add_bench_arguments(parser, add_producer_options):
    parser.add_argument("scenario", nargs="?", help="scenario name (see: python setup.py bench --list)")
    parser.add_argument("--list", action="store_true", help="list available scenarios")
    parser.add_argument("--label", help="suffix for the results folder, e.g. cores2")
    parser.add_argument("--quick", action="store_true", help="30 s steps, 10 s warm-up (for a trial run)")
    parser.add_argument("--steps", help="comma-separated step values, e.g. 25,50,100,200")
    parser.add_argument("--vary", choices=["rate", "threads"], help="what the steps change")
    parser.add_argument("--step-seconds", type=float)
    parser.add_argument("--warmup", type=float)
    parser.add_argument("--no-audit", action="store_true", help="skip the Spark audit at the end")
    parser.add_argument("--impl", choices=["kp", "ck", "both"])
    add_producer_options(parser)
    g = parser.add_argument_group("spark / kafka overrides (saved to .env, like `setup.py spark`)")
    g.add_argument("--trigger", metavar="INTERVAL")
    g.add_argument("--max-offsets", metavar="N")
    g.add_argument("--cores", type=int, metavar="N")
    g.add_argument("--worker-cores", type=int, metavar="N")
    g.add_argument("--worker-cpus", type=float, metavar="CPUS")
    g.add_argument("--worker-memory", metavar="SIZE")
    g.add_argument("--executor-memory", metavar="SIZE")
    g.add_argument("--partitions", type=int, metavar="N")


def list_scenarios():
    print("Available scenarios (python setup.py bench <name>):")
    for p in sorted(BENCH_DIR.glob("*.json")):
        data = json.loads(p.read_text())
        print(f"  {p.stem:<22} {data.get('title', '')}")
