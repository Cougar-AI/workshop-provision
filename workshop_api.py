import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR"))
REQUIREMENTS_PATH = Path(os.environ.get("REQUIREMENTS_PATH"))
DOCKERFILE_DIR = Path(os.environ.get("DOCKERFILE_DIR"))
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE")
API_KEY = os.environ.get("WORKSHOP_API_KEY")
DEFAULT_REPO_URL = os.environ.get("DEFAULT_GITHUB_REPO_URL", "https://github.com/LemurTech22/guacamole-test-repo.git")

PROVISION_SCRIPT = SCRIPTS_DIR / os.environ.get("PROVISION_SCRIPT", "provision_workshop.sh")
TEARDOWN_SCRIPT = SCRIPTS_DIR / os.environ.get("TEARDOWN_SCRIPT", "teardown")
RESET_SCRIPT = SCRIPTS_DIR / os.environ.get("RESET_SCRIPT", "reset_environments")

_jobs_lock = threading.Lock()
_jobs = {}  # job_id -> dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pkg_name(spec):
    """Strips version pins so 'flask==2.3' and 'flask>=2' compare as the same package."""
    return re.split(r"[=<>!~\[]", spec.strip())[0].lower()


def _read_requirements():
    if not REQUIREMENTS_PATH.exists():
        return []
    return [l.strip() for l in REQUIREMENTS_PATH.read_text().splitlines() if l.strip()]


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
            "stage": None,
            "stage_index": 0,
            "total_stages": None,
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


def _run_job_stages(job_id, stages, cwd=None, extra_env=None):
    """
    stages: list of (label, cmd) tuples run sequentially.
    extra_env: dict of additional env vars merged on top of the current
    process environment (e.g. GITHUB_REPO_URL), so downstream shell
    scripts like PROVISION_SCRIPT can read them and forward them into
    `docker run -e ...`.
    Updates job['stage'], job['stage_index'], job['total_stages'] so a
    frontend can render a progress bar per phase, not just done/not-done.
    """
    total = len(stages)
    _update_job(job_id, status="running", started_at=time.time(),
                total_stages=total, stage_index=0)
    full_env = {**os.environ, **(extra_env or {})}
    try:
        for i, (label, cmd) in enumerate(stages, start=1):
            _update_job(job_id, stage=label, stage_index=i)
            _append_log(job_id, f"=== stage {i}/{total}: {label} ===")
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=full_env,
                stdin=subprocess.DEVNULL,   # never block on a prompt
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                _append_log(job_id, line.rstrip("\n"))
            proc.wait()
            if proc.returncode != 0:
                _update_job(job_id, status="failed", exit_code=proc.returncode, finished_at=time.time())
                return
        _update_job(job_id, status="succeeded", exit_code=0, finished_at=time.time())
    except Exception as e:
        _append_log(job_id, f"FATAL: {e}")
        _update_job(job_id, status="failed", exit_code=-1, finished_at=time.time())


def _launch_stages(job_type, stages, args, cwd=None, extra_env=None):
    job_id = _new_job(job_type, args)
    t = threading.Thread(
        target=_run_job_stages,
        args=(job_id, stages),
        kwargs={"cwd": cwd, "extra_env": extra_env},
        daemon=True,
    )
    t.start()
    return job_id

def _current_container_count():
    """Returns the number of workshop-* containers currently on the host."""
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=workshop-", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10,
    )
    names = [n for n in ps.stdout.strip().splitlines() if n]
    return len(names)

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
# Pipeline — full "start from scratch" flow (build + provision in one job)
# ---------------------------------------------------------------------------
def _do_pipeline_run(data):
    num_containers = data.get("num_containers")
    if not num_containers:
        return jsonify({"error": "num_containers is required"}), 400

    num_students = data.get("num_students", num_containers)
    packages = data.get("packages")
    skip_build = bool(data.get("skip_build"))
    repo_url = (data.get("repo_url") or "").strip() or DEFAULT_REPO_URL

    if packages:
        REQUIREMENTS_PATH.write_text("\n".join(packages) + "\n")

    stages = []
    if not skip_build:
        stages.append(("building image", ["docker", "build", "-t", DOCKER_IMAGE, str(DOCKERFILE_DIR)]))
    stages.append((
        "provisioning containers",
        [str(PROVISION_SCRIPT), "--containers", str(int(num_containers)), "--students", str(int(num_students))],
    ))

    job_id = _launch_stages(
        "pipeline",
        stages,
        {"num_containers": num_containers, "num_students": num_students,
         "packages": packages, "skip_build": skip_build, "repo_url": repo_url},
        extra_env={"GITHUB_REPO_URL": repo_url},
    )
    return jsonify({"job_id": job_id, "stages": [s[0] for s in stages], "repo_url": repo_url}), 202


