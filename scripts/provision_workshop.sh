#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../.env"

DOCKER_IMAGE="workshop-desktop:latest"

#Default values but can be overrident with --containers or --students

NUM_CONTAINERS=$num_containers
NUM_STUDENTS=$num_containers

while [[ $# -gt 0 ]]; do
  case "$1" in
    --containers)
      NUM_CONTAINERS="$2"
      shift 2
      ;;
    --students)
      NUM_STUDENTS="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--containers N] [--students N]"
      echo "  --containers N   Number of desktop containers to provision"
      echo "  --students N     Number of student accounts to create (default: same as --containers)"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 [--containers N] [--students N]"
      exit 1
      ;;
  esac
done

if [ -z "$NUM_CONTAINERS" ]; then
  if [ -t 0 ]; then
    read -p "Enter the amount of containers to create: " NUM_CONTAINERS
  else
    echo "ERROR: --containers N is required when running non-interactively." >&2
    exit 1
  fi
fi

# Students default to containers if not explicitly set.
if [ -z "$NUM_STUDENTS" ]; then
  NUM_STUDENTS="$NUM_CONTAINERS"
fi

if ! [[ "$NUM_CONTAINERS" =~ ^[0-9]+$ ]] || [ "$NUM_CONTAINERS" -eq 0 ]; then
  echo "ERROR: --containers must be a positive integer, got: '$NUM_CONTAINERS'" >&2
  exit 1
fi

echo "=== Step 1: Authenticating to Guacamole API ==="
TOKEN=$(curl -s -X POST "${GUAC_URL}/api/tokens" \
  --data-urlencode "username=${GUAC_ADMIN_USER}" \
  --data-urlencode "password=${GUAC_ADMIN_PASS}" \
  | grep -o '"authToken":"[^"]*"' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
  echo "ERROR: Failed to authenticate. Check GUAC_ADMIN_USER/PASS and GUAC_URL."
  exit 1
fi
echo "Got auth token."

API="${GUAC_URL}/api/session/data/${GUAC_DATASOURCE}"

echo "=== Step 2: Creating (or finding) the Balancing Connection Group ==="
GROUP_RESPONSE=$(curl -s -X POST "${API}/connectionGroups?token=${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"parentIdentifier\": \"ROOT\",
    \"name\": \"${CONNECTION_GROUP_NAME}\",
    \"type\": \"BALANCING\",
    \"attributes\": {}
  }")

GROUP_ID=$(echo "$GROUP_RESPONSE" | grep -o '"identifier":"[^"]*"' | cut -d'"' -f4)

if [ -z "$GROUP_ID" ]; then
  if echo "$GROUP_RESPONSE" | grep -q "already exists"; then
    echo "Group already exists, looking up its ID..."
    ALL_GROUPS=$(curl -s -X GET "${API}/connectionGroups?token=${TOKEN}")
    # Find the identifier whose preceding name field matches CONNECTION_GROUP_NAME
    GROUP_ID=$(echo "$ALL_GROUPS" | grep -o "\"identifier\":\"[^\"]*\",\"name\":\"${CONNECTION_GROUP_NAME}\"" | grep -o '"identifier":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [ -z "$GROUP_ID" ]; then
      # Fallback: try alternate JSON key ordering (name before identifier)
      GROUP_ID=$(python3 -c "
import json,sys
data = json.loads('''$ALL_GROUPS''')
for k,v in data.items():
    if v.get('name') == '${CONNECTION_GROUP_NAME}':
        print(v.get('identifier'))
        break
" 2>/dev/null)
    fi
  fi
fi

if [ -z "$GROUP_ID" ]; then
  echo "ERROR: could not create or find the connection group. Response was:"
  echo "$GROUP_RESPONSE"
  exit 1
fi
echo "Using group '${CONNECTION_GROUP_NAME}' with ID ${GROUP_ID}"

echo "=== Step 3: Spinning up containers + creating connections ==="
for i in $(seq 1 $NUM_CONTAINERS); do
  PORT=$((START_PORT + i - 1))
  CONTAINER_NAME="workshop-${i}"

  echo "--- Container $i: $CONTAINER_NAME on port $PORT ---"

  # Remove any pre-existing container with this name
  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

  # Start the container
  docker run -d \
    --name "$CONTAINER_NAME" \
    -p ${PORT}:3389 \
    --memory="1.3g" \
    --cpus="0.8" \
    --shm-size="512m"\
    "$DOCKER_IMAGE"

  # Create the matching Guacamole connection inside the balancing group
  CONN_RESPONSE=$(curl -s -X POST "${API}/connections?token=${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{
      \"parentIdentifier\": \"${GROUP_ID}\",
      \"name\": \"${CONTAINER_NAME}\",
      \"protocol\": \"rdp\",
      \"parameters\": {
        \"hostname\": \"${WORKSHOP_HOST}\",
        \"port\": \"${PORT}\",
        \"username\": \"${RDP_USERNAME}\",
        \"password\": \"${RDP_PASSWORD}\",
        \"security\": \"any\",
        \"ignore-cert\": \"true\"
      },
      \"attributes\": {}
    }")

  CONN_ID=$(echo "$CONN_RESPONSE" | grep -o '"identifier":"[^"]*"' | cut -d'"' -f4)

  if [ -z "$CONN_ID" ]; then
    echo "  WARNING: connection creation may have failed for $CONTAINER_NAME"
    echo "  Response: $CONN_RESPONSE"
  else
    echo "  Connection created (ID $CONN_ID)"
  fi
done

echo "=== Step 4: Creating student accounts with access to the pool ==="

for i in $(seq 1 $NUM_STUDENTS); do
  STUDENT_USER="student${i}"

  echo "--- Creating user: $STUDENT_USER ---"
  curl -s -X POST "${API}/users?token=${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{
      \"username\": \"${STUDENT_USER}\",
      \"password\": \"${STUDENT_PASSWORD}\",
      \"attributes\": {}
    }" > /dev/null

  # Grant this user READ access to the balancing group
  curl -s -X PATCH "${API}/users/${STUDENT_USER}/permissions?token=${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "[{
      \"op\": \"add\",
      \"path\": \"/connectionGroupPermissions/${GROUP_ID}\",
      \"value\": \"READ\"
    }]" > /dev/null

  echo "  $STUDENT_USER created and granted access to '${CONNECTION_GROUP_NAME}'"
done

echo ""
echo "=== DONE ==="
echo "Containers running: workshop-1 through workshop-${NUM_CONTAINERS}"
echo "Ports used: ${START_PORT} through $((START_PORT + NUM_CONTAINERS - 1))"
echo "Connection group: ${CONNECTION_GROUP_NAME}"
echo "Student accounts: student1 through student${NUM_STUDENTS}, password: ${STUDENT_PASSWORD}"
echo ""
echo "Each student should log into Guacamole and click '${CONNECTION_GROUP_NAME}'"
echo "— they'll be auto-routed to whichever container is free."

