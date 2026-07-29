# workshop-provision

Docker-based provisioning setup for the CougarAI workshop environment. This image defines the OS configuration and resource parameters used by the workshop instance, accessible at [workshop.cougarai.org](https://workshop.cougarai.org).

## Overview

`workshop-provision` builds and configures the container environment that powers the CougarAI workshop. It handles OS-level setup, dependency installation, and resource allocation so the workshop environment is consistent and reproducible for every participant/session.

## What's Inside

- `Dockerfile` — base image, OS packages, and environment setup
- `scripts/` — provisioning and setup scripts run at build/start time

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)
- Access credentials for `workshop.cougarai.org` (if required)

## Quick Start

Clone the repository:

```bash
git clone https://github.com/Cougar-AI/workshop-provision.git
cd workshop-provision
```

Build the image:

```bash
docker build -t cougarai/workshop-provision .
```

Run the container:

```bash
docker run -it --rm \
  -p 8080:8080 \
  cougarai/workshop-provision
```

Or, using Docker Compose:

```bash
docker compose up --build
```

## Configuration
Refer to .env.example. Stricly on the CougarAI server only adjust to your application.
```
GUAC_URL=""
GUAC_DATASOURCE=""
GUAC_ADMIN_USER=""
GUAC_ADMIN_PASS=""

#adjust port to application to Docker File
START_PORT=
WORKSHOP_HOST=""

#Must share as Docker File and provision.sh
RDP_USERNAME=""
RDP_PASSWORD=""

CONNECTION_GROUP_NAME=""
STUDENT_PASSWORD=""

SCRIPTS_DIR=""
REQUIREMENTS_PATH=""

DOCKER_FILE=""
DOCKER_IMAGE=""

#Must generate your own if own your local machine.
API_KEY=""

#Directory for /scripts
PROVISION_SCRIPT=""
TEARDOWN_SCRIPT=""
RESET_SCRIPT=""
```
*(fill in the actual environment variables / params your image exposes)*

## Starting the application
Once you adjust the .env to your settings. navigate to manually run the script.
```
cd workshop-provision/scripts

#Give user permissions in linux.
sudo chmod +x workshop-provision.sh reset_environments teardown

./provision_workshop.sh
Enter the amount of containers to create: 1
```

## Accessing the Workshop
After completion, the script will generate the following
```
Containers running: workshop-1 through workshop-1
Ports used: Starting_Port through End Port
Connection group: Workshop Pool
Student accounts: username1 through username1, password: password

```
Once running, the environment is available at:

```
https://workshop.cougarai.org

Enter the credentials 
```
## CougarAI Website & Workshop-Provision
If you want to run the workshop_api.py and cougarai website together and send requests. You must refer to the CougarAI Revamp Website repo. Grab the API key you generated and workshop url to the website .env. 

To get the server of the workshop run:
```
python ./workshop_api.py
```
## API scripts
To verify that the website server and workshop are working together:

```
#Status Check
curl -s -H "X-API-Key: $WORKSHOP_API_KEY" "$WORKSHOP_API_URL/admin/workshops/status" | jq
```

```
#Auth Check
curl -s -H "X-API-Key: wrong-key" "$WORKSHOP_API_URL/admin/workshops/status"
```

```
#Gets the current requirements
curl -s -H "X-API-Key: $WORKSHOP_API_KEY" "$WORKSHOP_API_URL/admin/workshops/requirements" | jq
```

```
#Preview Diff Requirements
curl -s -X POST \
  -H "X-API-Key: $WORKSHOP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"packages": ["flask==2.3", "requests"]}' \
  "$WORKSHOP_API_URL/admin/workshops/requirements/preview" | jq
```

```
#Provisioning 1 container
curl -s -X POST \
  -H "X-API-Key: $WORKSHOP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"num_containers": 1}' \
  "$WORKSHOP_API_URL/admin/workshops/provision" | jq
```

```
#Job Status grab it from any of the scripts
curl -s -H "X-API-Key: $WORKSHOP_API_KEY" "$WORKSHOP_API_URL/admin/workshops/jobs/<job_id_CHANGE_ME>" | jq
```

```
#Reset
curl -s -X POST \
  -H "X-API-Key: $WORKSHOP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"num_containers": 1}' \
  "$WORKSHOP_API_URL/admin/workshops/reset" | jq
```

```
#Teardown
curl -s -X POST \
  -H "X-API-Key: $WORKSHOP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"num_containers": 1}' \
  "$WORKSHOP_API_URL/admin/workshops/teardown" | jq
```
