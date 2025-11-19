#!/bin/bash
download_if_missing() {
  local url=$1
  local dest_dir=$2
  local filename=$(basename "$url")
  local filepath="$dest_dir/$filename"

  if [ -f "$filepath" ]; then
    echo "✔️  $filename already exists, skipping."
  else
    echo "⬇️  Downloading $filename..."
    wget "$url" -P "$dest_dir" && echo "✅ $filename downloaded."
  fi
}

download_if_missing https://rosenkrantzt.utasites.cloud/CSE6332/project_6/images.zip .

unzip images.zip

docker load -i bitnami-spark.tar
docker tag f89f5d170f07 bitnami/spark:3.5.5
docker load -i bitnami-kafka.tar
docker tag 287cec8bf1b2 bitnami/kafka:3.6
docker load -i bitnami-zookeeper.tar
docker tag c2cfdb8592ba bitnami/zookeeper:3.9
