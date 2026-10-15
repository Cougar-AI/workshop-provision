#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../.env"

DOCKER_IMAGE="workshop-desktop:latest"

#Default values but can be overrident with --containers or --students

NUM_CONTAINERS=$num_containers
NUM_STUDENTS=$num_containers
# By default containers that are already running are left alone, so adding VMs
# doesn't kick connected students. --recreate (used by the full pipeline after
# an image rebuild) replaces every container with a fresh one.
RECREATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --recreate)
      RECREATE=true
      shift
      ;;
    --containers)
      NUM_CONTAINERS="$2"
      shift 2
      ;;
    --students)
      NUM_STUDENTS="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--containers N] [--students N] [--recreate]"
      echo "  --containers N   Number of desktop containers to provision"
      echo "  --recreate       Replace containers that are already running (default: keep them)"
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

if [ -n "$GITHUB_REPO_URL" ]; then
  echo "Using workshop repo: $GITHUB_REPO_URL"
else
  echo "No GITHUB_REPO_URL provided, containers will use the image's default repo."
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
FAILURES=0

# Existing connections in the group, as "name<TAB>identifier" lines, so re-provisioning
# updates them in place instead of failing with "already exists".
EXISTING_CONNECTIONS=$(curl -s "${API}/connectionGroups/${GROUP_ID}/tree?token=${TOKEN}" | python3 -c '
import json, sys
try:
    tree = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for conn in tree.get("childConnections", []) or []:
    print(conn.get("name"), conn.get("identifier"), sep="\t")
')

for i in $(seq 1 $NUM_CONTAINERS); do
  PORT=$((START_PORT + i - 1))
  CONTAINER_NAME="workshop-${i}"

  echo "--- Container $i: $CONTAINER_NAME on port $PORT ---"

  STATE=$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || true)
  if [ "$STATE" = "running" ] && [ "$RECREATE" = false ]; then
    echo "  Already running, leaving it alone (use --recreate to replace it)"
  else
    # Remove any stopped/old container with this name (or a running one when --recreate)
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

    docker run -d \
      --name "$CONTAINER_NAME" \
      -p ${PORT}:3389 \
      --memory="1.3g" \
      --cpus="0.8" \
      --shm-size="512m"\
      -e GITHUB_REPO_URL="${GITHUB_REPO_URL}" \
      "$DOCKER_IMAGE" >/dev/null
    echo "  Container started"
  fi

  CONN_BODY="{
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
    }"

  EXISTING_ID=$(printf '%s\n' "$EXISTING_CONNECTIONS" | awk -F'\t' -v n="$CONTAINER_NAME" '$1 == n { print $2; exit }')
  if [ -n "$EXISTING_ID" ]; then
    # Keep the same connection (and ID) but refresh host/port/credentials.
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "${API}/connections/${EXISTING_ID}?token=${TOKEN}" \
      -H "Content-Type: application/json" -d "$CONN_BODY")
    if [ "$CODE" = "204" ] || [ "$CODE" = "200" ]; then
      echo "  Connection already exists (ID $EXISTING_ID), settings refreshed"
    else
      echo "  ERROR: could not update connection $CONTAINER_NAME (ID $EXISTING_ID), HTTP $CODE"
      FAILURES=$((FAILURES + 1))
    fi
  else
    CONN_RESPONSE=$(curl -s -X POST "${API}/connections?token=${TOKEN}" \
      -H "Content-Type: application/json" -d "$CONN_BODY")
    CONN_ID=$(echo "$CONN_RESPONSE" | grep -o '"identifier":"[^"]*"' | cut -d'"' -f4)
    if [ -n "$CONN_ID" ]; then
      echo "  Connection created (ID $CONN_ID)"
    else
      echo "  ERROR: connection creation failed for $CONTAINER_NAME"
      echo "  Response: $CONN_RESPONSE"
      FAILURES=$((FAILURES + 1))
    fi
  fi
done

echo "=== Step 4: Creating student accounts with access to the pool ==="

for i in $(seq 1 $NUM_STUDENTS); do
  STUDENT_USER="student${i}"
  USER_BODY="{
      \"username\": \"${STUDENT_USER}\",
      \"password\": \"${STUDENT_PASSWORD}\",
      \"attributes\": {}
    }"

  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${API}/users?token=${TOKEN}" \
    -H "Content-Type: application/json" -d "$USER_BODY")
  if [ "$CODE" = "200" ]; then
    echo "  $STUDENT_USER created"
  else
    # Most likely the account already exists: make sure its password matches STUDENT_PASSWORD.
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "${API}/users/${STUDENT_USER}?token=${TOKEN}" \
      -H "Content-Type: application/json" -d "$USER_BODY")
    if [ "$CODE" = "204" ] || [ "$CODE" = "200" ]; then
      echo "  $STUDENT_USER already existed, password reset to STUDENT_PASSWORD"
    else
      echo "  ERROR: could not create or update $STUDENT_USER (HTTP $CODE)"
      FAILURES=$((FAILURES + 1))
      continue
    fi
  fi

  # Grant this user READ access to the balancing group
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PATCH "${API}/users/${STUDENT_USER}/permissions?token=${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "[{
      \"op\": \"add\",
      \"path\": \"/connectionGroupPermissions/${GROUP_ID}\",
      \"value\": \"READ\"
    }]")
  if [ "$CODE" = "204" ] || [ "$CODE" = "200" ]; then
    echo "  $STUDENT_USER can access '${CONNECTION_GROUP_NAME}'"
  else
    echo "  ERROR: could not grant $STUDENT_USER access to '${CONNECTION_GROUP_NAME}' (HTTP $CODE)"
    FAILURES=$((FAILURES + 1))
  fi
done

if [ "$FAILURES" -gt 0 ]; then
  echo ""
  echo "=== FAILED: $FAILURES Guacamole step(s) failed — see ERROR lines above ===" >&2
  exit 1
fi

echo ""
echo "=== DONE ==="
echo "Containers running: workshop-1 through workshop-${NUM_CONTAINERS}"
echo "Ports used: ${START_PORT} through $((START_PORT + NUM_CONTAINERS - 1))"
echo "Connection group: ${CONNECTION_GROUP_NAME}"
echo "Student accounts: student1 through student${NUM_STUDENTS}, password: ${STUDENT_PASSWORD}"
echo ""
echo "Each student should log into Guacamole and click '${CONNECTION_GROUP_NAME}'"
echo "— they'll be auto-routed to whichever container is free."
