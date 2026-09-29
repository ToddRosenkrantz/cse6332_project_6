#!/usr/bin/env python3
"""
CSE 6332 pipeline controller: Kafka (KRaft) -> Spark Structured Streaming -> S3
(SeaweedFS), monitored by Prometheus + Grafana (+ cAdvisor).

One entry point for Windows PowerShell, Linux/WSL2 bash and macOS:

    python setup.py help

Assumes Python 3.9+ in an activated virtual environment and Docker with
Compose v2. All settings (credentials, ports, image tags) live in .env.
"""
import sys

if sys.version_info < (3, 9):
    sys.exit("setup.py needs Python 3.9 or newer (found %d.%d). Create/activate a venv with a newer Python."
             % sys.version_info[:2])

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

# Emoji output must not crash a cp1252 Windows console
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

IS_WINDOWS = os.name == "nt"
# Host-side connections use IPv4: on Windows "localhost" resolves to ::1 first,
# which Docker Desktop does not forward. (Browser URLs still say localhost.)
HOST = "127.0.0.1"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
RUN_DIR = ROOT / "run"
LOG_DIR = ROOT / "logs"
JARS_DIR = ROOT / "volumes" / "jars"
JMX_DIR = ROOT / "volumes" / "jmx"
S3_CONFIG = ROOT / "volumes" / "seaweedfs" / "s3.json"
DASHBOARD_DIR = ROOT / "volumes" / "grafana" / "dashboards"
PROJECT_NAME = "cse6332"  # must match `name:` in docker-compose.yaml
# Host files/folders bind-mounted (read-only) into containers
MOUNT_SOURCES = [ROOT / "volumes", ROOT / "jobs", ROOT / "prometheus.yml"]

# ==============================================================================
# DEPENDENCY RESOLUTION MAP (Spark 3.5.9 + Kafka + S3A + JMX)
# Versions follow Spark 3.5.9's own pom: kafka 3.4.1, hadoop 3.3.4, pool2 2.11.1.
# ==============================================================================
MAVEN = "https://repo1.maven.org/maven2"
SPARK_VERSION = "3.5.9"
DOWNLOAD_FILES = [
    {
        "name": "Prometheus JMX Java Agent v1.0.1",
        "url": f"{MAVEN}/io/prometheus/jmx/jmx_prometheus_javaagent/1.0.1/jmx_prometheus_javaagent-1.0.1.jar",
        "dest": JMX_DIR / "jmx_prometheus_javaagent-1.0.1.jar",
    },
    {
        "name": "Spark SQL Kafka Connector",
        "url": f"{MAVEN}/org/apache/spark/spark-sql-kafka-0-10_2.12/{SPARK_VERSION}/spark-sql-kafka-0-10_2.12-{SPARK_VERSION}.jar",
        "dest": JARS_DIR / f"spark-sql-kafka-0-10_2.12-{SPARK_VERSION}.jar",
    },
    {
        "name": "Spark Token Provider Kafka",
        "url": f"{MAVEN}/org/apache/spark/spark-token-provider-kafka-0-10_2.12/{SPARK_VERSION}/spark-token-provider-kafka-0-10_2.12-{SPARK_VERSION}.jar",
        "dest": JARS_DIR / f"spark-token-provider-kafka-0-10_2.12-{SPARK_VERSION}.jar",
    },
    {
        "name": "Kafka Clients Library",
        "url": f"{MAVEN}/org/apache/kafka/kafka-clients/3.4.1/kafka-clients-3.4.1.jar",
        "dest": JARS_DIR / "kafka-clients-3.4.1.jar",
    },
    {
        "name": "Apache Commons Pool2",
        "url": f"{MAVEN}/org/apache/commons/commons-pool2/2.11.1/commons-pool2-2.11.1.jar",
        "dest": JARS_DIR / "commons-pool2-2.11.1.jar",
    },
    {
        "name": "Hadoop AWS S3 Connector",
        "url": f"{MAVEN}/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar",
        "dest": JARS_DIR / "hadoop-aws-3.3.4.jar",
    },
    {
        "name": "AWS Java SDK Bundle",
        "url": f"{MAVEN}/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar",
        "dest": JARS_DIR / "aws-java-sdk-bundle-1.12.262.jar",
    },
]

# Producer implementations and their Prometheus ports (see prometheus.yml)
PRODUCER_SCRIPTS = {
    "kp": ROOT / "producers" / "poisson_kafka_producer.py",
    "ck": ROOT / "producers" / "producer_confluent_kafka.py",
}
PRODUCER_PORTS = {
    ("kp", "topic-json"): 9108,
    ("kp", "topic-parq"): 9109,
    ("ck", "topic-json"): 9118,
    ("ck", "topic-parq"): 9119,
}
DATA_TOPICS = ["topic-json", "topic-parq"]

CONSUMERS = {"spark-consumer-json": "topic-json_sink", "spark-consumer-parquet": "topic-parq_sink"}


# ==============================================================================
# Helpers
# ==============================================================================
def print_banner(message):
    print("\n" + "=" * 70)
    print(f" 🚀 {message}")
    print("=" * 70)


def run_command(cmd, description=None, fatal=True, capture=False, input_text=None, quiet=False):
    """Run an argument list (never a shell string). Returns CompletedProcess."""
    if description and not quiet:
        print(f"\n▶ {description}")
    try:
        result = subprocess.run(
            cmd,
            check=False,
            text=True,
            input=input_text,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            encoding="utf-8" if capture or input_text is not None else None,
            errors="replace" if capture or input_text is not None else None,
        )
    except FileNotFoundError:
        print(f"❌ Command not found: {cmd[0]}")
        if fatal:
            sys.exit(1)
        return subprocess.CompletedProcess(cmd, 127, "", "")
    if result.returncode != 0 and fatal:
        if capture and result.stderr:
            print(result.stderr.strip())
        print(f"❌ Failed ({result.returncode}): {description or ' '.join(cmd)}")
        sys.exit(result.returncode)
    return result


def compose(*args):
    return ["docker", "compose", *args]


def load_env():
    """Read .env (falling back to .env.example for missing keys) without python-dotenv."""
    values = {}
    for path in (ENV_EXAMPLE, ENV_FILE):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key.strip()] = value
    return values


ENV = load_env()


def env_int(key, default):
    try:
        return int(ENV.get(key, default))
    except ValueError:
        return default


def env_bool(key, default=True):
    value = ENV.get(key)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def _env_lines(path):
    """{key: original line} for KEY=VALUE lines."""
    lines = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            lines[line.split("=", 1)[0].strip()] = raw
    return lines


# Credentials generated at install (letters and digits only: safe in .env and compose files)
SECRET_KEYS = ("S3_ACCESS_KEY", "S3_SECRET_KEY", "GRAFANA_ADMIN_PASSWORD", "SEAWEED_ADMIN_PASSWORD")
WEAK_SECRET_VALUES = {"", "admin", "password", "changeme", "generated-at-install"}


def generate_secret(key):
    import secrets
    import string
    if key == "S3_ACCESS_KEY":  # AWS-style: 20 upper-case letters and digits
        return "AK" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(18))
    length = 40 if key == "S3_SECRET_KEY" else 24
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def weak_secrets():
    """Credential keys in .env that still hold a placeholder or a well-known default."""
    return [k for k in SECRET_KEYS
            if ENV.get(k, "").strip().lower() in WEAK_SECRET_VALUES or ENV.get(k, "").lower().startswith("change-me")]


def protect_env_file():
    """.env holds credentials: readable only by its owner (no-op on Windows)."""
    if not IS_WINDOWS and ENV_FILE.exists():
        try:
            os.chmod(ENV_FILE, 0o600)
        except OSError:
            pass


def ensure_env_file():
    """Create .env from .env.example (with freshly generated credentials), or add keys a newer
    version introduced. docker compose reads only .env, so a key missing there would break it."""
    if not ENV_FILE.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        ENV.update(load_env())
        generated = {k: generate_secret(k) for k in weak_secrets()}
        if generated:
            _write_env_values(generated)
        protect_env_file()
        print(f"  🔐 Created .env with unique random credentials (S3 keys, Grafana and SeaweedFS admin passwords).")
        print("     Show them any time with: python setup.py secrets")
    else:
        current = _env_lines(ENV_FILE)
        missing = [line for key, line in _env_lines(ENV_EXAMPLE).items() if key not in current]
        if missing:
            text = ENV_FILE.read_text(encoding="utf-8")
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"\n# Added by setup.py from .env.example ({time.strftime('%Y-%m-%d')})\n" + "\n".join(missing) + "\n"
            ENV_FILE.write_text(text, encoding="utf-8")
            keys = ", ".join(line.split("=", 1)[0].strip() for line in missing)
            print(f"  📄 Added new settings to .env from .env.example: {keys}")
        protect_env_file()
    ENV.update(load_env())
    weak = weak_secrets()
    if weak:
        print(f"  ⚠️ .env uses default or placeholder credentials for {', '.join(weak)}.")
        print("     Replace them with random ones: python setup.py secrets --rotate")