@app.route("/admin/workshops/pipeline/run", methods=["POST"])
def pipeline_run():

    return _do_pipeline_run(request.get_json(silent=True) or {})


# ---------------------------------------------------------------------------
# Provisioning lifecycle
# ---------------------------------------------------------------------------
def _do_provision(data):

    current = _current_container_count()

    if "add" in data:
        num_containers = current + int(data["add"])
    elif "num_containers" in data:
        num_containers = int(data["num_containers"])
    else:
        return jsonify({"error": "either num_containers (target total) or add (delta) is required"}), 400

    if num_containers <= current and "add" not in data:
        return jsonify({
            "error": f"num_containers ({num_containers}) must be greater than current count ({current}) to add VMs",
            "current_count": current,
        }), 400

    num_students = data.get("num_students", num_containers)
    packages = data.get("packages")
    force_rebuild = bool(data.get("force_rebuild"))
    repo_url = (data.get("repo_url") or "").strip() or DEFAULT_REPO_URL

    if packages:
        REQUIREMENTS_PATH.write_text("\n".join(packages) + "\n")

    # Only rebuild the image if the caller actually changed packages or
    # explicitly asked for a rebuild — this route is meant to be the
    # lightweight "add/adjust containers" call, not a full rebuild by default.
    stages = []
    if packages or force_rebuild:
        stages.append(("building image", ["docker", "build", "-t", DOCKER_IMAGE, str(DOCKERFILE_DIR)]))
    stages.append((
        "provisioning containers",
        [str(PROVISION_SCRIPT), "--containers", str(int(num_containers)), "--students", str(int(num_students))],
    ))

    job_id = _launch_stages(
        "provision", stages,
        {"num_containers": num_containers, 
	 "num_students": num_students,
         "packages": packages, 
	 "force_rebuild": force_rebuild,
	 "previous_count": current,
	 "repo_url": repo_url},
        extra_env={"GITHUB_REPO_URL": repo_url},
    )
    return jsonify({"job_id": job_id, "previous_count": current, "target_count": num_containers, "repo_url": repo_url}), 202


@app.route("/admin/workshops/provision", methods=["POST"])
def provision_route():
    return _do_provision(request.get_json(silent=True) or {})


def _do_teardown(data):
    if data.get("all"):
        num_containers = _current_container_count()
        if num_containers == 0:
            return jsonify({"message": "nothing to tear down — no workshop containers running"}), 200
    else:
        num_containers = data.get("num_containers")
        if not num_containers:
            return jsonify({"error": "num_containers is required (or pass \"all\": true)"}), 400
        num_containers = int(num_containers)

    job_id = _launch_stages(
        "teardown",
        [("tearing down", [str(TEARDOWN_SCRIPT), "--containers", str(num_containers),
                            "--students", str(num_containers), "--force"])],
        {"num_containers": num_containers, "all": bool(data.get("all"))},
    )
    return jsonify({"job_id": job_id, "torn_down_count": num_containers}), 202

@app.route("/admin/workshops/teardown", methods=["POST"])
def teardown_route():
    return _do_teardown(request.get_json(silent=True) or {})

def _do_reset(data):
    if data.get("all"):
        num_containers = _current_container_count()
        if num_containers == 0:
            return jsonify({"message": "nothing to reset — no workshop containers running"}), 200
    else:
        num_containers = data.get("num_containers")
        if not num_containers:
            return jsonify({"error": "num_containers is required (or pass \"all\": true)"}), 400
        num_containers = int(num_containers)

    job_id = _launch_stages(
        "reset",
        [("resetting containers", [str(RESET_SCRIPT), "--containers", str(num_containers)])],
        {"num_containers": num_containers, "all": bool(data.get("all"))},
    )
    return jsonify({"job_id": job_id}), 202

