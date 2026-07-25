# workshop-provision

Docker-based provisioning setup for the CougarAI workshop environment. This image defines the OS configuration and resource parameters used by the workshop instance, accessible at [workshop.cougarai.org](https://workshop.cougarai.org).

## Overview

`workshop-provision` builds and configures the container environment that powers the CougarAI workshop. It handles OS-level setup, dependency installation, and resource allocation so the workshop environment is consistent and reproducible for every participant/session.

## What's Inside

- `Dockerfile` — base image, OS packages, and environment setup
- `scripts/` — provisioning and setup scripts run at build/start time

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (version X.X+)
- [Docker Compose](https://docs.docker.com/compose/install/) (if used)
- Access credentials for `workshop.cougarai.org` (if required)

## Quick Start

Clone the repository:

```bash
git clone https://github.com/<org>/workshop-provision.git
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

| Variable | Description | Default |
|---|---|---|
| `EXAMPLE_VAR` | What this controls | `value` |

*(fill in the actual environment variables / params your image exposes)*

## Resource Parameters

Briefly describe what OS/resource settings this image enforces (CPU/memory limits, base OS version, etc.) so users know what to expect before running it.

## Accessing the Workshop

Once running, the environment is available at:

```
https://workshop.cougarai.org
```

*(add any login/auth notes here)*

## Project Structure

```
workshop-provision/
├── Dockerfile
├── scripts/
└── README.md
```