def _write_env_values(values):
    """Low-level KEY=VALUE update of .env (keeps comments and order)."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    protect_env_file()
    ENV.update(load_env())


def reclaim_with_docker(folder):
    """Hand a root-owned folder (created by Docker for a missing bind-mount source)
    back to the current user, using Docker itself instead of sudo."""
    if IS_WINDOWS or not hasattr(os, "getuid"):
        return False
    result = run_command(
        ["docker", "run", "--rm", "--user", "0", "--entrypoint", "sh", "-v", f"{folder}:/fix",
         ENV["SEAWEEDFS_IMAGE"], "-c", f"chown -R {os.getuid()}:{os.getgid()} /fix"],
        capture=True, fatal=False,
    )
    return result.returncode == 0 and os.access(folder, os.W_OK)


def repair_mount_sources():
    """Undo what `docker compose up` does when a bind-mount source is missing:
    it creates a (root-owned, on Linux) directory in its place."""
    for folder in (JARS_DIR, JMX_DIR, S3_CONFIG.parent, RUN_DIR, LOG_DIR):
        folder.mkdir(parents=True, exist_ok=True)
        if not os.access(folder, os.W_OK):
            rel = folder.relative_to(ROOT)
            if reclaim_with_docker(folder):
                print(f"  🔧 {rel} was owned by root (created by Docker); ownership returned to you")
            else:
                print(f"❌ {rel} is not writable by you (probably created by Docker as root).")
                print(f"   Fix once with:  sudo chown -R $(id -u):$(id -g) \"{rel}\"")
                sys.exit(1)
    if S3_CONFIG.is_dir():
        rel = S3_CONFIG.relative_to(ROOT)
        try:
            shutil.rmtree(S3_CONFIG)
        except OSError:
            if not (reclaim_with_docker(S3_CONFIG.parent) and not shutil.rmtree(S3_CONFIG, ignore_errors=True)
                    and not S3_CONFIG.exists()):
                print(f"❌ {rel} is a directory Docker created and you cannot remove it.")
                print(f"   Fix once with:  sudo rm -rf \"{rel}\"")
                sys.exit(1)
        print(f"  🔧 Replaced directory {rel} (created by Docker) with the real file")


def fix_mount_permissions():
    """Containers run as their own users (kafka 1000, spark 185, grafana 472, ...),
    so every bind-mounted file must be world-readable whatever the umask was."""
    if IS_WINDOWS:
        return
    fixed = failed = 0
    for base in MOUNT_SOURCES:
        paths = [base] + (list(base.rglob("*")) if base.is_dir() else [])
        for path in paths:
            try:
                mode = path.stat().st_mode
                want = mode | (0o055 if path.is_dir() else 0o044)
                if want != mode:
                    os.chmod(path, want)
                    fixed += 1
            except OSError:
                failed += 1
    if fixed:
        print(f"  🔧 Made {fixed} mounted file(s)/folder(s) readable by the containers")
    if failed:
        print(f"  ⚠️ Could not adjust permissions on {failed} path(s); containers may not be able to read them")


def check_container_conflicts(fatal=True):
    """Fixed container names clash with a stack started from another project
    (e.g. the old v1 cse6332_project). Returns True when clear."""
    result = run_command(compose("config", "--format", "json"), capture=True, fatal=False)
    try:
        names = {svc.get("container_name") for svc in json.loads(result.stdout)["services"].values()} - {None}
    except (ValueError, KeyError):
        return True
    fmt = '{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}'
    listing = run_command(["docker", "ps", "-a", "--format", fmt], capture=True, fatal=False)
    clashes, moved = [], set()
    for line in listing.stdout.splitlines():
        parts = (line.split("\t") + ["", ""])[:3]
        name, project, workdir = parts
        if name not in names:
            continue
        if project != PROJECT_NAME:
            clashes.append((name, project or "(not compose)", workdir))
        elif name == "kafka" and workdir and Path(workdir).resolve() != ROOT:
            # kafka mounts project files, so compose always recreates it for a new
            # folder; containers without such mounts keep their old working_dir label
            moved.add(workdir)
    for workdir in moved:
        print(f"  ℹ️ This stack was last started from {workdir}; it will now run from {ROOT} (data is kept).")
    if clashes:
        print("❌ Containers from another stack use the same names:")
        for name, project, workdir in clashes:
            print(f"     {name:<24} project={project}  {workdir}")
        dirs = sorted({w for _, _, w in clashes if w})
        print("   Stop that stack first, e.g.:" + "".join(f"\n     cd {d} && docker compose down" for d in dirs))
        print(f"   or remove the containers:  docker rm -f {' '.join(n for n, _, _ in clashes)}")
        if fatal:
            sys.exit(1)
        return False
    return True


def http_status(url, timeout=3):
    """Return the HTTP status code for url, or None when unreachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def port_open(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def download_with_progress(url, dest_path, item_name):
    dest_path = Path(dest_path)
    if dest_path.exists() and dest_path.stat().st_size > 0:
        print(f"  ✔️ [SKIPPED] {item_name} already present at {dest_path.relative_to(ROOT)}")
        return

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  📥 [DOWNLOADING] {item_name}...")
    print(f"     Source: {url}")

    def progress_bar(block_num, block_size, total_size):
        read_bytes = block_num * block_size
        if total_size > 0:
            percent = min(100, int(read_bytes * 100 / total_size))
            filled = int(30 * percent / 100)
            bar = "█" * filled + "-" * (30 - filled)
            sys.stdout.write(f"\r     Progress: [{bar}] {percent}% ({read_bytes / (1024*1024):.1f} MB)")
            sys.stdout.flush()

    partial = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        urllib.request.urlretrieve(url, partial, reporthook=progress_bar)
        partial.replace(dest_path)
        print(f"\n     ✅ Installed: {dest_path.name}")
    except Exception as e:
        partial.unlink(missing_ok=True)
        print(f"\n     ❌ Failed download for {dest_path.name}: {e}")
        print("\n⛔ Aborting: required JAR dependency could not be fetched.")
        sys.exit(1)


def prune_stale_jars():
    """Only the manifest's jars may sit in volumes/jars (spark-submit loads them all)."""
    wanted = {item["dest"].name for item in DOWNLOAD_FILES if item["dest"].parent == JARS_DIR}
    for jar in JARS_DIR.glob("*.jar"):
        if jar.name not in wanted:
            jar.unlink()
            print(f"  🧹 Removed stale jar (not in manifest): {jar.name}")


def jars_present():
    return all(item["dest"].exists() for item in DOWNLOAD_FILES)


def render_s3_config():
    """SeaweedFS cannot read env vars in s3.json, so render it from .env."""
    config = {
        "identities": [
            {
                "name": "admin",
                "credentials": [
                    {"accessKey": ENV["S3_ACCESS_KEY"], "secretKey": ENV["S3_SECRET_KEY"]}
                ],
                "actions": ["Admin", "Read", "List", "Tagging", "Write"],
            }
        ]
    }
    S3_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(config, indent=2) + "\n"
    if not S3_CONFIG.exists() or S3_CONFIG.read_text(encoding="utf-8") != text:
        S3_CONFIG.write_text(text, encoding="utf-8")
        print(f"  📄 Rendered {S3_CONFIG.relative_to(ROOT)} from .env")


def compose_services():
    result = run_command(compose("config", "--services"), capture=True, fatal=True)
    return [s for s in result.stdout.split() if s]


def container_state(name):
    result = run_command(
        ["docker", "inspect", "-f", "{{.State.Status}}", name], capture=True, fatal=False
    )
    return result.stdout.strip() if result.returncode == 0 else "absent"


def s3_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"http://{HOST}:{ENV['S3_PORT']}",
        aws_access_key_id=ENV["S3_ACCESS_KEY"],
        aws_secret_access_key=ENV["S3_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2}),
    )


# ==============================================================================
# Checks
# ==============================================================================
def check_prerequisites():
    print_banner("PRE-FLIGHT SYSTEM DEPENDENCY CHECK")
    if shutil.which("docker") is None:
        print("❌ Critical Error: 'docker' is not installed or not in PATH.")
        sys.exit(1)
    print("  ✅ Executable verified: docker")

    if run_command(["docker", "info"], capture=True, fatal=False).returncode != 0:
        print("❌ Critical Error: Docker daemon is not running. Start Docker Desktop or the Docker service.")
        sys.exit(1)
    print("  ✅ Docker daemon is active and responding.")

    version = run_command(compose("version", "--short"), capture=True, fatal=False)
    if version.returncode != 0:
        print("❌ Critical Error: Docker Compose v2 ('docker compose') is not available.")
        sys.exit(1)
    print(f"  ✅ Docker Compose {version.stdout.strip()}")


def host_arch():
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "amd64"


def doctor(_args=None):
    check_prerequisites()
    ok = True

    print_banner("ENVIRONMENT")
    print(f"  • Host OS / arch   : {platform.system()} / {host_arch()}")
    print(f"  • Python           : {sys.version.split()[0]} ({sys.executable})")
    print(f"  • Virtual env      : {'yes' if sys.prefix != sys.base_prefix else 'NO (activate your venv)'}")
    if IS_WINDOWS and sys.version_info < (3, 11):
        print("  ⚠️ On Windows, Python 3.11+ is recommended: older versions sleep in ~15.6 ms ticks, which caps")
        print("     each producer thread near 64 msgs/s and skews throughput measurements.")

    missing = []
    for module in ("kafka", "confluent_kafka", "prometheus_client", "psutil", "boto3"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        ok = False
        print(f"  ⚠️ Missing Python packages: {', '.join(missing)}  →  pip install -r requirements.txt")
    else:
        print("  ✅ Python packages installed")

    info = run_command(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"], capture=True, fatal=False)
    try:
        ncpu, mem = (int(x) for x in info.stdout.split())
        need_cores = 2 * env_int("SPARK_APP_CORES", 1)
        worker_cores = env_int("SPARK_WORKER_CORES", 2)
        print(f"  • Docker resources : {ncpu} CPUs, {mem / 2**30:.1f} GiB RAM")
        if worker_cores < need_cores:
            ok = False
            print(f"  ❌ SPARK_WORKER_CORES={worker_cores} < 2 x SPARK_APP_CORES={need_cores}: one consumer would get no executors")
        if ncpu < max(worker_cores, 2):
            ok = False
            print(f"  ❌ Docker has {ncpu} CPUs; the Spark worker is configured for {worker_cores}")
        if mem < 6 * 2**30:
            print("  ⚠️ Less than 6 GiB RAM for Docker; consider `python setup.py up --no-cadvisor` or raise Docker Desktop's memory")
    except ValueError:
        print("  ⚠️ Could not read Docker CPU/memory")

    print_banner("IMAGE ARCHITECTURE CHECK")
    arch = host_arch()
    for key in sorted(k for k in ENV if k.endswith("_IMAGE")):
        image = ENV[key]
        result = run_command(["docker", "manifest", "inspect", image], capture=True, fatal=False)
        archs = set()
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                archs = {m["platform"]["architecture"] for m in data.get("manifests", []) if "platform" in m}
            except (ValueError, KeyError):
                pass
        portable = "amd64+arm64" if {"amd64", "arm64"} <= archs else "host arch only"
        if arch in archs:
            print(f"  ✅ {image:<55} linux/{arch} ({portable})")
        elif result.returncode != 0:
            print(f"  ⚠️ {image:<55} could not inspect (offline?)")
        else:
            ok = False
            print(f"  ❌ {image:<55} has no linux/{arch} build ({', '.join(sorted(archs))})")

    if platform.system() == "Darwin":
        shared = ("/Users/", "/Volumes/", "/private/", "/tmp/", "/var/folders/")
        if not str(ROOT).startswith(shared):
            ok = False
            print(f"  ❌ {ROOT} is outside Docker Desktop's default shared folders ({', '.join(shared)}).")
            print("     Move the project under your home folder, or add it in Docker Desktop →")
            print("     Settings → Resources → File sharing.")
        else:
            print("  ✅ Project folder is shared with Docker Desktop")
    if not IS_WINDOWS:
        current_umask = os.umask(0)
        os.umask(current_umask)  # os.umask can only be read by setting it; restore at once
        if current_umask & 0o044:
            print(f"  ℹ️ umask {current_umask:03o} makes new files private; setup.py makes mounted files readable automatically.")

    print_banner("CONTAINER NAMES")
    if check_container_conflicts(fatal=False):
        print(f"  ✅ No clashes with other stacks (project '{PROJECT_NAME}')")
    else:
        ok = False

    print_banner("HOST PORTS")
    running = container_state("kafka") == "running"
    if running:
        print("  ℹ️ Stack is running; skipping free-port check.")
    else:
        for key in sorted(k for k in ENV if k.endswith("_PORT")):
            port = env_int(key, 0)
            if port and port_open("127.0.0.1", port, timeout=0.5):
                ok = False
                print(f"  ❌ Port {port} ({key}) is already in use")
        print("  ✅ Port check complete")

    print("\n" + ("✅ Doctor: ready." if ok else "⚠️ Doctor: fix the items above."))
    return ok


# ==============================================================================
# Stack lifecycle
# ==============================================================================
def cadvisor_wanted(args):
    if getattr(args, "no_cadvisor", False):
        return False
    return env_bool("CADVISOR_ENABLED", True)


def up(args):
    ensure_env_file()
    repair_mount_sources()
    if not jars_present():
        print("❌ Spark/JMX jars are missing. Run: python setup.py install")
        sys.exit(1)
    render_s3_config()
    fix_mount_permissions()
    check_container_conflicts()

    print_banner("STARTING CONTAINER STACK")
    core = [s for s in compose_services() if s != "cadvisor"]
    run_command(
        compose("up", "-d", "--wait", "--wait-timeout", "300", *core),
        "Starting core services and waiting for health checks / init jobs",
    )

    if cadvisor_wanted(args):
        result = run_command(compose("up", "-d", "cadvisor"), "Starting cAdvisor", fatal=False)
        if result.returncode != 0:
            print("  ⚠️ cAdvisor failed to start (its host mounts are limited on some Docker Desktop setups).")
            print("     The pipeline is unaffected. Use `python setup.py up --no-cadvisor` to skip it.")
    else:
        if container_state("cadvisor") == "running":
            run_command(compose("stop", "cadvisor"), "Stopping cAdvisor (disabled)", fatal=False)
        print("  ℹ️ cAdvisor disabled (--no-cadvisor or CADVISOR_ENABLED=false).")

    verify_dashboards()
    print_endpoints(cadvisor_wanted(args))


def grafana_get(path, timeout=10):
    import base64

    token = base64.b64encode(f"{ENV['GRAFANA_ADMIN_USER']}:{ENV['GRAFANA_ADMIN_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(f"http://{HOST}:{ENV['GRAFANA_PORT']}{path}", headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def verify_dashboards(wait_seconds=60):
    """Grafana provisions every JSON in volumes/grafana/dashboards on start; confirm they all loaded."""
    print_banner("GRAFANA DASHBOARDS (provisioned from volumes/grafana/dashboards)")
    expected = {}
    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expected[data.get("uid") or path.stem] = (data.get("title", path.stem), path.name)
        except ValueError as exc:
            print(f"  ❌ {path.name}: invalid JSON ({exc})")
    deadline = time.time() + wait_seconds
    found = {}
    while True:
        try:
            found = {d["uid"]: d["title"] for d in grafana_get("/api/search?type=dash-db")}
        except Exception:
            found = {}
        if all(uid in found for uid in expected) or time.time() > deadline:
            break
        time.sleep(3)
    for uid, (title, filename) in expected.items():
        if uid in found:
            print(f"  ✅ {title}  ({filename})")
        else:
            print(f"  ❌ {title}  ({filename}) not loaded — see: python setup.py logs grafana")
    missing = [u for u in expected if u not in found]
    print(f"  {len(expected) - len(missing)}/{len(expected)} dashboards available at http://localhost:{ENV['GRAFANA_PORT']}"
          " (folder 'CSE 6332 Pipeline')")


def print_endpoints(cadvisor_on=True):
    e = ENV
    print_banner("SYSTEM ENDPOINTS")
    print(f"  • Grafana dashboards    : http://localhost:{e['GRAFANA_PORT']}  (login: python setup.py secrets)")
    print(f"  • Prometheus            : http://localhost:{e['PROMETHEUS_PORT']}/targets")
    print(f"  • S3 API (SeaweedFS)    : http://localhost:{e['S3_PORT']}  bucket '{e['S3_BUCKET']}'")
    print(f"  • SeaweedFS admin UI    : http://localhost:{e['SEAWEED_ADMIN_PORT']}  (login: python setup.py secrets)")
    print(f"  • SeaweedFS filer UI    : http://localhost:{e['SEAWEED_FILER_PORT']}/buckets/{e['S3_BUCKET']}/")
    print(f"  • SeaweedFS master UI   : http://localhost:{e['SEAWEED_MASTER_PORT']}")
    print(f"  • SeaweedFS volume UI   : http://localhost:{e['SEAWEED_VOLUME_PORT']}/ui/index.html")
    print(f"  • Spark master UI       : http://localhost:{e['SPARK_MASTER_UI_PORT']}")
    print(f"  • Spark worker UI       : http://localhost:{e['SPARK_WORKER_UI_PORT']}")
    print(f"  • Spark consumer JSON   : http://localhost:{e['SPARK_JSON_UI_PORT']}/StreamingQuery/")
    print(f"  • Spark consumer Parquet: http://localhost:{e['SPARK_PARQUET_UI_PORT']}/StreamingQuery/")
    print("  • cAdvisor              : " + (f"http://localhost:{e['CADVISOR_PORT']}" if cadvisor_on else "disabled"))
    print(f"  • Kafka bootstrap       : 127.0.0.1:{e['KAFKA_PORT']}")
    if e.get("KAFKA_LAN_HOST", "127.0.0.1") not in ("127.0.0.1", "localhost", ""):
        print(f"  • Kafka for LAN clients : {e['KAFKA_LAN_HOST']}:{e['KAFKA_LAN_PORT']}")
    print("\n  Next: python setup.py producers start   (then: status / monitor)")


def install(args):
    check_prerequisites()
    ensure_env_file()

    print_banner("PROVISIONING DIRECTORIES")
    repair_mount_sources()
    print("  📁 volumes/, run/, logs/ ready")

    print_banner("FETCHING EXTERNAL DEPENDENCIES (JARS & JMX AGENT)")
    for item in DOWNLOAD_FILES:
        download_with_progress(item["url"], item["dest"], item["name"])
    prune_stale_jars()

    print_banner("RENDERING CONFIGURATION")
    render_s3_config()

    print_banner("LOCAL PYTHON DEPENDENCY INSTALLATION")
    run_command([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], "pip install -r requirements.txt")

    up(args)


def stop_stack(_args=None):
    print_banner("STOPPING CONTAINER STACK (data is kept)")
    run_command(compose("stop"), "docker compose stop")


def shutdown(args):
    """Producers -> consumers -> everything else (replaces stack_shutdown.sh)."""
    stop_producers(impls=["kp", "ck"], topics=DATA_TOPICS, include_ramps=True)
    print_banner("STOPPING SPARK CONSUMERS")
    run_command(compose("stop", *CONSUMERS), "Stopping consumers", fatal=False)
    stop_stack()
    print("\n✅ Shutdown complete: producers, consumers and containers stopped. Restart with: python setup.py up")


def down(args):
    stop_producers(impls=["kp", "ck"], topics=DATA_TOPICS, include_ramps=True)
    print_banner("REMOVING CONTAINERS (named volumes are kept)")
    run_command(compose("down"), "docker compose down")


def restart(args):
    print_banner("RESTARTING CONTAINER STACK")
    services = [] if not args.service else [args.service]
    run_command(compose("restart", *services), "docker compose restart")


def logs(args):
    cmd = compose("logs", "--tail", str(args.tail))
    if args.follow:
        cmd.append("-f")
    if args.service:
        cmd.append(args.service)
    run_command(cmd, fatal=False)


def ps(_args):
    run_command(compose("ps", "-a", "--format", "table {{.ID}}\t{{.Name}}\t{{.Status}}\t{{.Ports}}"), fatal=False)


def init(_args):
    print_banner("RE-RUNNING TOPIC AND BUCKET INITIALIZATION")
    render_s3_config()
    run_command(compose("run", "--rm", "kafka-init"), "Creating Kafka topics")
    run_command(compose("run", "--rm", "s3-init"), "Creating S3 bucket")


def _project_anonymous_volumes():
    """Unnamed volumes mounted by this project's containers (removed together with them)."""
    ids = run_command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT_NAME}"],
                      capture=True, fatal=False).stdout.split()
    if not ids:
        return set()
    result = run_command(["docker", "inspect", "--format",
                          "{{range .Mounts}}{{if eq .Type \"volume\"}}{{.Name}} {{end}}{{end}}", *ids],
                         capture=True, fatal=False)
    return {v for v in result.stdout.split() if len(v) == 64 and all(c in "0123456789abcdef" for c in v)}


def _leftover_project_volumes():
    """Named volumes from any version or copy of this project (Compose project name cse6332...),
    e.g. v1's object-storage, coordination and Grafana volumes, or volumes named after an older folder."""
    result = run_command(["docker", "volume", "ls", "--format",
                          '{{.Name}}\t{{.Label "com.docker.compose.project"}}'], capture=True, fatal=False)
    names = []
    for line in result.stdout.splitlines():
        name, _, project = line.partition("\t")
        if project.startswith("cse6332"):
            names.append(name)
    return names


def _empty_dangling_anonymous_volumes():
    """Unused unnamed volumes that contain no files: safe to delete whichever project made them."""
    dangling = [v for v in run_command(["docker", "volume", "ls", "-q", "-f", "dangling=true"],
                                       capture=True, fatal=False).stdout.split()
                if len(v) == 64 and all(c in "0123456789abcdef" for c in v)]
    if not dangling:
        return []
    mounts = []
    for i, vol in enumerate(dangling):
        mounts += ["-v", f"{vol}:/v/{i}:ro"]
    script = "for d in /v/*; do [ -z \"$(ls -A $d)\" ] && basename $d; done; true"
    result = run_command(["docker", "run", "--rm", "--entrypoint", "sh", *mounts, ENV["SPARK_IMAGE"], "-c", script],
                         capture=True, fatal=False)
    return [dangling[int(i)] for i in result.stdout.split() if i.isdigit() and int(i) < len(dangling)]


def _remove_volumes(names, label):
    removed = 0
    for name in names:
        if run_command(["docker", "volume", "rm", name], capture=True, fatal=False).returncode == 0:
            removed += 1
    if names:
        print(f"  🧹 Removed {removed} {label}" + (f" ({len(names) - removed} still in use)" if removed < len(names) else ""))


def _remove_path(path):
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()
    else:
        return False
    return True


def clean(args):
    """Remove what setup.py created or downloaded. --volumes also deletes all data."""
    print_banner("CLEANING PIPELINE" + (" (INCLUDING ALL DATA)" if args.volumes else ""))
    stop_producers(impls=["kp", "ck"], topics=DATA_TOPICS, include_ramps=True)
    anonymous = _project_anonymous_volumes()
    if args.volumes:
        print("  ⚠️ Deleting all data: Kafka messages, S3 objects (SeaweedFS data and admin), Spark checkpoints,")
        print("     Prometheus history and the Grafana database")
        run_command(compose("down", "-v", "--remove-orphans"), "Removing containers, network and data volumes")
    else:
        run_command(compose("down", "--remove-orphans"), "Removing containers and network (data volumes kept)")
    _remove_volumes(sorted(anonymous), "unnamed volume(s) left by this project's containers")
    if args.volumes:
        _remove_volumes(_leftover_project_volumes(), "volume(s) from older versions or copies of this project")
        _remove_volumes(_empty_dangling_anonymous_volumes(), "empty unused unnamed volume(s)")

    # files setup.py generates or downloads
    generated = [S3_CONFIG, RUN_DIR, LOG_DIR] + sorted(ROOT.glob("lambda_*.txt")) + sorted(ROOT.glob("producers_*.txt"))
    if not args.keep_jars:
        generated += sorted(JARS_DIR.glob("*.jar")) + sorted(JMX_DIR.glob("*.jar")) + sorted(ROOT.glob("volumes/**/*.part"))
    if args.results:
        generated.append(ROOT / "results")
    for path in generated:
        if _remove_path(path):
            print(f"  🧹 Removed {path.relative_to(ROOT)}")

    print("\n✅ Clean complete." + (" All data deleted." if args.volumes else " Data volumes were kept."))
    kept = [".env (your settings)"] + ([] if args.results else ["results/ (your experiment results)"])
    kept += [] if args.volumes else ["data volumes (use --volumes to delete them)"]
    kept += ["downloaded jars"] if args.keep_jars else []
    print(f"  Kept: {', '.join(kept)}. Start again with: python setup.py install")


# ==============================================================================
# Producers (replaces manage_producers.sh, manage_producers_ckafka.sh, lambda_ramp.sh)
# ==============================================================================
def pid_file(name):
    return RUN_DIR / f"{name}.pid"


def read_pid(name):
    path = pid_file(name)
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def live_process(name, marker):
    """psutil.Process for the pid in run/<name>.pid if it is still our process."""
    import psutil

    pid = read_pid(name)
    if pid is None:
        return None
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        if any(marker in part for part in proc.cmdline()):
            return proc
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def spawn_detached(cmd, log_path):
    """Start cmd in the background, detached from this terminal, output to log_path."""
    LOG_DIR.mkdir(exist_ok=True)
    RUN_DIR.mkdir(exist_ok=True)
    log = open(log_path, "a", encoding="utf-8")
    kwargs = {"cwd": ROOT, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
    if IS_WINDOWS:
        # Own (hidden) console + process group: CTRL_BREAK_EVENT can target it
        # from any later PowerShell window, and closing this window does not kill it.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE
        kwargs["startupinfo"] = startup
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        return subprocess.Popen(cmd, env=env, **kwargs)
    finally:
        log.close()


# Runs in a throwaway process: attach to the producer's console and send
# CTRL_BREAK_EVENT to the producer's process group (its group id is its pid,
# since it was started with CREATE_NEW_PROCESS_GROUP). Windows only delivers
# console signals within a console, hence the attach; targeting the group
# rather than the whole console (0) keeps the helper itself out of it.
_WIN_SIGNAL_HELPER = r"""
import ctypes, sys
k = ctypes.WinDLL("kernel32", use_last_error=True)
pid = int(sys.argv[1])
k.FreeConsole()
if not k.AttachConsole(pid):
    sys.exit(2)
ok = k.GenerateConsoleCtrlEvent(1, pid)
k.FreeConsole()
sys.exit(0 if ok else 3)
"""


def send_stop_signal(proc):
    """SIGTERM on POSIX; CTRL_BREAK_EVENT (Python's SIGBREAK) on Windows."""
    if IS_WINDOWS:
        result = subprocess.run(
            [sys.executable, "-c", _WIN_SIGNAL_HELPER, str(proc.pid)],
            creationflags=subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    os.kill(proc.pid, signal.SIGTERM)
    return True


def stop_process(name, marker, label):
    import psutil

    proc = live_process(name, marker)
    if proc is None:
        if pid_file(name).exists():
            pid_file(name).unlink()
            print(f"  ℹ️ {label}: not running (removed stale pid file)")
        return
    timeout = env_int("PRODUCER_STOP_TIMEOUT", 15)
    signal_name = "CTRL_BREAK_EVENT" if IS_WINDOWS else "SIGTERM"
    print(f"  🛑 {label}: sending {signal_name} to PID {proc.pid}")
    if not send_stop_signal(proc):
        print(f"  ⚠️ {label}: could not deliver {signal_name}; waiting before force-stop")
    try:
        proc.wait(timeout=timeout)
        print(f"  ✅ {label}: stopped")
    except psutil.TimeoutExpired:
        for p in proc.children(recursive=True) + [proc]:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        print(f"  ⚠️ {label}: did not exit within {timeout}s — force-stopped")
    except psutil.NoSuchProcess:
        print(f"  ✅ {label}: stopped")
    pid_file(name).unlink(missing_ok=True)


def selected(args_impl, args_topic):
    impls = ["kp", "ck"] if args_impl == "both" else [args_impl]
    topics = DATA_TOPICS if not args_topic else [args_topic]
    return impls, topics


def producer_label(impl, topic):
    return f"{'kafka-python' if impl == 'kp' else 'confluent-kafka'} producer [{topic}]"


PRODUCER_COMPRESSION = {"kp": ("none", "gzip"), "ck": ("none", "gzip", "snappy", "lz4", "zstd")}


def add_producer_options(parser):
    """Tuning options shared by `producers start` and `bench` (all optional; unset = script default)."""
    g = parser.add_argument_group("producer tuning (see docs/producer_tuning.md)")
    g.add_argument("--rate", type=float, metavar="LAMBDA", help="Poisson rate in msgs/s per producer thread")
    g.add_argument("--threads", type=int, metavar="N", help="confluent-kafka producer threads per topic")
    g.add_argument("--acks", choices=["0", "1", "all"])
    g.add_argument("--idempotence", choices=["true", "false"], help="requires --acks all when true")
    g.add_argument("--linger-ms", type=int, metavar="MS")
    g.add_argument("--batch-size", type=int, metavar="BYTES")
    g.add_argument("--compression", choices=PRODUCER_COMPRESSION["ck"], help="kafka-python: none|gzip only")
    g.add_argument("--payload-bytes", type=int, metavar="BYTES", help="extra filler bytes per message")
    g.add_argument("--burst", action="store_true", help="send as fast as possible (ignores the rate)")


def producer_script_args(impl, args):
    """Translate `setup.py` producer options into the producer script's own options."""
    out = []
    acks, idem = getattr(args, "acks", None), getattr(args, "idempotence", None)
    if idem == "true" and acks not in (None, "all"):
        print("❌ --idempotence true requires --acks all")
        sys.exit(1)
    comp = getattr(args, "compression", None)
    if comp and comp not in PRODUCER_COMPRESSION[impl]:
        print(f"❌ {producer_label(impl, '').split(' producer')[0]} supports --compression "
              f"{'|'.join(PRODUCER_COMPRESSION[impl])} here (got {comp})")
        sys.exit(1)
    for opt in ("acks", "idempotence", "linger_ms", "batch_size", "compression", "payload_bytes"):
        value = getattr(args, opt, None)
        if value is not None:
            if opt in ("linger_ms", "batch_size", "payload_bytes") and value < 0:
                print(f"❌ --{opt.replace('_', '-')} must be >= 0")
                sys.exit(1)
            out += [f"--{opt.replace('_', '-')}", str(value)]
    if getattr(args, "burst", False):
        out.append("--burst")
    return out


def producer_options_summary(args):
    keys = ("rate", "threads", "acks", "idempotence", "linger_ms", "batch_size", "compression", "payload_bytes", "burst")
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) not in (None, False)}


def start_producers(args):
    impls, topics = selected(args.impl, args.topic)
    print_banner("STARTING MESSAGE PRODUCERS")
    threads = getattr(args, "threads", None)
    if threads is not None:
        if "ck" not in impls:
            print("❌ --threads applies to confluent-kafka (--impl ck or both); kafka-python runs one sender per topic")
            sys.exit(1)
        if threads < 1:
            print("❌ --threads must be >= 1")
            sys.exit(1)
    rate = getattr(args, "rate", None)
    if rate is not None and rate < 0:
        print("❌ --rate must be >= 0")
        sys.exit(1)
    bootstrap = ENV.get("KAFKA_BOOTSTRAP_HOST", f"{HOST}:9092")
    host, _, port = bootstrap.split(",")[0].rpartition(":")
    if not port_open(host, int(port), timeout=3):
        print(f"  ❌ Kafka is not reachable at {bootstrap}. Start the stack first: python setup.py up")
        sys.exit(1)
    for impl in impls:
        script = PRODUCER_SCRIPTS[impl]
        for topic in topics:
            name = f"{impl}_{topic}"
            label = producer_label(impl, topic)
            if live_process(name, script.name):
                print(f"  ⚠️ {label} already running (PID {read_pid(name)})")
                continue
            lam = ROOT / f"lambda_{topic}.txt"
            if rate is not None:
                lam.write_text(f"{rate:g}\n")
            elif not lam.exists():
                lam.write_text("5\n")
            threads_file = ROOT / f"producers_{topic}.txt"
            if impl == "ck":
                if threads is not None:
                    threads_file.write_text(f"{threads}\n")
                elif not threads_file.exists():
                    threads_file.write_text("2\n")
            port = PRODUCER_PORTS[(impl, topic)]
            log_path = LOG_DIR / f"{name}.out"
            extra = producer_script_args(impl, args) + list(getattr(args, "producer_args", None) or [])
            proc = spawn_detached(
                [sys.executable, str(script), topic, str(port), "--bootstrap", bootstrap, *extra], log_path
            )
            pid_file(name).write_text(str(proc.pid))
            (RUN_DIR / f"{name}.json").write_text(json.dumps({"args": extra, **producer_options_summary(args)}))
            time.sleep(1.5)
            if proc.poll() is None:
                print(f"  ✅ {label} started (PID {proc.pid}, metrics :{port}, log {log_path.relative_to(ROOT)})")
            else:
                pid_file(name).unlink(missing_ok=True)
                print(f"  ❌ {label} exited immediately; see {log_path.relative_to(ROOT)}")
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
                if "Address already in use" in tail or "WinError 10048" in tail:
                    print(f"     Metrics port {port} is busy: another producer is using it, or (WSL2 mirrored")
                    print("     networking) Windows still holds it from a Windows-side run; retry in a few minutes.")


def stop_producers(impls, topics, include_ramps=False):
    print_banner("STOPPING MESSAGE PRODUCERS")
    for impl in impls:
        for topic in topics:
            stop_process(f"{impl}_{topic}", PRODUCER_SCRIPTS[impl].name, producer_label(impl, topic))
    if include_ramps:
        for topic in topics:
            stop_process(f"ramp_{topic}", "setup.py", f"lambda ramp [{topic}]")


def producers_status(_args):
    print_banner("PRODUCER STATUS")
    for impl in ("kp", "ck"):
        for topic in DATA_TOPICS:
            name = f"{impl}_{topic}"
            label = producer_label(impl, topic)
            proc = live_process(name, PRODUCER_SCRIPTS[impl].name)
            lam = (ROOT / f"lambda_{topic}.txt")
            lam_value = lam.read_text().strip() if lam.exists() else "?"
            extra = ""
            if impl == "ck":
                pf = ROOT / f"producers_{topic}.txt"
                extra = f" • threads={pf.read_text().strip() if pf.exists() else '?'}"
            if proc:
                opts_file = RUN_DIR / f"{name}.json"
                opts = ""
                if opts_file.exists():
                    try:
                        given = json.loads(opts_file.read_text()).get("args", [])
                        opts = f" • options: {' '.join(given)}" if given else ""
                    except ValueError:
                        pass
                print(f"  ✅ {label}: running (PID {proc.pid}) • port={PRODUCER_PORTS[(impl, topic)]} • lambda={lam_value}{extra}{opts}")
            else:
                print(f"  ❌ {label}: not running")
    for topic in DATA_TOPICS:
        proc = live_process(f"ramp_{topic}", "setup.py")
        if proc:
            print(f"  📈 lambda ramp [{topic}]: running (PID {proc.pid})")


def validate_topic(topic):
    if topic not in DATA_TOPICS:
        print(f"❌ Unknown topic '{topic}'. Use one of: {', '.join(DATA_TOPICS)}")
        sys.exit(1)


def set_lambda(args):
    validate_topic(args.topic)
    if args.value < 0:
        print("❌ Lambda must be >= 0")
        sys.exit(1)
    (ROOT / f"lambda_{args.topic}.txt").write_text(f"{args.value:g}\n")
    print(f"✅ lambda for {args.topic} set to {args.value:g} (producers pick it up within 2 s)")


def set_prods(args):
    validate_topic(args.topic)
    if args.count < 0:
        print("❌ Producer thread count must be >= 0")
        sys.exit(1)
    (ROOT / f"producers_{args.topic}.txt").write_text(f"{args.count}\n")
    print(f"✅ confluent-kafka producer threads for {args.topic} set to {args.count}")


def ramp(args):
    validate_topic(args.topic)
    if args.foreground:
        run_ramp(args.topic, args.start, args.end, args.step, args.interval)
        return
    name = f"ramp_{args.topic}"
    if live_process(name, "setup.py"):
        print(f"⚠️ A ramp for {args.topic} is already running (PID {read_pid(name)}); stop it with: python setup.py producers stop")
        return
    log_path = LOG_DIR / f"{name}.out"
    proc = spawn_detached(
        [sys.executable, str(ROOT / "setup.py"), "producers", "ramp", args.topic,
         "--from", f"{args.start:g}", "--to", f"{args.end:g}", "--step", f"{args.step:g}",
         "--interval", f"{args.interval:g}", "--foreground"],
        log_path,
    )
    pid_file(name).write_text(str(proc.pid))
    steps = int((args.end - args.start) / args.step) + 1 if args.step > 0 else 1
    print(f"📈 Lambda ramp for {args.topic}: {args.start:g} → {args.end:g} step {args.step:g} every {args.interval:g}s "
          f"(~{steps * args.interval / 60:.1f} min), PID {proc.pid}, log {log_path.relative_to(ROOT)}")
    print("   Stop early with: python setup.py producers stop")


def run_ramp(topic, start, end, step, interval):
    stopping = []

    def request_stop(signum, frame):
        stopping.append(signum)

    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)

    lam_file = ROOT / f"lambda_{topic}.txt"
    value = start
    while value <= end + 1e-9 and not stopping:
        lam_file.write_text(f"{value:g}\n")
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}: λ[{topic}] = {value:g}", flush=True)
        deadline = time.time() + interval
        while time.time() < deadline and not stopping:
            time.sleep(0.5)
        if step <= 0:
            break
        value += step
    print("Ramp stopped by signal." if stopping else "Ramp complete.", flush=True)


