#!/bin/bash
# Create 'spark-output' bucket in MinIO
MC_CMD="./minio_cli"

# Set alias and create bucket
$MC_CMD alias set local http://localhost:9000 minioadmin minioadmin

# Create bucket (ignore error if it already exists)
$MC_CMD mb local/spark-output || echo "Bucket 'spark-output' already exists or failed to create."