@app.route("/admin/workshops/reset", methods=["POST"])
def reset_route():
    return _do_reset(request.get_json(silent=True) or {})


# ---------------------------------------------------------------------------
# Job status polling, history, and rerun
# ---------------------------------------------------------------------------
@app.route("/admin/workshops/jobs", methods=["GET"])
def list_jobs():
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
        summary = [{k: v for k, v in j.items() if k != "log"} for j in jobs]  # trim logs for list view
    return jsonify({"jobs": summary}), 200


@app.route("/admin/workshops/jobs/<job_id>", methods=["GET"])
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(job), 200


@app.route("/admin/workshops/jobs/<job_id>/rerun", methods=["POST"])
def rerun_job(job_id):
    with _jobs_lock:
        old = _jobs.get(job_id)
    if not old:
        return jsonify({"error": "unknown job_id"}), 404

    dispatch = {
        "pipeline": _do_pipeline_run,
        "provision": _do_provision,
        "teardown": _do_teardown,
        "reset": _do_reset,
    }
    fn = dispatch.get(old["type"])
    if not fn:
        return jsonify({"error": f"cannot rerun job type '{old['type']}'"}), 400
    return fn(old["args"])


# ---------------------------------------------------------------------------
# Requirements management
# ---------------------------------------------------------------------------
@app.route("/admin/workshops/requirements", methods=["GET"])
def get_requirements():
    return jsonify({"packages": _read_requirements()}), 200


@app.route("/admin/workshops/requirements", methods=["PUT"])
def update_requirements():
    data = request.get_json(silent=True) or {}
    packages = data.get("packages")
    if packages is None:
        return jsonify({"error": "packages is required"}), 400
    REQUIREMENTS_PATH.write_text("\n".join(packages) + "\n")
    return jsonify({
        "packages": packages,
        "note": "rebuild required for this to take effect — call /pipeline/run or "
                "/provision with force_rebuild=true",
    }), 200


@app.route("/admin/workshops/requirements/add", methods=["POST"])
def add_requirement():
    data = request.get_json(silent=True) or {}
    pkg = data.get("package")
    if not pkg:
        return jsonify({"error": "package is required"}), 400

    current = _read_requirements()
    names = {_pkg_name(p): i for i, p in enumerate(current)}
    name = _pkg_name(pkg)
    if name in names:
        current[names[name]] = pkg  # replace with new version pin
    else:
        current.append(pkg)
    REQUIREMENTS_PATH.write_text("\n".join(current) + "\n")
    return jsonify({
        "packages": current,
        "note": "rebuild required for this to take effect — call /pipeline/run or "
                "/provision with force_rebuild=true",
    }), 200


@app.route("/admin/workshops/requirements/remove", methods=["POST"])
def remove_requirement():
    data = request.get_json(silent=True) or {}
    pkg = data.get("package")
    if not pkg:
        return jsonify({"error": "package is required"}), 400

    current = [p for p in _read_requirements() if _pkg_name(p) != _pkg_name(pkg)]
    REQUIREMENTS_PATH.write_text("\n".join(current) + "\n")
    return jsonify({
        "packages": current,
        "note": "rebuild required for this to take effect — call /pipeline/run or "
                "/provision with force_rebuild=true",
    }), 200


@app.route("/admin/workshops/requirements/preview", methods=["POST"])
def preview_requirements():
    data = request.get_json(silent=True) or {}
    incoming = data.get("packages", [])

    current = _read_requirements()
    current_names = {_pkg_name(p): p for p in current}
    incoming_names = {_pkg_name(p): p for p in incoming}

    added = [incoming_names[n] for n in incoming_names if n not in current_names]
    removed = [current_names[n] for n in current_names if n not in incoming_names]
    unchanged = [current_names[n] for n in current_names if n in incoming_names]

    return jsonify({"added": added, "removed": removed, "unchanged": unchanged}), 200


# ---------------------------------------------------------------------------
# Clean up
# ---------------------------------------------------------------------------
@app.route("/admin/workshops/image/prune", methods=["POST"])
def prune_images():
    result = subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, text=True, timeout=30)
    return jsonify({"output": result.stdout, "error": result.stderr or None}), 200


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