def producer_logs(args):
    validate_topic(args.topic)
    impls = ["kp", "ck"] if args.impl == "both" else [args.impl]
    for impl in impls:
        path = LOG_DIR / f"{impl}_{args.topic}.out"
        print_banner(f"{producer_label(impl, args.topic)} — last {args.lines} lines of {path.relative_to(ROOT)}")
        if not path.exists():
            print("  (no log yet)")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-args.lines:]))


def producers_cmd(args):
    action = args.producer_action
    if action == "start":
        start_producers(args)
    elif action == "stop":
        impls, topics = selected(args.impl, args.topic)
        stop_producers(impls, topics, include_ramps=True)
    elif action == "status":
        producers_status(args)
    elif action == "set-lambda":
        set_lambda(args)
    elif action == "set-prods":
        set_prods(args)
    elif action == "ramp":
        ramp(args)
    elif action == "logs":
        producer_logs(args)


# ==============================================================================
# Tuning: every knob is a setup.py option, saved to .env (students never edit files)
# ==============================================================================
TUNABLES = {
    "Spark": ["SPARK_TRIGGER_INTERVAL", "SPARK_MAX_OFFSETS_PER_TRIGGER", "SPARK_APP_CORES", "SPARK_WORKER_CORES",
              "SPARK_WORKER_CPUS", "SPARK_WORKER_MEMORY", "SPARK_EXECUTOR_MEMORY", "SPARK_DRIVER_MEMORY", "SPARK_CONSUMER_MEM_LIMIT"],
    "Kafka": ["KAFKA_PARTITIONS", "KAFKA_HEAP_OPTS"],
    "Monitoring": ["CADVISOR_ENABLED", "GRAFANA_ANONYMOUS_ACCESS"],
}
SPARK_CONSUMER_SERVICES = list(CONSUMERS)


