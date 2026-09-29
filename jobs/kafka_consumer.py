from pyspark.sql import SparkSession

spark = SparkSession.builder \
    .appName("KafkaConsumerApp") \
    .getOrCreate()

# Read from Kafka topic
df = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "kafka:29092") \
    .option("subscribe", "test-topic") \
    .option("startingOffsets", "latest") \
    .option("kafka.group.id", "spark-consumers") \
    .load()

# Convert binary key/value to strings
parsed_df = df.selectExpr(
    "CAST(key AS STRING) as key",
    "CAST(value AS STRING) as value",
    "timestamp"
)

# Output to console
query = parsed_df.writeStream \
    .format("console") \
    .outputMode("append") \
    .option("truncate", False) \
    .start()

query.awaitTermination()
