"""
Compaction exercise (research avenue C: the small-files problem).

Reads one date/hour partition of a streaming sink, rewrites it as a few large
files under s3a://<bucket>/compacted/<sink>/date=.../hour=..., and compares file
count, size and read time before and after. The original data is not changed.

Run with `python setup.py compact [--sink parquet] [--date D --hour H] [--files N]`.
Prints one line starting with COMPACT_JSON followed by the result as JSON.
"""
import argparse
import json
import os
import time

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

parser = argparse.ArgumentParser()
parser.add_argument("--sink", choices=["json", "parquet"], default="parquet")
parser.add_argument("--date", required=True, help="partition date, e.g. 2026-09-26")
parser.add_argument("--hour", required=True, type=int, help="partition hour, 0-23")
parser.add_argument("--files", type=int, default=1, help="number of output files")
args = parser.parse_args()

S3_BUCKET = os.environ.get("S3_BUCKET", "spark-output")
spark = SparkSession.builder \
    .appName("CompactPartition") \
    .config("spark.hadoop.fs.s3a.access.key", os.environ["S3_ACCESS_KEY"]) \
    .config("spark.hadoop.fs.s3a.secret.key", os.environ["S3_SECRET_KEY"]) \
    .config("spark.hadoop.fs.s3a.endpoint", os.environ.get("S3_ENDPOINT", "http://seaweedfs:8333")) \
    .config("spark.hadoop.fs.s3a.path.style.access", "true") \
    .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false") \
    .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
    .config("spark.sql.session.timeZone", os.environ.get("TZ", "UTC")) \
    .getOrCreate()
spark.sparkContext.setLogLevel("ERROR")


def describe(path, fmt):
    """Files, bytes, rows and a cold-ish read time for a directory of fmt files."""
    df = spark.read.format(fmt).load(path)
    start = time.time()
    rows = df.count()
    read_s = time.time() - start
    files = df.select(F.col("_metadata.file_path").alias("f"), F.col("_metadata.file_size").alias("s")).distinct()
    agg = files.agg(F.count(F.lit(1)).alias("files"), F.sum("s").alias("bytes")).first()
    return {"files": agg["files"], "bytes": agg["bytes"] or 0, "rows": rows, "read_seconds": round(read_s, 3)}


source = f"s3a://{S3_BUCKET}/{args.sink}/date={args.date}/hour={args.hour}"
target = f"s3a://{S3_BUCKET}/compacted/{args.sink}/date={args.date}/hour={args.hour}"

before = describe(source, args.sink)
start = time.time()
spark.read.format(args.sink).load(source).coalesce(args.files) \
    .write.mode("overwrite").format(args.sink).save(target)
write_s = time.time() - start
after = describe(target, args.sink)

print("COMPACT_JSON " + json.dumps({
    "sink": args.sink, "partition": f"date={args.date}/hour={args.hour}", "target": target,
    "before": before, "after": after, "compaction_seconds": round(write_s, 3),
}), flush=True)
spark.stop()