def set_env_values(values):
    """Set KEY=VALUE pairs in .env, keeping comments, order and every other line."""
    ensure_env_file()
    _write_env_values(values)


def parse_memory(text):
    """'512m' / '2g' -> MiB, or None if invalid."""
    import re
    m = re.fullmatch(r"(\d+)([mMgG])", str(text).strip())
    if not m:
        return None
    return int(m.group(1)) * (1024 if m.group(2).lower() == "g" else 1)


def normalize_trigger(text):
    """'500ms' / '5s' / '2m' / '5 seconds' / 'off' -> Spark interval string ('' = off)."""
    import re
    t = str(text).strip().lower()
    if t in ("", "off", "none", "0"):
        return ""
    m = re.fullmatch(r"(\d+)\s*([a-z]+)", t)
    units = {"ms": "milliseconds", "millisecond": "milliseconds", "milliseconds": "milliseconds",
             "s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds", "seconds": "seconds",
             "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes"}
    if not m or m.group(2) not in units or int(m.group(1)) == 0:
        print(f"❌ --trigger must look like 500ms, 5s, 2m or off (got '{text}')")
        sys.exit(1)
    return f"{m.group(1)} {units[m.group(2)]}"


def stack_running():
    return container_state("kafka") == "running"


def apply_spark_changes(worker_changed):
    if not stack_running():
        print("  ℹ️ Stack is not running; the new settings apply at the next `python setup.py up`.")
        return
    if worker_changed:
        run_command(compose("up", "-d", "--wait", "spark-worker"), "Recreating the Spark worker")
    run_command(compose("up", "-d", "--force-recreate", "--no-deps", *SPARK_CONSUMER_SERVICES),
                "Restarting the Spark consumers (they resume from their checkpoints)")


