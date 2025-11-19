# ----------------------------
# Makefile for Kafka/Spark stack
# ----------------------------

run:
	docker compose start

stop:
	@echo "🔐 Running clean shutdown of producers..."
	./stack_shutdown.sh

logs:
	docker compose logs -f --tail=50

open:
	@echo "Grafana:     http://localhost:3000"
	@echo "MinIO:       http://localhost:9001"
	@echo "Prometheus:  http://localhost:9090"

install:
	@echo "> Installing Python dependencies..."
	pip3 install -r requirements.txt || true

	@echo "> Downloading required Spark/Kafka JARs..."
	./download_spark_kafka_jars.sh || true

	@echo "> Installing MinIO client (mc)..."
	[ -f ./mc ] || (wget -q https://dl.min.io/client/mc/release/linux-amd64/mc && chmod +x mc)

	@echo "> Downloading and installing some Docker images which are no longer freely available..."
	./download_images.sh

	@echo "> Starting Docker containers..."
	docker compose up -d

	@echo "> Running first-run setup scripts..."
	./create_kafka_topic.sh
	./create_minio_bucket.sh
	@echo "> Running Grafana setup..."
	docker compose stop zookeeper
	docker cp ./zoo.cfg zookeeper:/opt/bitnami/zookeeper/conf/zoo.cfg
	docker compose start zookeeper
	docker compose stop grafana
	docker cp ./grafana_export/grafana.db grafana:/var/lib/grafana/grafana.db
	docker cp ./grafana_export/provisioning/dashboards grafana:/etc/grafana/provisioning/dashboards
	docker cp ./grafana_export/provisioning/datasources grafana:/etc/grafana/provisioning/datasources
	docker cp ./grafana_export/plugins grafana:/var/lib/grafana/plugins
	docker start grafana
	docker exec grafana grafana cli admin reset-admin-password admin
	@echo "> Setup complete. Access Grafana at http://localhost:3000 (admin/admin)."
