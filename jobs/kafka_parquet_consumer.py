import os

from pyspark.sql import SparkSession
from pyspark.sql.functions import from_json, col, to_date, hour, to_timestamp, current_timestamp
from pyspark.sql.types import StructType, StringType, IntegerType, BooleanType

# Connection settings come from the container environment (.env via docker compose)
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:29092")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://seaweedfs:8333")
S3_ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["S3_SECRET_KEY"]
S3_BUCKET = os.environ.get("S3_BUCKET", "spark-output")
CHECKPOINT_ROOT = os.environ.get("CHECKPOINT_ROOT", "/opt/spark/work-dir/checkpoints")
# Tuning knobs, set with `python setup.py spark --trigger 10s --max-offsets 5000`
# (empty or "off" = Spark default: next micro-batch as soon as the previous ends, no rate limit)
TRIGGER = os.environ.get("SPARK_TRIGGER_INTERVAL", "").strip()
MAX_OFFSETS = os.environ.get("SPARK_MAX_OFFSETS_PER_TRIGGER", "").strip()

TOPIC = "topic-parq"
SINK = "topic-parq_sink"

# Define the schema for IoT data
schema = StructType() \
    .add("msg_id", StringType()) \
    .add("device_id", StringType()) \
    .add("battery_level", IntegerType()) \
    .add("motion_detected", BooleanType()) \
    .add("timestamp", StringType()) \
    .add("payload", StringType())

# Build Spark session with S3A config for the S3 store
spark = SparkSession.builder \
    .appName("KafkaParquetToS3") \
    .config("spark.hadoop.fs.s3a.access.key", S3_ACCESS_KEY) \
    .config("spark.hadoop.fs.s3a.secret.key", S3_SECRET_KEY) \
    .config("spark.hadoop.fs.s3a.endpoint", S3_ENDPOINT) \
    .config("spark.hadoop.fs.s3a.path.style.access", "true") \
    .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false") \
    .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
    .getOrCreate()

# Read Kafka stream. "earliest" only applies to a brand-new checkpoint, so a new
# sink stores everything in the topic and sent = stored can be checked exactly.
reader = spark.readStream.format("kafka") \
    .option("kafka.bootstrap.servers", KAFKA_BOOTSTRAP) \
    .option("subscribe", TOPIC) \
    .option("startingOffsets", "earliest") \
    .option("failOnDataLoss", "false")
if MAX_OFFSETS and MAX_OFFSETS.lower() != "off":
    reader = reader.option("maxOffsetsPerTrigger", int(MAX_OFFSETS))
df = reader.load()

# Parse JSON payload and add date/hour for partitioning. The producer's UTC
# ("...Z") timestamp becomes an instant; date/hour are then taken in the
# session timezone (spark.sql.session.timeZone = TZ), i.e. local time.
json_df = df.select(from_json(col("value").cast("string"), schema).alias("data")) \
    .select("data.*") \
    .withColumn("timestamp", to_timestamp(col("timestamp"))) \
    .withColumn("date", to_date(col("timestamp"))) \
    .withColumn("hour", hour(col("timestamp"))) \
    .withColumn("processed_at", current_timestamp())  # micro-batch start; used for latency analysis

# Write to S3 as partitioned Parquet
writer = json_df.writeStream \
    .format("parquet") \
    .option("path", f"s3a://{S3_BUCKET}/parquet") \
    .option("checkpointLocation", f"{CHECKPOINT_ROOT}/{SINK}") \
    .partitionBy("date", "hour") \
    .outputMode("append") \
    .queryName(SINK)
if TRIGGER and TRIGGER.lower() != "off":
    writer = writer.trigger(processingTime=TRIGGER)
writer.start().awaitTermination()