def spark_cmd(args):
    changes = {}
    if args.trigger is not None:
        changes["SPARK_TRIGGER_INTERVAL"] = normalize_trigger(args.trigger)
    if args.max_offsets is not None:
        mo = args.max_offsets.strip().lower()
        if mo in ("off", "none", "0", ""):
            changes["SPARK_MAX_OFFSETS_PER_TRIGGER"] = ""
        elif mo.isdigit():
            changes["SPARK_MAX_OFFSETS_PER_TRIGGER"] = mo
        else:
            print(f"❌ --max-offsets must be a positive number or off (got '{args.max_offsets}')")
            sys.exit(1)
    for opt, key in (("cores", "SPARK_APP_CORES"), ("worker_cores", "SPARK_WORKER_CORES")):
        value = getattr(args, opt)
        if value is not None:
            if value < 1:
                print(f"❌ --{opt.replace('_', '-')} must be >= 1")
                sys.exit(1)
            changes[key] = str(value)
    if args.worker_cpus is not None:
        if args.worker_cpus < 0:
            print("❌ --worker-cpus must be >= 0 (0 = unlimited)")
            sys.exit(1)
        changes["SPARK_WORKER_CPUS"] = f"{args.worker_cpus:g}"
    for opt, key in (("worker_memory", "SPARK_WORKER_MEMORY"), ("executor_memory", "SPARK_EXECUTOR_MEMORY"),
                     ("driver_memory", "SPARK_DRIVER_MEMORY"), ("consumer_memory", "SPARK_CONSUMER_MEM_LIMIT")):
        value = getattr(args, opt)
        if value is not None:
            if parse_memory(value) is None:
                print(f"❌ --{opt.replace('_', '-')} must look like 512m or 2g (got '{value}')")
                sys.exit(1)
            changes[key] = value.lower()

    if not changes:
        print_banner("SPARK SETTINGS")
        show_tunables(["Spark"])
        print("\n  Change with e.g.: python setup.py spark --trigger 10s --max-offsets 5000 --cores 2 --worker-cores 4")
        return

    # validate the combination before saving anything
    merged = {**{k: ENV.get(k, "") for k in TUNABLES["Spark"]}, **changes}
    app_cores, worker_cores = int(merged["SPARK_APP_CORES"]), int(merged["SPARK_WORKER_CORES"])
    exec_mb, worker_mb = parse_memory(merged["SPARK_EXECUTOR_MEMORY"]), parse_memory(merged["SPARK_WORKER_MEMORY"])
    driver_mb, limit_mb = parse_memory(merged["SPARK_DRIVER_MEMORY"]), parse_memory(merged["SPARK_CONSUMER_MEM_LIMIT"])
    problems = []
    if worker_cores < 2 * app_cores:
        problems.append(f"worker cores ({worker_cores}) < 2 apps x --cores {app_cores}: one sink would get no executors "
                        f"(use --worker-cores {2 * app_cores})")
    if worker_mb < 2 * app_cores * exec_mb:
        need = 2 * app_cores * exec_mb
        problems.append(f"worker memory ({merged['SPARK_WORKER_MEMORY']}) < 2 apps x {app_cores} executors x "
                        f"{merged['SPARK_EXECUTOR_MEMORY']} (use --worker-memory {max(1, -(-need // 1024))}g)")
    if limit_mb < driver_mb + 256:
        problems.append(f"consumer memory limit ({merged['SPARK_CONSUMER_MEM_LIMIT']}) must exceed driver memory "
                        f"({merged['SPARK_DRIVER_MEMORY']}) by at least 256m")
    if problems:
        print("❌ These Spark settings would not work:")
        for problem in problems:
            print(f"   • {problem}")
        print("   Nothing was changed.")
        sys.exit(1)
    info = run_command(["docker", "info", "--format", "{{.NCPU}}"], capture=True, fatal=False)
    if info.stdout.strip().isdigit() and worker_cores > int(info.stdout.strip()):
        print(f"  ⚠️ Docker has {info.stdout.strip()} CPUs; a {worker_cores}-core worker will oversubscribe them.")
    if info.stdout.strip().isdigit() and float(merged.get("SPARK_WORKER_CPUS") or 0) > int(info.stdout.strip()):
        print(f"❌ --worker-cpus {merged['SPARK_WORKER_CPUS']} exceeds Docker's {info.stdout.strip()} CPUs")
        sys.exit(1)

    set_env_values(changes)
    print_banner("SPARK SETTINGS UPDATED")
    for key, value in changes.items():
        print(f"  • {key} = {value or '(default)'}")
    worker_keys = {"SPARK_WORKER_CORES", "SPARK_WORKER_MEMORY", "SPARK_WORKER_CPUS"}
    apply_spark_changes(worker_changed=bool(worker_keys & set(changes)))


