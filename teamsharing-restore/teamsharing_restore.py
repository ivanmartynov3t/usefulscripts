#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["questionary>=2.0"]
# ///
"""
Team Sharing backup restore helper.

Automates the "Retrieve a backup" and "Spin up a MongoDB server" steps of the
Ops runbook "How to recover a user's Team Sharing data in case it was
erroneously deleted", but runs MongoDB inside Docker instead of a local
mongod binary:

  1. Select a MongoDB Atlas snapshot archive (.tar.gz / .tgz)
  2. Select a working folder and extract the archive into it
  3. Create and start a MongoDB container (version must match the backup's
     featureCompatibilityVersion - 6.0 at the time the runbook was written)
  4. Verify the container is up and mongod answers
  5. Restore the extracted data files into the container's Docker volume
  6. Verify the restored databases (TEAM_SHARING_ENCRYPTED_PROD, ENCRYPTION_PROD)
  7. Clean up: container, volume, pulled image, extracted files, state

Everything the tool creates is recorded in a state file, so the tool can be
closed or the machine restarted at any point: on the next start it checks
whether each recorded container / volume / folder still exists and drops
references to anything that is gone. Docker objects are also labelled, so
cleanup finds them even if the state file was lost.

The backup contains production customer data. MongoDB is published on
127.0.0.1 only and runs without authentication - do not change the bind
address, and run cleanup as soon as the recovery is done.

Requires Python 3.9+, Docker, and the `questionary` package:
    uv run teamsharing_restore.py
    # or
    pip install questionary && python teamsharing_restore.py
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    import questionary
except ImportError:
    sys.exit("Missing dependency 'questionary'. Run with `uv run teamsharing_restore.py` "
             "or install it with `pip install questionary`.")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LABEL_KEY = "usefulscripts.teamsharing-restore"
LABEL = f"{LABEL_KEY}=1"
CONTAINER_NAME = "teamsharing-restore"
VOLUME_NAME = "teamsharing-restore-data"
DATA_DIR_IN_CONTAINER = "/data/db"
EXTRACT_MARKER = ".teamsharing-restore"
DEFAULT_PORT = 50000
DEFAULT_MONGO_VERSION = "6.0"
MONGO_VERSIONS = ["6.0", "7.0", "8.0", "5.0"]
READY_TIMEOUT_SECONDS = 120

# Databases / collections the runbook works with
EXPECTED_COLLECTIONS = {
    "TEAM_SHARING_ENCRYPTED_PROD": ["user", "folder", "resource", "resourceContent", "permission"],
    "ENCRYPTION_PROD": ["__keyVault"],
}


def _state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "teamsharing-restore"
    return Path.home() / ".local" / "state" / "teamsharing-restore"


STATE_FILE = _state_dir() / "state.json"


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def _empty_state() -> Dict[str, object]:
    return {
        "archive": None,           # selected .tar.gz
        "workdir": None,           # selected working folder
        "extract_dir": None,       # folder this tool created inside workdir
        "dbpath": None,            # folder inside extract_dir holding the WiredTiger files
        "mongo_version": None,
        "image": None,
        "images_pulled": [],       # images this tool pulled (removed on cleanup)
        "port": None,
        "container_id": None,
        "volume": None,
        "restored": False,
        "updated": None,
    }


def load_state() -> Dict[str, object]:
    state = _empty_state()
    if STATE_FILE.is_file():
        try:
            state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"! Could not read state file {STATE_FILE}: {e} - starting with empty state.")
    return state


def save_state(state: Dict[str, object]) -> None:
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

class DockerError(RuntimeError):
    pass


def docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", *args]
    result = subprocess.run(cmd, capture_output=capture, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise DockerError(f"`docker {args[0]}` failed (exit {result.returncode}). {detail}")
    return result


def docker_available() -> Optional[str]:
    """Return an error message if Docker can't be used, else None."""
    if not shutil.which("docker"):
        return "Docker CLI not found on PATH. Install Docker Desktop first."
    r = docker("info", "--format", "{{.ServerVersion}}", check=False)
    if r.returncode != 0:
        return "Docker daemon is not reachable. Start Docker Desktop and try again."
    return None


