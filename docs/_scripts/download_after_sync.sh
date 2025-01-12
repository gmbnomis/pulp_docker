#!/usr/bin/env bash

DOCKER_TAG='manifest_a'

echo "Setting REGISTRY_PATH, which can be used directly with the Docker Client."
export REGISTRY_PATH=$(http $BASE_ADDR$DISTRIBUTION_HREF | jq -r '.registry_path')

echo "Next we pull the image from pulp and run it."
echo "$REGISTRY_PATH:$DOCKER_TAG"
sudo docker run $REGISTRY_PATH:$DOCKER_TAG