def topic_partitions():
    """{topic: partition count} for the data topics, or {} if Kafka is unreachable."""
    result = run_command(kafka_cli("/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server", "localhost:29092",
                                   "--describe", "--topic", ",".join(DATA_TOPICS)), capture=True, fatal=False)
    counts = {}
    for line in result.stdout.splitlines():
        if line.startswith("Topic:") and "PartitionCount:" in line:
            fields = dict(part.split(": ", 1) for part in line.split("\t") if ": " in part)
            counts[fields["Topic"].strip()] = int(fields["PartitionCount"].strip())
    return counts


def topics_cmd(args):
    if not stack_running():
        print("❌ Kafka is not running. Start the stack first: python setup.py up")
        sys.exit(1)
    current = topic_partitions()
    if args.partitions is None:
        print_banner("TOPIC PARTITIONS")
        for topic in DATA_TOPICS:
            print(f"  • {topic}: {current.get(topic, '?')} partition(s)")
        print("\n  Change with: python setup.py topics --partitions 4 [--topic topic-json]")
        return
    if args.partitions < 1:
        print("❌ --partitions must be >= 1")
        sys.exit(1)
    print_banner("CHANGING TOPIC PARTITIONS")
    for topic in ([args.topic] if args.topic else DATA_TOPICS):
        have = current.get(topic)
        if have is None:
            print(f"  ❌ {topic}: not found")
        elif args.partitions == have:
            print(f"  ✅ {topic}: already {have} partition(s)")
        elif args.partitions < have:
            print(f"  ❌ {topic}: has {have}; Kafka can only increase partitions.")
            print("     To start over with fewer: python setup.py clean --volumes, set it with this command after `up`.")
        else:
            run_command(kafka_cli("/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server", "localhost:29092",
                                  "--alter", "--topic", topic, "--partitions", str(args.partitions)),
                        capture=True, fatal=True)
            print(f"  ✅ {topic}: {have} → {args.partitions} partitions (Spark picks them up automatically)")
    if not args.topic:
        set_env_values({"KAFKA_PARTITIONS": str(args.partitions)})


def show_tunables(groups=None):
    for group in groups or TUNABLES:
        print(f"  {group}:")
        for key in TUNABLES[group]:
            value = ENV.get(key, "")
            default = _env_lines(ENV_EXAMPLE).get(key, "=").split("=", 1)[1].strip()
            mark = "" if value == default else f"   (default: {default or 'empty'})"
            shown = value if value else "(empty: Spark default)"
            print(f"    {key:<32} {shown}{mark}")


def secrets_cmd(args):
    """Show the credentials in .env, or --rotate: generate new ones and apply them everywhere."""
    ensure_env_file()
    if args.rotate:
        grafana_db = run_command(["docker", "volume", "inspect", f"{PROJECT_NAME}_grafana_data"],
                                 capture=True, fatal=False).returncode == 0
        if grafana_db and container_state("grafana") != "running":
            print("❌ Grafana stores its admin password in its own database, so it must be running to change it.")
            print("   Start the stack first (python setup.py up), then run: python setup.py secrets --rotate")
            sys.exit(1)
        print_banner("ROTATING CREDENTIALS")
        set_env_values({k: generate_secret(k) for k in SECRET_KEYS})
        render_s3_config()
        if stack_running():
            run_command(compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "seaweedfs"),
                        "Restarting SeaweedFS with the new S3 keys")
            run_command(compose("up", "-d", "--no-deps", "--wait", "seaweedfs-admin"),
                        "Restarting the SeaweedFS admin console with its new password")
            run_command(["docker", "exec", "grafana", "grafana", "cli", "admin", "reset-admin-password",
                         ENV["GRAFANA_ADMIN_PASSWORD"]], "Setting the new Grafana admin password", capture=True)
            run_command(compose("up", "-d", "--no-deps", "--wait", "grafana"), "Restarting Grafana")
            run_command(compose("up", "-d", "--no-deps", "--force-recreate", *SPARK_CONSUMER_SERVICES),
                        "Restarting the Spark consumers with the new S3 keys")
            try:
                s3_client().list_objects_v2(Bucket=ENV["S3_BUCKET"], MaxKeys=1)
                grafana_get("/api/user")
                print("  ✅ Verified: S3 accepts the new keys and Grafana accepts the new password")
            except Exception as exc:
                print(f"  ⚠️ Could not verify the new credentials yet ({str(exc)[:80]}); check: python setup.py status")
        else:
            print("  ℹ️ Stack not running; the new credentials take effect at the next: python setup.py up")
    print_banner("CREDENTIALS (stored in .env, which only you can read)")
    e = ENV
    rows = [
        ("Grafana", f"http://localhost:{e['GRAFANA_PORT']}", e["GRAFANA_ADMIN_USER"], e["GRAFANA_ADMIN_PASSWORD"]),
        ("SeaweedFS admin console", f"http://localhost:{e['SEAWEED_ADMIN_PORT']}", e["SEAWEED_ADMIN_USER"],
         e["SEAWEED_ADMIN_PASSWORD"]),
        ("S3 API (access / secret key)", f"http://localhost:{e['S3_PORT']}", e["S3_ACCESS_KEY"], e["S3_SECRET_KEY"]),
    ]
    for name, url, user, secret in rows:
        print(f"  • {name:<30} {url}")
        print(f"      user / key : {user}")
        print(f"      password   : {secret}")
    weak = weak_secrets()
    if weak:
        print(f"\n  ⚠️ Still default or placeholder: {', '.join(weak)}. Fix with: python setup.py secrets --rotate")


