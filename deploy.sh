#!/bin/bash

set -euo pipefail
set -o allexport
. ./mongo-rs.env
set +o allexport

# Use to avoid subnet overlaps - specify your own subnet
if ! docker network inspect "$BACKEND_NETWORK_NAME" >/dev/null 2>&1; then
	docker network create $BACKEND_NETWORK_OPTIONS -d overlay --subnet="$BACKEND_NETWORK_SUBNET" --gateway="$BACKEND_NETWORK_GATEWAY" "$BACKEND_NETWORK_NAME"
fi
# docker network create --opt encrypted -d overlay ${BACKEND_NETWORK_NAME}

if ! docker secret inspect mongo-keyfile >/dev/null 2>&1; then
	openssl rand -base64 741 | docker secret create mongo-keyfile -
fi

mongo_root_secret="${STACK_NAME}_mongo_root_password"
initdb_secret="${STACK_NAME}_initdb_password"
mongo_client_secret="${STACK_NAME}_mongo_client_url"
mongo_root_password=''

if ! docker secret inspect "$mongo_root_secret" >/dev/null 2>&1 || ! docker secret inspect "$mongo_client_secret" >/dev/null 2>&1; then
	if [[ ! -t 0 ]]; then
		echo "Mongo root password secret is missing; run deploy.sh interactively to create it." >&2
		exit 1
	fi
	read -r -s -p "Mongo root password (stored as a Swarm secret): " mongo_root_password
	printf '\n' >&2
	if [[ -z "$mongo_root_password" ]]; then
		echo "Mongo root password must not be empty." >&2
		exit 1
	fi
fi

if ! docker secret inspect "$mongo_root_secret" >/dev/null 2>&1; then
	printf '%s' "$mongo_root_password" | docker secret create "$mongo_root_secret" - >/dev/null
fi
if ! docker secret inspect "$mongo_client_secret" >/dev/null 2>&1; then
	printf '%s' "$mongo_root_password" | python3 -c 'import os, sys; from urllib.parse import quote; user = quote(os.environ["MONGO_ROOT_USERNAME"], safe=""); password = quote(sys.stdin.read(), safe=""); replica_set = quote(os.environ["REPLICASET_NAME"], safe=""); print(f"mongodb://{user}:{password}@database:27017/?replicaSet={replica_set}", end="")' | docker secret create "$mongo_client_secret" - >/dev/null
fi
unset mongo_root_password

if ! docker secret inspect "$initdb_secret" >/dev/null 2>&1; then
	if [[ ! -t 0 ]]; then
		echo "Initial database password secret is missing; run deploy.sh interactively to create it." >&2
		exit 1
	fi
	read -r -s -p "Initial database user password (stored as a Swarm secret): " initdb_password
	printf '\n' >&2
	if [[ -z "$initdb_password" ]]; then
		echo "Initial database user password must not be empty." >&2
		exit 1
	fi
	printf '%s' "$initdb_password" | docker secret create "$initdb_secret" - >/dev/null
	unset initdb_password
fi

docker stack deploy -c docker-compose-stack.yml "$STACK_NAME"
