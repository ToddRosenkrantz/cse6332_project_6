# ----------------------------
# Makefile for Kafka/Spark stack
# ----------------------------
# Detect OS and architecture
OS := $(shell uname -s | tr '[:upper:]' '[:lower:]')
ARCH := $(shell uname -m)

# Normalize architecture names
ifeq ($(ARCH),x86_64)
    ARCH := amd64
endif
ifeq ($(ARCH),aarch64)
    ARCH := arm64
endif

# Base URL for MinIO client
MC_BASE_URL := https://dl.min.io/client/mc/release

# Compute download URL
MC_URL := $(MC_BASE_URL)/$(OS)-$(ARCH)/mc

# Install path
BIN_DIR := .
MC_BIN := $(BIN_DIR)/mc

.PHONY: graphana_restore
.PHONY: install-mc
install-mc:


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

	@echo "Detected OS: $(OS), ARCH: $(ARCH)"
	@echo "Downloading mc from $(MC_URL)"
	curl -fsSL $(MC_URL) -o $(MC_BIN)
	chmod +x $(MC_BIN)
	@echo "✅ Installed mc at $(MC_BIN)"

	@echo "> Starting Docker containers..."
	docker compose up -d

	@echo "> Running Zookeeper setup..."
	docker compose stop zookeeper
	docker cp ./zoo.cfg zookeeper:/opt/bitnami/zookeeper/conf/zoo.cfg
	docker compose start zookeeper

	@echo "> Running Grafana setup..."
	@$(MAKE) graphana_restore

graphana_restore:
	docker compose exec grafana kill -TERM 1
	docker cp grafana.db grafana:/var/lib/grafana/grafana.db
	docker compose run --rm -u root --entrypoint /bin/bash grafana -c "chown grafana:root /var/lib/grafana/grafana.db"
#	docker exec -u root grafana /bin/bash -c "chown 472:0 /var/lib/grafana/grafana.db"
#	docker exec -u root grafana /bin/bash -c "chmod 640 /var/lib/grafana/grafana.db"
	docker compose restart grafana
	sleep 5
	docker exec grafana grafana cli admin reset-admin-password admin
