# workshop_api.py
"""
Runs on the Docker host, listens on the Tailscale interface (port 5100).
This is the thing WORKSHOP_API_URL in workshop_proxy.py points at.

Responsibilities:
  - authenticate incoming requests via X-API-Key
  - kick off the shell scripts as background jobs, return a job_id immediately
  - track job status/output so the proxy (and bot/dashboard) can poll it
  - handle requirements.txt diffing + optional image rebuild before provisioning
  - report live docker status
"""

import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR"))
REQUIREMENTS_PATH = Path(os.environ.get("REQUIREMENTS_PATH"))
DOCKERFILE_DIR = Path(os.environ.get("DOCKERFILE_DIR"))
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE")
API_KEY = os.environ.get("WORKSHOP_API_KEY")

PROVISION_SCRIPT = SCRIPTS_DIR
TEARDOWN_SCRIPT = SCRIPTS_DIR
RESET_SCRIPT = SCRIPTS_DIR 

# ---------------------------------------------------------------------------
# Job store — in-memory is fine for a single-host workshop tool. If you ever
# need this to survive a restart mid-job, swap this dict for sqlite.
# ---------------------------------------------------------------------------
_jobs_lock = threading.Lock()
_jobs = {}  # job_id -> dict


def _new_job(job_type, args):
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "type": job_type,
            "status": "queued",       # queued | running | succeeded | failed
            "args": args,
            "log": [],
            "exit_code": None,
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
        }
    return job_id


def _update_job(job_id, **fields):
    with _jobs_lock:
        _jobs[job_id].update(fields)


def _append_log(job_id, line):
    with _jobs_lock:
        _jobs[job_id]["log"].append(line)
        # keep memory bounded on noisy runs
        if len(_jobs[job_id]["log"]) > 5000:
            _jobs[job_id]["log"] = _jobs[job_id]["log"][-5000:]


def _run_job(job_id, cmd, cwd=None):
    """Runs a script in a background thread, streaming output into the job log."""
    _update_job(job_id, status="running", started_at=time.time())
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,   # never block on a prompt
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            _append_log(job_id, line.rstrip("\n"))
        proc.wait()
        _update_job(
            job_id,
            status="succeeded" if proc.returncode == 0 else "failed",
            exit_code=proc.returncode,
            finished_at=time.time(),
        )
    except Exception as e:
        _append_log(job_id, f"FATAL: {e}")
        _update_job(job_id, status="failed", exit_code=-1, finished_at=time.time())


def _launch(job_type, cmd, args, cwd=None):
    job_id = _new_job(job_type, args)
    t = threading.Thread(target=_run_job, args=(job_id, cmd), kwargs={"cwd": cwd}, daemon=True)
    t.start()
    return job_id


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.before_request
def check_api_key():
    if not API_KEY:
        return jsonify({"error": "WORKSHOP_API_KEY not configured on this host"}), 500
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401


# ---------------------------------------------------------------------------
# Provisioning lifecycle
# ---------------------------------------------------------------------------
@app.route("/admin/workshops/provision", methods=["POST"])
def provision():
    data = request.get_json(silent=True) or {}
    num_containers = data.get("num_containers")
    if not num_containers:
        return jsonify({"error": "num_containers is required"}), 400

    num_students = data.get("num_students", num_containers)
    packages = data.get("packages")
    force_rebuild = bool(data.get("force_rebuild"))

    # If the caller wants new packages baked in, or an explicit rebuild,
    # do that synchronously-ish as its own job before provisioning — but
    # still return a single job_id the caller can poll end-to-end.
    if packages or force_rebuild:
        if packages:
            REQUIREMENTS_PATH.write_text("\n".join(packages) + "\n")
        cmd_chain = (
            f'docker build -t {DOCKER_IMAGE} {DOCKERFILE_DIR} && '
            f'{PROVISION_SCRIPT} --containers {int(num_containers)} --students {int(num_students)}'
        )
        job_id = _launch(
            "provision",
            ["bash", "-lc", cmd_chain],
            {"num_containers": num_containers, "num_students": num_students,
             "packages": packages, "force_rebuild": force_rebuild},
        )
    else:
        job_id = _launch(
            "provision",
            [str(PROVISION_SCRIPT), "--containers", str(int(num_containers)),
             "--students", str(int(num_students))],
            {"num_containers": num_containers, "num_students": num_students},
        )

    return jsonify({"job_id": job_id}), 202


