# cse6332 project_6
CSE 6332 Cloud Project

Use:
	docker compose stop
		&
	docker compose start
	if you run: docker compose down -v
       you will erase most if not all of your data. 
	   Only do this in preparation to clean the project for removal

Quick Start
This is updated over what is in the zip file:

1. Unzip the file.  It will create a sub-directory called project_6   

	unzip project_6.zip -d project_6

2. Change directories into that new sub-directory

	cd project_6

3. Run 'make install', this will gather the various Python modules, project specific jar files and utilities.

	make install

4. You should now be able to start the message producers to begin 'publishing' messages
(you should stop the producers by using the stop parameter when shutting down your testing)

	./manage_producers.sh start
		and/or
    ./manage_producers_ckafka.sh start

6. Finally, after a minute or so, the following script should return without errors and give an indication of the entire process functioning.

	./pipeline_monitor.sh
7. !!!IMPORTANT!!!
	Prior to stopping the containers
	Manually stop the producers or use:
	make stop
	which runs ./stack_shutdown.sh

8. Restart without all the initial install/startup process,
	Just: make run

You will always need to manually run the manage_producers*.sh scripts.


Some things to exammine:
Prometheus:
	http://localhost:9090/targets
	all targets should be green if all the producers are running. Only the producers not running
	sould be red if not. the "check_services.sh" script does a similar check

Minio:
	http://localhost:9001
	username:password = minioadmin:minioadmin
	You should see a bucket named 'spark-output' with 'json' and 
	'parquet' directories if you ran the producers at least once.

Spark:
	The spark master at http://localhost:8080
	the spark worker at http://localhost:8081
	the json job executor at http://localhost:4041 (the actual 'consumer')
		the tab 'Structured Streaming' at http://localhost:4041/StreamingQuery/
		will have a 'Run ID' you can select to see some graphs showing various
		rates
	the parquet executor at http://localhost:4042  (the actual 'consumer')
		this will have simmilar information as above

Grafana:
	Grafana is a Dashboard which reads metrics data from Prometheus
	and can be used to display, monitor, and provide alerts based on 
	the configuration and customization.
	http://localhost:3000
	the default username:password is admin:admin but we have replaced the 
	default grafana.db with one that has some dashboards installed.
	the username:password is admin:admin

The prometheus endpoints are managed in the prometheus.yml file.

Required file and directory permissions are handled by 'make install' 
script which invokes the download_spark_kafka_jars.sh script. And 
the containers, volumes, and ports are all managed by docker-commpose.yml

Avoid using docker compose down -v as it will wipe out the minio data
End of Quick Start

docker compose ps --services will list the following:
  cadvisor
  grafana
  kafka
  kafka-exporter
  minio
  prometheus
  spark-consumer-json
  spark-consumer-parquet
  spark-master
  spark-worker
  zookeeper
  zookeeper-exporter

- **Kafka** – Distributed, fault-tolerant message broker for streaming data between producers and consumers in real time.
- **MinIO** – High-performance, S3-compatible object storage for saving and retrieving data.
- **Prometheus** – Metrics collection and time-series database for monitoring services and infrastructure.
- **cAdvisor** – Container resource usage and performance monitoring tool exposing metrics for Prometheus.
- **Spark Master** – Central coordinator that manages cluster resources and schedules Spark jobs.
- **Spark Worker** – Executes tasks assigned by the Spark Master as part of distributed processing.
- **Spark Consumers** – Structured Streaming jobs that consume data (e.g., from Kafka) and process it in Spark.
- **Zookeeper** – Coordination service for maintaining Kafka’s metadata, leader election, and cluster state.
- **Exporters (Kafka, Node, etc.)** – Translate application or system metrics into Prometheus-compatible format for monitoring.