def container_info(container_id: str) -> Optional[Dict[str, object]]:
    r = docker("inspect", "--type", "container", container_id, check=False)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)[0]


def container_running(container_id: str) -> bool:
    info = container_info(container_id)
    return bool(info and info["State"]["Running"])


def volume_exists(name: str) -> bool:
    return docker("volume", "inspect", name, check=False).returncode == 0


def image_exists(image: str) -> bool:
    return docker("image", "inspect", image, check=False).returncode == 0


def labelled_containers() -> List[str]:
    r = docker("ps", "-a", "-q", "--no-trunc", "--filter", f"label={LABEL}", check=False)
    return r.stdout.split() if r.returncode == 0 else []


def labelled_volumes() -> List[str]:
    r = docker("volume", "ls", "-q", "--filter", f"label={LABEL}", check=False)
    return r.stdout.split() if r.returncode == 0 else []


def mongosh(container_id: str, script: str) -> subprocess.CompletedProcess:
    return docker("exec", container_id, "mongosh", "--quiet", "--norc", "--eval", script, check=False)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Reconcile state with reality (survives restarts / manual deletions)
# ---------------------------------------------------------------------------

def reconcile(state: Dict[str, object], docker_ok: bool) -> None:
    changed = False
    if state["extract_dir"] and not Path(state["extract_dir"]).is_dir():
        print(f"- Extracted folder {state['extract_dir']} no longer exists - reference removed.")
        state["extract_dir"] = state["dbpath"] = None
        state["restored"] = False
        changed = True
    if state["archive"] and not Path(state["archive"]).is_file():
        print(f"- Archive {state['archive']} no longer exists - reference removed.")
        state["archive"] = None
        changed = True
    if docker_ok:
        if state["container_id"] and not container_info(state["container_id"]):
            print(f"- Container {state['container_id'][:12]} no longer exists - reference removed.")
            state["container_id"] = None
            state["restored"] = False
            changed = True
        if state["volume"] and not volume_exists(state["volume"]):
            print(f"- Volume {state['volume']} no longer exists - reference removed.")
            state["volume"] = None
            state["restored"] = False
            changed = True
        known = set(filter(None, [state["container_id"]]))
        orphans = [c for c in labelled_containers() if c not in known]
        if orphans:
            print(f"- {len(orphans)} labelled container(s) not tracked in state - cleanup will remove them.")
    if changed:
        save_state(state)


def print_status(state: Dict[str, object], docker_ok: bool) -> None:
    def show(label: str, value: object) -> None:
        print(f"  {label:<14} {value if value else '-'}")

    print("\nCurrent state")
    show("Archive", state["archive"])
    show("Working dir", state["workdir"])
    show("Extracted to", state["extract_dir"])
    show("Data path", state["dbpath"])
    if state["container_id"]:
        if docker_ok:
            status = "running" if container_running(state["container_id"]) else "stopped"
        else:
            status = "unknown (Docker not reachable)"
        show("Container", f"{CONTAINER_NAME} ({state['container_id'][:12]}, {state['image']}, {status})")
        show("Connect", f"mongodb://127.0.0.1:{state['port']}/?directConnection=true")
    else:
        show("Container", None)
    show("Volume", state["volume"])
    show("Data restored", "yes" if state["restored"] else "no")
    print(f"  (state file: {STATE_FILE})\n")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _is_archive(p: str) -> bool:
    return p.lower().endswith((".tar.gz", ".tgz"))


def select_archive(state: Dict[str, object]) -> None:
    def validate(p: str):
        path = Path(p.strip().strip('"')).expanduser()
        if not path.is_file():
            return "File does not exist"
        if not _is_archive(path.name):
            return "Expected a .tar.gz or .tgz archive"
        return True

    answer = questionary.path("Backup archive (.tar.gz):", default=state["archive"] or "",
                              validate=validate).ask()
    if not answer:
        return
    state["archive"] = str(Path(answer.strip().strip('"')).expanduser().resolve())
    save_state(state)
    print(f"Archive selected: {state['archive']}")