def config_cmd(args):
    ensure_env_file()
    if args.reset:
        defaults = {k: v.split("=", 1)[1].strip() for k, v in _env_lines(ENV_EXAMPLE).items()
                    if any(k in keys for keys in TUNABLES.values())}
        set_env_values(defaults)
        for topic in DATA_TOPICS:
            (ROOT / f"lambda_{topic}.txt").write_text("5\n")
            (ROOT / f"producers_{topic}.txt").write_text("2\n")
        print_banner("TUNABLES RESET TO DEFAULTS")
        print("  λ = 5 and 2 confluent-kafka threads per topic; credentials and ports untouched.")
        if stack_running():
            apply_spark_changes(worker_changed=True)
            print("  ℹ️ Topic partitions can't be reduced; `clean --volumes` + `up` starts over with the default.")
    print_banner("CURRENT SETTINGS")
    show_tunables()
    if stack_running():
        parts = topic_partitions()
        print("  Topics (live):")
        for topic in DATA_TOPICS:
            print(f"    {topic:<32} {parts.get(topic, '?')} partition(s)")
    print("  Producers:")
    for topic in DATA_TOPICS:
        lam = ROOT / f"lambda_{topic}.txt"
        thr = ROOT / f"producers_{topic}.txt"
        print(f"    {topic:<32} λ={lam.read_text().strip() if lam.exists() else '5'}  "
              f"ck threads={thr.read_text().strip() if thr.exists() else '2'}")
    print("\n  Change with: python setup.py spark …  |  topics --partitions N  |  producers start --rate/--threads …")


# ==============================================================================
# Observability (replaces check_services.sh and pipeline_monitor.sh)
# ==============================================================================
def status(_args):
    e = ENV
    cadvisor_on = container_state("cadvisor") == "running"
    print_banner("SERVICE HEALTH")
    checks = [
        ("Kafka broker", "tcp", int(e["KAFKA_PORT"]), None),
        ("Kafka JMX exporter", "http", f"http://{HOST}:{e['KAFKA_JMX_PORT']}/metrics", None),
        ("Kafka exporter", "http", f"http://{HOST}:{e['KAFKA_EXPORTER_PORT']}/metrics", None),
        ("S3 API (SeaweedFS)", "http", f"http://{HOST}:{e['S3_PORT']}/", None),
        ("SeaweedFS master", "http", f"http://{HOST}:{e['SEAWEED_MASTER_PORT']}/cluster/status", None),
        ("SeaweedFS filer", "http", f"http://{HOST}:{e['SEAWEED_FILER_PORT']}/", None),
        ("SeaweedFS volume server", "http", f"http://{HOST}:{e['SEAWEED_VOLUME_PORT']}/ui/index.html", None),
        ("SeaweedFS admin UI", "http", f"http://{HOST}:{e['SEAWEED_ADMIN_PORT']}/", None),
        ("SeaweedFS metrics", "http", f"http://{HOST}:{e['SEAWEED_METRICS_PORT']}/metrics", None),
        ("Spark master UI", "http", f"http://{HOST}:{e['SPARK_MASTER_UI_PORT']}/", None),
        ("Spark worker UI", "http", f"http://{HOST}:{e['SPARK_WORKER_UI_PORT']}/", None),
        ("Spark consumer JSON UI", "http", f"http://{HOST}:{e['SPARK_JSON_UI_PORT']}/", None),
        ("Spark consumer Parquet UI", "http", f"http://{HOST}:{e['SPARK_PARQUET_UI_PORT']}/", None),
        ("Prometheus", "http", f"http://{HOST}:{e['PROMETHEUS_PORT']}/-/healthy", None),
        ("Grafana", "http", f"http://{HOST}:{e['GRAFANA_PORT']}/api/health", None),
        ("cAdvisor", "http", f"http://{HOST}:{e['CADVISOR_PORT']}/healthz", "cadvisor"),
    ]
    for name, kind, target, optional in checks:
        if optional == "cadvisor" and not cadvisor_on:
            print(f"  ⏸️  {name:<26} disabled")
            continue
        if kind == "tcp":
            ok = port_open(HOST, target)
            where = f"{HOST}:{target}"
        else:
            code = http_status(target)
            ok = code is not None and code < 500
            where = target
        print(f"  {'✅' if ok else '❌'} {name:<26} {where}")

    print_banner("PROMETHEUS SCRAPE TARGETS")
    try:
        data = http_json(f"http://{HOST}:{e['PROMETHEUS_PORT']}/api/v1/targets")
    except Exception as exc:
        print(f"  ❌ Prometheus API unreachable: {exc}")
        return
    running_producers = set()
    for (impl, topic), port in PRODUCER_PORTS.items():
        if live_process(f"{impl}_{topic}", PRODUCER_SCRIPTS[impl].name):
            running_producers.add(port)
    for target in sorted(data["data"]["activeTargets"], key=lambda t: t["labels"]["job"]):
        job = target["labels"]["job"]
        health = target["health"]
        port = int(target["labels"]["instance"].rsplit(":", 1)[-1])
        if health == "up":
            mark, note = "✅", "up"
        elif job == "cadvisor" and not cadvisor_on:
            mark, note = "⏸️ ", "disabled"
        elif job.startswith("kafka-producer") and port not in running_producers:
            mark, note = "⏸️ ", "producer not started"
        else:
            mark, note = "❌", f"{health}: {target.get('lastError', '')[:60]}"
        print(f"  {mark} {job:<34} {note}")


def kafka_cli(*args):
    """Run a Kafka CLI tool inside the broker container (KAFKA_OPTS cleared for the JMX port)."""
    return ["docker", "exec", "-e", "KAFKA_OPTS=", "kafka", *args]


def monitor(_args):
    bootstrap = ["--bootstrap-server", "localhost:29092"]
    print_banner("Step 1: Kafka topics")
    run_command(kafka_cli("/opt/kafka/bin/kafka-topics.sh", *bootstrap, "--list"), fatal=False)

    for i, topic in enumerate(DATA_TOPICS):
        print_banner(f"Step 2{'AB'[i]}: Describe topic '{topic}'")
        run_command(kafka_cli("/opt/kafka/bin/kafka-topics.sh", *bootstrap, "--describe", "--topic", topic), fatal=False)

    for i, topic in enumerate(DATA_TOPICS):
        print_banner(f"Step 3{'AB'[i]}: End offsets for '{topic}' (topic:partition:offset)")
        for attempt in (1, 2):
            result = run_command(kafka_cli("/opt/kafka/bin/kafka-get-offsets.sh", *bootstrap, "--topic", topic),
                                 capture=True, fatal=False)
            if result.returncode == 0:
                print(result.stdout.strip())
                break
            if attempt == 2:
                print(f"  ❌ Offset lookup failed: {result.stdout.strip().splitlines()[0] if result.stdout.strip() else result.stderr.strip()[:200]}")

    for i, container in enumerate(CONSUMERS):
        print_banner(f"Step 4{'AB'[i]}: {container} log (last 3 lines)")
        run_command(["docker", "logs", "--tail", "3", container], fatal=False)

    for i, (container, sink) in enumerate(CONSUMERS.items()):
        print_banner(f"Step 5{'AB'[i]}: Latest checkpoint offsets ({sink})")
        result = run_command(
            ["docker", "exec", container, "ls", "-t", f"/opt/spark/work-dir/checkpoints/{sink}/offsets"],
            capture=True, fatal=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            batches = result.stdout.split()
            print(f"  {len(batches)} micro-batches committed; newest: {', '.join(batches[:3])}")
        else:
            print(f"  ❌ No checkpoint yet for {sink}")

    for i, prefix in enumerate(("json/", "parquet/")):
        print_banner(f"Step 6{'AB'[i]}: S3 output s3://{ENV['S3_BUCKET']}/{prefix} (2 most recent)")
        show_s3_objects(prefix, limit=2)

    print_banner("Step 7: Pipeline check complete")


def list_objects(prefix):
    client = s3_client()
    objects = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=ENV["S3_BUCKET"], Prefix=prefix):
        objects.extend(o for o in page.get("Contents", []) if "_spark_metadata" not in o["Key"])
    return objects


def show_s3_objects(prefix, limit):
    try:
        objects = list_objects(prefix)
    except ImportError:
        print("  ❌ boto3 not installed: pip install -r requirements.txt")
        return
    except Exception as exc:
        print(f"  ❌ S3 listing failed: {exc}")
        return
    if not objects:
        print(f"  ❌ No objects under {prefix}")
        return
    objects.sort(key=lambda o: o["LastModified"], reverse=True)
    total = sum(o["Size"] for o in objects)
    print(f"  {len(objects)} objects, {total / 1024:.1f} KiB total")
    for o in objects[:limit]:
        print(f"  {o['LastModified']:%Y-%m-%d %H:%M:%S}  {o['Size']:>9}  {o['Key']}")


def s3_ls(args):
    print_banner(f"S3 OBJECTS: s3://{ENV['S3_BUCKET']}/{args.prefix}")
    show_s3_objects(args.prefix, limit=args.limit)


