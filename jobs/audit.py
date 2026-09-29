"""
Audit what the streaming sinks wrote to S3: row counts, duplicates, files and latency.

Run with `python setup.py audit` (setup.py runs it in the spark-master container in
local mode, so it doesn't compete with the streaming consumers for cluster cores).

Latency is measured against the producer's event timestamp:
  queue latency       = processed_at (micro-batch start)          - event timestamp
  end-to-end latency  = micro-batch commit time (files are in S3) - event timestamp
Commit times come from the sink's checkpoint (millisecond precision), copied in by
setup.py as /tmp/commits_<sink>.json; S3 file times only have 1 s resolution.

Prints one line starting with AUDIT_JSON followed by the result as JSON.
"""
import argparse
import json
import os
import sys

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

parser = argparse.ArgumentParser()
parser.add_argument("--commits-dir", default="/tmp", help="folder with commits_<sink>.json")
parser.add_argument("--windows", default="[]",
                    help='JSON list of {"name": str, "start": epoch_s, "end": epoch_s} for per-window latency')
args = parser.parse_args()
windows = json.loads(args.windows)

S3_BUCKET = os.environ.get("S3_BUCKET", "spark-output")
spark = SparkSession.builder \
    .appName("PipelineAudit") \
    .config("spark.hadoop.fs.s3a.access.key", os.environ["S3_ACCESS_KEY"]) \
    .config("spark.hadoop.fs.s3a.secret.key", os.environ["S3_SECRET_KEY"]) \
    .config("spark.hadoop.fs.s3a.endpoint", os.environ.get("S3_ENDPOINT", "http://seaweedfs:8333")) \
    .config("spark.hadoop.fs.s3a.path.style.access", "true") \
    .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false") \
    .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
    .config("spark.sql.session.timeZone", os.environ.get("TZ", "UTC")) \
    .getOrCreate()
spark.sparkContext.setLogLevel("ERROR")


def percentiles(df, column):
    row = df.select(F.percentile_approx(column, [0.5, 0.95, 0.99], 1000).alias("p"),
                    F.count(column).alias("n")).first()
    if not row or not row["n"]:
        return None
    p50, p95, p99 = row["p"]
    return {"p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "p99_ms": round(p99, 1), "rows": row["n"]}


def commit_times(sink):
    """DataFrame (batch_ms, commit_ms) from the checkpoint export, or None."""
    path = os.path.join(args.commits_dir, f"commits_{sink}.json")
    if not os.path.exists(path):
        return None
    pairs = json.load(open(path))
    if not pairs:
        return None
    return spark.createDataFrame([(int(b), int(c)) for b, c in pairs], "batch_ms long, commit_ms long")


def audit_sink(sink):
    path = f"s3a://{S3_BUCKET}/{sink}"
    try:
        reader = spark.read.option("mergeSchema", "true") if sink == "parquet" else spark.read
        # Reading the sink directory uses its _spark_metadata log: only committed files count
        df = reader.format(sink).load(path)
    except Exception as exc:  # no output yet
        return {"exists": False, "error": str(exc).splitlines()[0][:200]}

    for name in ("msg_id", "processed_at"):
        if name not in df.columns:
            df = df.withColumn(name, F.lit(None).cast("string"))
    df = df.select(
        "msg_id",
        F.col("timestamp").cast("timestamp").alias("event_ts"),
        F.col("processed_at").cast("timestamp").alias("processed_at"),
        F.col("_metadata.file_modification_time").alias("file_ts"),
        F.col("_metadata.file_path").alias("file_path"),
        F.col("_metadata.file_size").alias("file_size"),
    ).cache()

    counts = df.agg(
        F.count(F.lit(1)).alias("rows"),
        F.count("msg_id").alias("rows_with_id"),
        F.countDistinct("msg_id").alias("distinct_ids"),
    ).first()
    files = df.select("file_path", "file_size").distinct().agg(
        F.count(F.lit(1)).alias("files"), F.sum("file_size").alias("bytes")).first()

    lat = df.withColumn("queue_ms", (F.col("processed_at").cast("double") - F.col("event_ts").cast("double")) * 1000) \
            .withColumn("batch_ms", F.round(F.col("processed_at").cast("double") * 1000).cast("long"))
    commits = commit_times(sink)
    if commits is not None:
        lat = lat.join(F.broadcast(commits), "batch_ms", "left") \
                 .withColumn("e2e_ms", F.col("commit_ms") - F.col("event_ts").cast("double") * 1000)
    else:
        lat = lat.withColumn("e2e_ms", F.lit(None).cast("double"))

    result = {
        "exists": True,
        "rows": counts["rows"],
        "rows_with_msg_id": counts["rows_with_id"],
        "duplicates": counts["rows_with_id"] - counts["distinct_ids"],
        "files": files["files"],
        "bytes": files["bytes"] or 0,
        "avg_file_kb": round((files["bytes"] or 0) / files["files"] / 1024, 2) if files["files"] else 0,
        "latency": {"queue": percentiles(lat, "queue_ms"), "end_to_end": percentiles(lat, "e2e_ms")},
        "windows": {},
    }
    for w in windows:
        part = lat.where((F.col("event_ts") >= F.lit(float(w["start"])).cast("timestamp")) &
                         (F.col("event_ts") < F.lit(float(w["end"])).cast("timestamp")))
        result["windows"][w["name"]] = {"queue": percentiles(part, "queue_ms"),
                                        "end_to_end": percentiles(part, "e2e_ms")}
    df.unpersist()
    return result


output = {"json": audit_sink("json"), "parquet": audit_sink("parquet")}
print("AUDIT_JSON " + json.dumps(output), flush=True)
spark.stop()
sys.exit(0)