def select_workdir(state: Dict[str, object]) -> None:
    def validate(p: str):
        return True if Path(p.strip().strip('"')).expanduser().is_dir() else "Folder does not exist"

    answer = questionary.path("Working folder (archive is extracted into a new subfolder here):",
                              default=state["workdir"] or "", only_directories=True, validate=validate).ask()
    if not answer:
        return
    state["workdir"] = str(Path(answer.strip().strip('"')).expanduser().resolve())
    save_state(state)
    print(f"Working folder selected: {state['workdir']}")


def _safe_extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, dest: Path) -> None:
    if hasattr(tarfile, "data_filter"):
        tar.extract(member, dest, filter="data")
        return
    # Fallback for Python builds without extraction filters: block path traversal, links and devices
    target = (dest / member.name).resolve()
    if dest.resolve() not in target.parents and target != dest.resolve():
        raise ValueError(f"Refusing to extract {member.name!r} outside the target folder")
    if not (member.isfile() or member.isdir()):
        print(f"  skipping non-regular entry {member.name!r}")
        return
    tar.extract(member, dest)


def _find_dbpath(root: Path) -> Optional[Path]:
    for candidate in [root, *sorted(p for p in root.rglob("*") if p.is_dir())]:
        if (candidate / "WiredTiger").is_file():
            return candidate
    return None