@app.route("/admin/workshops/teardown", methods=["POST"])
def teardown():
    """Full, permanent removal: containers + Guacamole connections + group + students."""
    data = request.get_json(silent=True) or {}
    num_containers = data.get("num_containers")
    if not num_containers:
        return jsonify({"error": "num_containers is required"}), 400

    job_id = _launch(
        "teardown",
        [str(TEARDOWN_SCRIPT), "--containers", str(int(num_containers)),
         "--students", str(int(num_containers)), "--force"],
        {"num_containers": num_containers},
    )
    return jsonify({"job_id": job_id}), 202


@app.route("/admin/workshops/reset", methods=["POST"])
def reset():
    """Recreates the N containers fresh. Guacamole connections/students untouched."""
    data = request.get_json(silent=True) or {}
    num_containers = data.get("num_containers")
    if not num_containers:
        return jsonify({"error": "num_containers is required"}), 400

    job_id = _launch(
        "reset",
        [str(RESET_SCRIPT), "--containers", str(int(num_containers))],
        {"num_containers": num_containers},
    )
    return jsonify({"job_id": job_id}), 202


# ---------------------------------------------------------------------------
# Job status polling
# ---------------------------------------------------------------------------
@app.route("/admin/workshops/jobs/<job_id>", methods=["GET"])
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(job), 200


# ---------------------------------------------------------------------------
# Requirements management
# ---------------------------------------------------------------------------
@app.route("/admin/workshops/requirements", methods=["GET"])
def get_requirements():
    if not REQUIREMENTS_PATH.exists():
        return jsonify({"packages": []}), 200
    lines = [l.strip() for l in REQUIREMENTS_PATH.read_text().splitlines() if l.strip()]
    return jsonify({"packages": lines}), 200


def _pkg_name(spec):
    """Strips version pins so 'flask==2.3' and 'flask>=2' compare as the same package."""
    return re.split(r"[=<>!~\[]", spec.strip())[0].lower()


@app.route("/admin/workshops/requirements/preview", methods=["POST"])
def preview_requirements():
    data = request.get_json(silent=True) or {}
    incoming = data.get("packages", [])

    current = []
    if REQUIREMENTS_PATH.exists():
        current = [l.strip() for l in REQUIREMENTS_PATH.read_text().splitlines() if l.strip()]

    current_names = {_pkg_name(p): p for p in current}
    incoming_names = {_pkg_name(p): p for p in incoming}

    added = [incoming_names[n] for n in incoming_names if n not in current_names]
    removed = [current_names[n] for n in current_names if n not in incoming_names]
    unchanged = [current_names[n] for n in current_names if n in incoming_names]

    return jsonify({"added": added, "removed": removed, "unchanged": unchanged}), 200


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------
@app.route("/admin/workshops/status", methods=["GET"])
def workshop_status():
    try:
        ps = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=workshop-",
             "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=10,
        )
        containers = []
        for line in ps.stdout.strip().splitlines():
            parts = line.split("\t")
            containers.append({
                "name": parts[0],
                "status": parts[1] if len(parts) > 1 else "",
                "ports": parts[2] if len(parts) > 2 else "",
            })

        image_info = subprocess.run(
            ["docker", "inspect", "-f", "{{.Created}}", DOCKER_IMAGE],
            capture_output=True, text=True, timeout=10,
        )
        last_build = image_info.stdout.strip() if image_info.returncode == 0 else None

        return jsonify({
            "containers": containers,
            "container_count": len(containers),
            "image": DOCKER_IMAGE,
            "image_built_at": last_build,
        }), 200
    except Exception as e:
        return jsonify({"error": f"could not read docker status: {e}"}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5100)))