#!/bin/bash

# Define the PID files for the producers
JSON_PID="producer_json.pid"
PARQ_PID="producer_parq.pid"
JSON_CK_PID="producer_json_ck.pid"
PARQ_CK_PID="producer_parq_ck.pid"

# Function to stop Kafka producers by running the appropriate shutdown script
stop_producer() {
    local stop_script=$1
    echo "Stopping both Kafka producers using $stop_script script..."
    ./$stop_script stop  # Run the script with the "stop" argument
#    sleep 10  # Wait for the producers to shut down gracefully
#    echo "Both Kafka producers stopped."
}

# Step 1: Initialize a flag to track if any PID file action was taken
action_taken=false

# If either JSON_PID or PARQ_PID exists, use manage_producers.sh to stop the producers
if [ -f "$JSON_PID" ] || [ -f "$PARQ_PID" ]; then
    stop_producer "manage_producers.sh"
    action_taken=true
fi

#step 2
# If either JSON_CK_PID or PARQ_CK_PID exists, use manage_producers_ckafka.sh to stop the producers
if [ -f "$JSON_CK_PID" ] || [ -f "$PARQ_CK_PID" ]; then
    stop_producer "manage_producers_ckafka.sh"
    action_taken=true
fi

# If no action was taken (i.e., no PID files found), print a message indicating no producers were found
if ! $action_taken; then
    echo "No producer PID file found, skipping producer shutdown."
else
    sleep 10
    echo "Producers shutdown process completed."
fi

# Step 3: Stop the Spark consumer (Kafka consumer in Docker)
echo "Stopping Spark consumer (Kafka consumer)..."
docker compose stop spark-consumer-json     # Stop the Spark json consumer container
docker compose stop spark-consumer-parquet  # Stop the Spark parquet consumer container

# Optionally, bring down the Docker containers
echo "Bringing down Docker containers..."
docker compose stop

echo "Shutdown completed: Producers and consumers stopped."