def extract_archive(state: Dict[str, object]) -> None:
    if not state["archive"] or not state["workdir"]:
        print("Select the archive and the working folder first.")
        return
    archive = Path(state["archive"])
    name = archive.name
    for suffix in (".tar.gz", ".tgz"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    dest = Path(state["workdir"]) / f"teamsharing-restore-{name}"

    if state["extract_dir"] and Path(state["extract_dir"]).is_dir():
        if not questionary.confirm(f"Already extracted to {state['extract_dir']}. Delete and extract again?",
                                   default=False).ask():
            return
        _remove_extract_dir(state)
    if dest.exists():
        print(f"{dest} already exists and was not created by this tool - choose another working folder "
              f"or remove it manually.")
        return

    dest.mkdir(parents=True)
    (dest / EXTRACT_MARKER).write_text("created by teamsharing_restore.py\n", encoding="utf-8")
    state["extract_dir"] = str(dest)
    state["dbpath"] = None
    state["restored"] = False
    save_state(state)

    print(f"Extracting {archive} -> {dest}")
    count = 0
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                _safe_extract_member(tar, member, dest)
                count += 1
                if count % 200 == 0:
                    print(f"  {count} entries...")
    except (tarfile.TarError, OSError, ValueError) as e:
        print(f"Extraction failed: {e}")
        return

    dbpath = _find_dbpath(dest)
    if not dbpath:
        print(f"Extracted {count} entries, but no folder containing a 'WiredTiger' file was found. "
              f"Is this a MongoDB snapshot archive?")
        return
    state["dbpath"] = str(dbpath)
    save_state(state)
    print(f"Extracted {count} entries. MongoDB data path: {dbpath}")


def _pull_image(state: Dict[str, object], image: str) -> bool:
    if image_exists(image):
        return True
    print(f"Pulling {image} ...")
    if docker("pull", image, check=False, capture=False).returncode != 0:
        print(f"Could not pull {image}.")
        return False
    if image not in state["images_pulled"]:
        state["images_pulled"].append(image)
        save_state(state)
    return True


def _remove_container_and_volume(state: Dict[str, object]) -> None:
    if state["container_id"]:
        docker("rm", "-f", "-v", state["container_id"], check=False)
        state["container_id"] = None
    if state["volume"]:
        docker("volume", "rm", "-f", state["volume"], check=False)
        state["volume"] = None
    state["restored"] = False
    save_state(state)


def create_container(state: Dict[str, object]) -> None:
    if state["container_id"]:
        if not questionary.confirm("A container already exists. Remove it (and its data volume) and create "
                                   "a new one?", default=False).ask():
            return
        _remove_container_and_volume(state)

    if container_info(CONTAINER_NAME):
        print(f"A container named '{CONTAINER_NAME}' exists but is not tracked. Run cleanup first.")
        return

    version = questionary.select(
        "MongoDB version (must match the backup's featureCompatibilityVersion):",
        choices=MONGO_VERSIONS + ["Other..."], default=state["mongo_version"] or DEFAULT_MONGO_VERSION).ask()
    if not version:
        return
    if version == "Other...":
        version = (questionary.text("Image tag (e.g. 6.0.19):").ask() or "").strip()
        if not version:
            return

    port_answer = questionary.text("Host port (bound to 127.0.0.1 only):", default=str(state["port"] or DEFAULT_PORT),
                                   validate=lambda v: v.isdigit() and 1024 <= int(v) <= 65535 or
                                   "Enter a port between 1024 and 65535").ask()
    if not port_answer:
        return
    port = int(port_answer)
    if not port_free(port):
        print(f"Port {port} is already in use on 127.0.0.1.")
        return

    image = f"mongo:{version}"
    if not _pull_image(state, image):
        return

    try:
        docker("volume", "create", "--label", LABEL, VOLUME_NAME)
        state["volume"] = VOLUME_NAME
        save_state(state)
        r = docker("create", "--name", CONTAINER_NAME, "--label", LABEL,
                   "-p", f"127.0.0.1:{port}:27017",
                   "-v", f"{VOLUME_NAME}:{DATA_DIR_IN_CONTAINER}",
                   image)
        state["container_id"] = r.stdout.strip()
        state["image"] = image
        state["mongo_version"] = version
        state["port"] = port
        state["restored"] = False
        save_state(state)
        docker("start", state["container_id"])
    except DockerError as e:
        print(e)
        return

    print(f"Container {state['container_id'][:12]} created from {image}.")
    if _wait_ready(state["container_id"]):
        print("Container is up and running (empty database). Next: restore the data.")


def _print_fcv_hint(logs: str) -> None:
    keywords = ("featureCompatibilityVersion", "UPGRADE PROBLEM", "Wrong mongod version")
    hits = [line for line in logs.splitlines() if any(k in line for k in keywords)]
    if hits:
        print("\nThe logs mention a featureCompatibilityVersion mismatch:")
        for line in hits[-3:]:
            print(f"  {line[:300]}")
        print("Recreate the container with a matching MongoDB version, then restore again.")


def _wait_ready(container_id: str) -> bool:
    print("Waiting for mongod to accept connections", end="", flush=True)
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not container_running(container_id):
            print(" - container stopped.")
            logs = docker("logs", "--tail", "40", container_id, check=False)
            output = (logs.stdout or "") + (logs.stderr or "")
            print(output[-4000:])
            _print_fcv_hint(output)
            return False
        r = mongosh(container_id, "db.adminCommand({ping: 1}).ok")
        if r.returncode == 0 and r.stdout.strip() == "1":
            print(" - ready.")
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    print(f" - not ready after {READY_TIMEOUT_SECONDS}s. Check `docker logs {CONTAINER_NAME}`.")
    return False


def verify_container(state: Dict[str, object]) -> bool:
    if not state["container_id"]:
        print("No container yet - create it first.")
        return False
    info = container_info(state["container_id"])
    if not info:
        print("Tracked container no longer exists.")
        reconcile(state, True)
        return False
    if not info["State"]["Running"]:
        print(f"Container is {info['State']['Status']}.")
        if not questionary.confirm("Start it now?", default=True).ask():
            return False
        try:
            docker("start", state["container_id"])
        except DockerError as e:
            print(e)
            return False
    return _wait_ready(state["container_id"])


def restore_data(state: Dict[str, object]) -> None:
    if not state["dbpath"] or not Path(state["dbpath"]).is_dir():
        print("Extract the archive first.")
        return
    if not state["container_id"] or not state["volume"]:
        print("Create the container first.")
        return
    if state["restored"] and not questionary.confirm(
            "Data was already restored. Wipe the container's data and restore again?", default=False).ask():
        return

    cid = state["container_id"]
    try:
        print("Stopping container ...")
        docker("stop", cid)
        print("Clearing container data volume ...")
        docker("run", "--rm", "--label", LABEL, "-v", f"{state['volume']}:{DATA_DIR_IN_CONTAINER}",
               "--entrypoint", "sh", state["image"], "-c", f"find {DATA_DIR_IN_CONTAINER} -mindepth 1 -delete")
        print(f"Copying {state['dbpath']} into the container (this can take a few minutes) ...")
        docker("cp", str(Path(state["dbpath"])) + os.sep + ".", f"{cid}:{DATA_DIR_IN_CONTAINER}")
        print("Starting container ...")
        docker("start", cid)
    except DockerError as e:
        print(e)
        return

    if not _wait_ready(cid):
        return
    state["restored"] = True
    save_state(state)
    print("Data restored.")
    verify_databases(state)


def verify_databases(state: Dict[str, object]) -> None:
    if not state["container_id"] or not container_running(state["container_id"]):
        print("Container is not running - use 'Verify container' first.")
        return
    script = (
        "const out = {fcv: null, dbs: []};"
        "try { out.fcv = db.adminCommand({getParameter: 1, featureCompatibilityVersion: 1})"
        ".featureCompatibilityVersion.version; } catch (e) { out.fcv = 'unknown'; }"
        "for (const d of db.adminCommand({listDatabases: 1}).databases) {"
        "  const s = db.getSiblingDB(d.name); const cols = {};"
        "  for (const c of s.getCollectionNames()) { cols[c] = s.getCollection(c).estimatedDocumentCount(); }"
        "  out.dbs.push({name: d.name, size: Number(d.sizeOnDisk), collections: cols});"
        "}"
        "print(JSON.stringify(out));"
    )
    r = mongosh(state["container_id"], script)
    if r.returncode != 0:
        print(f"Query failed: {(r.stderr or r.stdout).strip()}")
        return
    try:
        result = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"Unexpected output: {r.stdout.strip()[:500]}")
        return

    print(f"\nfeatureCompatibilityVersion: {result['fcv']}")
    print(f"{'Database':<32} {'Size (MB)':>10} {'Collections':>12}")
    for d in result["dbs"]:
        print(f"{d['name']:<32} {d['size'] / 1024 / 1024:>10.1f} {len(d['collections']):>12}")

    dbs = {d["name"]: d["collections"] for d in result["dbs"]}
    all_ok = True
    print("\nRunbook collections:")
    for db_name, collections in EXPECTED_COLLECTIONS.items():
        for col in collections:
            if db_name in dbs and col in dbs[db_name]:
                print(f"  OK       {db_name}.{col}  ({dbs[db_name][col]} documents)")
            else:
                print(f"  MISSING  {db_name}.{col}")
                all_ok = False
    print("\nAll expected collections present." if all_ok else "\nSome expected collections are missing.")
    print(f"Connect Studio 3T to mongodb://127.0.0.1:{state['port']}/?directConnection=true")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _force_remove(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_extract_dir(state: Dict[str, object]) -> None:
    extract_dir = Path(state["extract_dir"]) if state["extract_dir"] else None
    if extract_dir and extract_dir.is_dir():
        if (extract_dir / EXTRACT_MARKER).is_file():
            shutil.rmtree(extract_dir, onerror=_force_remove)
            print(f"Removed {extract_dir}")
        else:
            print(f"Not removing {extract_dir}: marker file missing, it was not created by this tool.")
            return
    state["extract_dir"] = state["dbpath"] = None
    state["restored"] = False
    save_state(state)


def cleanup(state: Dict[str, object], docker_ok: bool) -> None:
    containers = set(labelled_containers()) if docker_ok else set()
    if state["container_id"]:
        containers.add(state["container_id"])
    volumes = set(labelled_volumes()) if docker_ok else set()
    if state["volume"]:
        volumes.add(state["volume"])
    images = [i for i in state["images_pulled"] if docker_ok and image_exists(i)]

    print("\nCleanup will permanently remove:")
    for c in containers:
        print(f"  container  {c[:12]}")
    for v in volumes:
        print(f"  volume     {v}")
    for i in images:
        print(f"  image      {i}  (pulled by this tool)")
    if state["extract_dir"]:
        print(f"  folder     {state['extract_dir']}")
    print(f"  state      {STATE_FILE}")
    if not docker_ok and (state["container_id"] or state["volume"]):
        print("! Docker is not reachable - Docker objects cannot be removed now. Start Docker and run cleanup again.")
        return
    if not questionary.confirm("Proceed? This cannot be undone.", default=False).ask():
        return

    for c in containers:
        r = docker("rm", "-f", "-v", c, check=False)
        print(f"Removed container {c[:12]}" if r.returncode == 0 else f"! Could not remove container {c[:12]}")
    state["container_id"] = None
    for v in volumes:
        r = docker("volume", "rm", "-f", v, check=False)
        print(f"Removed volume {v}" if r.returncode == 0 else f"! Could not remove volume {v}: {r.stderr.strip()}")
    state["volume"] = None
    for i in images:
        r = docker("rmi", i, check=False)
        print(f"Removed image {i}" if r.returncode == 0 else f"! Could not remove image {i} (in use?)")
    state["images_pulled"] = [i for i in images if image_exists(i)]
    save_state(state)

    _remove_extract_dir(state)

    if state["archive"] and Path(state["archive"]).is_file() and questionary.confirm(
            f"Also delete the backup archive {state['archive']}?", default=False).ask():
        Path(state["archive"]).unlink()
        print(f"Removed {state['archive']}")

    leftovers = state["images_pulled"] or state["extract_dir"]
    if leftovers:
        print("! Some items could not be removed - state file kept so cleanup can be retried.")
        return
    STATE_FILE.unlink(missing_ok=True)
    try:
        STATE_FILE.parent.rmdir()
    except OSError:
        pass
    state.clear()
    state.update(_empty_state())
    print("Cleanup complete - nothing left behind.")


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def run_menu() -> None:
    state = load_state()
    docker_error = docker_available()
    if docker_error:
        print(f"! {docker_error}")
    reconcile(state, docker_error is None)

    while True:
        docker_ok = docker_error is None
        print_status(state, docker_ok)
        choices = [
            questionary.Choice("1. Select backup archive (.tar.gz)", "archive"),
            questionary.Choice("2. Select working folder", "workdir"),
            questionary.Choice("3. Extract archive", "extract"),
            questionary.Choice("4. Create MongoDB container", "create", disabled=None if docker_ok else "Docker unavailable"),
            questionary.Choice("5. Verify container is running", "verify", disabled=None if docker_ok else "Docker unavailable"),
            questionary.Choice("6. Restore data into container", "restore", disabled=None if docker_ok else "Docker unavailable"),
            questionary.Choice("7. Verify databases", "verify_db", disabled=None if docker_ok else "Docker unavailable"),
            questionary.Choice("8. Clean up everything", "cleanup"),
            questionary.Choice("Re-check Docker", "recheck"),
            questionary.Choice("Exit", "exit"),
        ]
        action = questionary.select("What do you want to do?", choices=choices).ask()
        if action in (None, "exit"):
            return
        try:
            if action == "archive":
                select_archive(state)
            elif action == "workdir":
                select_workdir(state)
            elif action == "extract":
                extract_archive(state)
            elif action == "create":
                create_container(state)
            elif action == "verify":
                verify_container(state)
            elif action == "restore":
                restore_data(state)
            elif action == "verify_db":
                verify_databases(state)
            elif action == "cleanup":
                cleanup(state, docker_ok)
            elif action == "recheck":
                docker_error = docker_available()
                print(f"! {docker_error}" if docker_error else "Docker is available.")
                reconcile(state, docker_error is None)
        except DockerError as e:
            print(f"! {e}")


def main() -> int:
    try:
        run_menu()
    except KeyboardInterrupt:
        print("\nInterrupted - state is saved, run the tool again to continue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