def smoke_test(_args):
    print_banner("KAFKA END-TO-END SMOKE TEST (test-topic)")
    event = json.dumps({
        "device_id": "sensor-test-001", "battery_level": 88, "motion_detected": True,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    run_command(
        ["docker", "exec", "-i", "-e", "KAFKA_OPTS=", "kafka", "/opt/kafka/bin/kafka-console-producer.sh",
         "--bootstrap-server", "localhost:29092", "--topic", "test-topic"],
        "Producing one event to 'test-topic'", input_text=event + "\n",
    )
    result = run_command(
        kafka_cli("/opt/kafka/bin/kafka-console-consumer.sh", "--bootstrap-server", "localhost:29092",
                  "--topic", "test-topic", "--from-beginning", "--timeout-ms", "10000"),
        "Reading 'test-topic' back", capture=True, fatal=False,
    )
    if event in result.stdout:
        print("✅ Event produced and consumed through Kafka.")
    else:
        print("❌ Event not read back.\n" + (result.stdout or "") + (result.stderr or ""))
        sys.exit(1)


def submit(args):
    job = Path(args.job)
    if not job.exists():
        print(f"❌ Spark job '{args.job}' not found (jobs live under ./jobs).")
        sys.exit(1)
    container_job = f"/opt/spark/jobs/{job.name}"
    print_banner(f"SUBMITTING SPARK JOB: {job.name}")
    print("  Runs in the foreground on the cluster; its driver runs in spark-master.")
    run_command(
        ["docker", "exec", "-e", f"S3_ACCESS_KEY={ENV['S3_ACCESS_KEY']}", "-e", f"S3_SECRET_KEY={ENV['S3_SECRET_KEY']}",
         "spark-master", "/opt/spark/bin/spark-submit", "--master", "spark://spark-master:7077",
         "--jars", "/opt/spark/custom-jars/*", "--conf", "spark.cores.max=1", "--conf", "spark.executor.cores=1",
         "--conf", "spark.ui.port=4050", container_job],
        fatal=False,
    )


def grafana_export(args):
    uids = [args.uid] if args.uid else [d["uid"] for d in grafana_get("/api/search?type=dash-db")]
    for uid in uids:
        dash = grafana_get(f"/api/dashboards/uid/{uid}")["dashboard"]
        dash.pop("id", None)
        dash.pop("version", None)
        out = DASHBOARD_DIR / f"{uid}.json"
        out.write_text(json.dumps(dash, indent=2) + "\n", encoding="utf-8")
        print(f"  💾 {dash.get('title')} → {out.relative_to(ROOT)}")


# ==============================================================================
# Performance research (implemented in tools/perf.py)
# ==============================================================================
def _perf():
    from tools import perf
    return perf


def bench_cmd(args):
    if args.list or not args.scenario:
        _perf().list_scenarios()
        return
    _perf().bench_cmd(sys.modules[__name__], args)


def audit_cmd(args):
    _perf().audit_cmd(sys.modules[__name__], args)


def chaos_cmd(args):
    _perf().chaos_cmd(sys.modules[__name__], args)


def compact_cmd(args):
    _perf().compact_cmd(sys.modules[__name__], args)


# ==============================================================================
# CLI
# ==============================================================================
def build_parser():
    parser = argparse.ArgumentParser(
        prog="python setup.py",
        description="CSE 6332 Kafka → Spark → S3 pipeline controller (Windows / Linux / macOS).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python setup.py install                      first-time setup + start everything
  python setup.py producers start              start all 4 producers (kp + ck, both topics)
  python setup.py producers start --impl ck --topic topic-json
  python setup.py producers start --impl ck -- --acks 1 --idempotence false --compression lz4
  python setup.py producers set-lambda topic-json 20
  python setup.py producers ramp topic-json --from 5 --to 50 --step 5 --interval 60
  python setup.py status | monitor
  python setup.py shutdown                     producers → consumers → containers (data kept)
  python setup.py up --no-cadvisor             restart without cAdvisor to save resources""",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("install", help="download jars, render config, pip install, start the stack")
    p.add_argument("--no-cadvisor", action="store_true", help="do not start cAdvisor")
    p.set_defaults(func=install)

    for name in ("up", "start"):
        p = sub.add_parser(name, help="start (or create) all containers and wait until healthy")
        p.add_argument("--no-cadvisor", action="store_true", help="do not start cAdvisor (saves memory/CPU)")
        p.set_defaults(func=up)

    sub.add_parser("shutdown", help="stop producers, then consumers, then all containers (data kept)").set_defaults(func=shutdown)
    sub.add_parser("stop", help="same as shutdown").set_defaults(func=shutdown)
    sub.add_parser("down", help="stop producers and remove containers (volumes kept)").set_defaults(func=down)

    p = sub.add_parser("restart", help="restart all containers, or one service")
    p.add_argument("service", nargs="?")
    p.set_defaults(func=restart)

    p = sub.add_parser("logs", help="show container logs")
    p.add_argument("service", nargs="?")
    p.add_argument("--tail", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=logs)

    sub.add_parser("ps", help="list containers").set_defaults(func=ps)
    sub.add_parser("init", help="re-run topic and bucket creation").set_defaults(func=init)
    sub.add_parser("status", help="endpoint health + Prometheus targets").set_defaults(func=status)
    sub.add_parser("monitor", help="topics, offsets, consumer logs, checkpoints, newest S3 objects").set_defaults(func=monitor)
    sub.add_parser("test", help="produce/consume one event on test-topic").set_defaults(func=smoke_test)
    sub.add_parser("doctor", help="check Docker, resources, ports and image architectures").set_defaults(func=doctor)
    sub.add_parser("render", help="re-render volumes/seaweedfs/s3.json from .env").set_defaults(func=lambda a: render_s3_config())

    p = sub.add_parser("submit", help="submit a job from ./jobs to the Spark cluster (foreground)")
    p.add_argument("job", nargs="?", default="jobs/kafka_consumer.py")
    p.set_defaults(func=submit)

    p = sub.add_parser("s3-ls", help="list objects in the S3 bucket")
    p.add_argument("prefix", nargs="?", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=s3_ls)

    p = sub.add_parser("grafana-export", help="save dashboards from Grafana into volumes/grafana/dashboards")
    p.add_argument("uid", nargs="?", help="dashboard uid (default: all)")
    p.set_defaults(func=grafana_export)

    p = sub.add_parser("clean", help="remove containers and everything setup.py created or downloaded (jars, "
                                      "config, logs); --volumes also deletes all data")
    p.add_argument("--volumes", action="store_true",
                   help="ALSO delete all data: Kafka, S3 (SeaweedFS), checkpoints, Prometheus, the Grafana database, "
                        "and leftover volumes from older versions")
    p.add_argument("--keep-jars", action="store_true", help="keep the downloaded jars (avoids a ~290 MB re-download)")
    p.add_argument("--results", action="store_true", help="also delete results/ (your experiment results)")
    p.add_argument("--jars", action="store_true", help=argparse.SUPPRESS)  # old flag; jars are now always removed
    p.set_defaults(func=clean)

    p = sub.add_parser("spark", help="show or change Spark tuning (trigger, rate limit, cores, memory)")
    p.add_argument("--trigger", metavar="INTERVAL", help="micro-batch interval: 500ms, 5s, 1m or off")
    p.add_argument("--max-offsets", metavar="N", help="max Kafka records per micro-batch, or off")
    p.add_argument("--cores", type=int, metavar="N", help="executor cores per consumer app")
    p.add_argument("--worker-cores", type=int, metavar="N", help="cores the Spark worker offers (>= 2 x --cores)")
    p.add_argument("--worker-cpus", type=float, metavar="CPUS",
                   help="Docker CPU cap for the worker/executors, e.g. 0.5 (0 = unlimited): the 'instance size'")
    p.add_argument("--worker-memory", metavar="SIZE", help="memory the worker offers, e.g. 4g")
    p.add_argument("--executor-memory", metavar="SIZE", help="memory per executor, e.g. 1g")
    p.add_argument("--driver-memory", metavar="SIZE", help="memory per consumer driver, e.g. 1g")
    p.add_argument("--consumer-memory", metavar="SIZE", help="container memory limit per consumer, e.g. 2g")
    p.set_defaults(func=spark_cmd)

    p = sub.add_parser("topics", help="show or increase Kafka topic partitions")
    p.add_argument("--partitions", type=int, metavar="N")
    p.add_argument("--topic", choices=DATA_TOPICS, help="one topic (default: both)")
    p.set_defaults(func=topics_cmd)

    p = sub.add_parser("bench", help="run a scripted performance experiment; results go to results/")
    _perf().add_bench_arguments(p, add_producer_options)
    p.set_defaults(func=bench_cmd)

    p = sub.add_parser("audit", help="count what reached S3 vs Kafka (exactly-once check), files and latency")
    p.add_argument("--json", metavar="FILE", help="also save the full result as JSON")
    p.set_defaults(func=audit_cmd)

    p = sub.add_parser("chaos", help="inject a fault and measure recovery")
    p.add_argument("action", choices=["kill-consumer", "pause-kafka", "restart-s3", "stop-worker"])
    p.add_argument("target", nargs="?", choices=["json", "parquet"], help="for kill-consumer (default json)")
    p.add_argument("--seconds", type=float, help="how long the fault lasts (default 15)")
    p.set_defaults(func=chaos_cmd)

    p = sub.add_parser("compact", help="compact one S3 partition into a few files and compare (small-files study)")
    p.add_argument("--sink", choices=["json", "parquet"], default="parquet")
    p.add_argument("--date", help="partition date YYYY-MM-DD (default: the busiest partition)")
    p.add_argument("--hour", type=int, help="partition hour 0-23")
    p.add_argument("--files", type=int, default=1, help="number of output files (default 1)")
    p.set_defaults(func=compact_cmd)

    p = sub.add_parser("secrets", help="show the generated credentials, or --rotate them (new random values)")
    p.add_argument("--rotate", action="store_true", help="generate new credentials and apply them to the running stack")
    p.set_defaults(func=secrets_cmd)

    p = sub.add_parser("config", help="show all tuning settings, or --reset them to defaults")
    p.add_argument("--reset", action="store_true", help="restore defaults for tuning settings (not credentials)")
    p.set_defaults(func=config_cmd)

    # producers ...
    p = sub.add_parser("producers", help="manage host-side Kafka producers")
    p.set_defaults(func=producers_cmd)
    psub = p.add_subparsers(dest="producer_action", metavar="<action>", required=True)

    for action, text in (("start", "start producers in the background"), ("stop", "signal producers (and ramps) to stop")):
        a = psub.add_parser(action, help=text)
        a.add_argument("--impl", choices=["kp", "ck", "both"], default="both",
                       help="kp = kafka-python, ck = confluent-kafka (default: both)")
        a.add_argument("--topic", choices=DATA_TOPICS, help="one topic (default: both)")
        if action == "start":
            add_producer_options(a)
            a.add_argument("producer_args", nargs="*", metavar="-- EXTRA",
                           help="any other producer-script option, after `--`")

    psub.add_parser("status", help="show producer processes, lambda and thread counts")

    a = psub.add_parser("set-lambda", help="set Poisson lambda (msgs/sec) for a topic")
    a.add_argument("topic")
    a.add_argument("value", type=float)

    a = psub.add_parser("set-prods", help="set confluent-kafka producer thread count for a topic")
    a.add_argument("topic")
    a.add_argument("count", type=int)

    a = psub.add_parser("ramp", help="step lambda up over time (runs in background)")
    a.add_argument("topic")
    a.add_argument("--from", dest="start", type=float, default=5)
    a.add_argument("--to", dest="end", type=float, default=50)
    a.add_argument("--step", type=float, default=5)
    a.add_argument("--interval", type=float, default=60, help="seconds per step")
    a.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)

    a = psub.add_parser("logs", help="show the last lines of a producer's log")
    a.add_argument("topic")
    a.add_argument("--impl", choices=["kp", "ck", "both"], default="both")
    a.add_argument("-n", "--lines", type=int, default=20)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
