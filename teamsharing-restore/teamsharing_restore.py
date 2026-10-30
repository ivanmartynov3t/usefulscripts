#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["questionary>=2.0"]
# ///
"""
Team Sharing backup restore helper.

Guided wizard for the Ops runbook "How to recover a user's Team Sharing data
in case it was erroneously deleted". MongoDB runs inside Docker instead of a
local mongod binary:

   1. Select a MongoDB Atlas snapshot archive (.tar.gz / .tgz)
   2. Select a working folder
   3. Extract the archive into it
   4. Create and start a MongoDB container (version must match the backup's
      featureCompatibilityVersion - 6.0 at the time the runbook was written)
   5. Verify the container is up and mongod answers
   6. Restore the extracted data files into the container's Docker volume
   7. Verify the restored databases (TEAM_SHARING_ENCRYPTED_PROD, ENCRYPTION_PROD)
   8. Identify the old (unlicensed) and new user ID by the user's email
   9. Find the deleted documents: folders, resources, resource contents,
      permissions and folder keys in ENCRYPTION_PROD.__keyVault
  10. Replace the old user ID with the new one (local copy only) and export
      the documents as Extended JSON in production insert order
  Inserting into production stays manual. "Clean up everything" removes the
  container, volume, pulled image, extracted files, export and state; the
  backup archive itself is never deleted.

Before every command (docker, mongosh script, file/folder write or delete)
the tool prints what it is for and the exact command, and asks to run it,
run the rest of the step without asking, or cancel. Only internal read-only
status checks (docker info/inspect/ps, the mongod ping) run without asking.

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
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

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

TEAM_SHARING_DB = "TEAM_SHARING_ENCRYPTED_PROD"
EXPORT_DIR_NAME = "teamsharing-recovery-export"
# Order in which the runbook inserts the recovered documents into production
EXPORT_ORDER = [
    "ENCRYPTION_PROD.__keyVault",
    f"{TEAM_SHARING_DB}.folder",
    f"{TEAM_SHARING_DB}.permission",
    f"{TEAM_SHARING_DB}.resource",
    f"{TEAM_SHARING_DB}.resourceContent",
]


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
        "db_verified": False,      # last database check found all runbook collections
        "email": None,             # affected user's email
        "old_user_id": None,       # unlicensed ID whose data was deleted (e.g. google-oauth2|...)
        "new_user_id": None,       # ID the data must be re-assigned to
        "found_counts": None,      # documents found per collection for old_user_id
        "export_dir": None,        # folder this tool created for the fixed documents
        "exported_for": None,      # [old_user_id, new_user_id] the export was made for
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


class StepCancelled(Exception):
    pass


# Set when the user picks "run all remaining commands of this step"; reset before every step
_approve_all = False


def reset_approvals() -> None:
    global _approve_all
    _approve_all = False


def approve(what: str, details: str) -> None:
    """Show exactly what is about to happen and ask before doing it. Raises StepCancelled on 'no'."""
    global _approve_all
    print(f"\n--- Next action: {what}")
    for line in details.splitlines():
        print(f"    {line}")
    if _approve_all:
        print("    (approved: running all commands of this step)")
        return
    answer = questionary.select("Run it?", choices=[
        questionary.Choice("Yes, run it", "yes"),
        questionary.Choice("Yes, and run the remaining commands of this step without asking", "all"),
        questionary.Choice("No, cancel this step", "no"),
    ], default="yes").ask()
    if answer == "all":
        _approve_all = True
    elif answer != "yes":
        raise StepCancelled(what)


def _format_script(script: str) -> str:
    """Indent a mongosh script for display."""
    return "\n".join("  " + line for line in script.strip().splitlines())


def docker(*args: str, check: bool = True, capture: bool = True, describe: Optional[str] = None,
           shown: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a docker command. With `describe`, the command is shown and confirmed first
    (internal read-only status checks pass no description and run silently)."""
    cmd = ["docker", *args]
    if describe:
        approve(describe, shown or subprocess.list2cmdline(cmd))
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


def mongosh(container_id: str, script: str, describe: Optional[str] = None) -> subprocess.CompletedProcess:
    shown = (f"docker exec {container_id[:12]} mongosh --quiet --norc --eval <script>\n"
             f"script:\n{_format_script(script)}")
    return docker("exec", container_id, "mongosh", "--quiet", "--norc", "--eval", script, check=False,
                  describe=describe, shown=shown)


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
    if state["export_dir"] and not Path(state["export_dir"]).is_dir():
        print(f"- Export folder {state['export_dir']} no longer exists - reference removed.")
        state["export_dir"] = state["exported_for"] = None
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
    show("Old user ID", state["old_user_id"])
    show("New user ID", state["new_user_id"])
    if state["found_counts"]:
        show("Found", ", ".join(f"{k.split('.')[-1]}: {v}" for k, v in state["found_counts"].items()))
    show("Export", state["export_dir"])
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

    approve("Extract the backup archive",
            f"create folder  {dest}\n"
            f"extract        {archive}\n"
            f"into           {dest}\n"
            "(Python tarfile, safe mode: no paths outside the folder, no links or device files)")
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
    if docker("pull", image, check=False, capture=False,
              describe=f"Download the official MongoDB image {image} from Docker Hub").returncode != 0:
        print(f"Could not pull {image}.")
        return False
    if image not in state["images_pulled"]:
        state["images_pulled"].append(image)
        save_state(state)
    return True


def _remove_container_and_volume(state: Dict[str, object]) -> None:
    if state["container_id"]:
        docker("rm", "-f", "-v", state["container_id"], check=False,
               describe="Remove the existing container (force-stops it if running)")
        state["container_id"] = None
    if state["volume"]:
        docker("volume", "rm", "-f", state["volume"], check=False,
               describe="Delete the container's data volume and the restored data in it")
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

    if _create_and_start(state, version, port):
        print("Container is up and running (empty database). Next: restore the data.")


def _create_and_start(state: Dict[str, object], version: str, port: int) -> bool:
    """Pull the image if needed, create volume + container and start it. Returns True when mongod is ready."""
    image = f"mongo:{version}"
    if not _pull_image(state, image):
        return False

    try:
        docker("volume", "create", "--label", LABEL, VOLUME_NAME,
               describe=f"Create Docker volume '{VOLUME_NAME}' that will hold the MongoDB data files")
        state["volume"] = VOLUME_NAME
        save_state(state)
        r = docker("create", "--name", CONTAINER_NAME, "--label", LABEL,
                   "-p", f"127.0.0.1:{port}:27017",
                   "-v", f"{VOLUME_NAME}:{DATA_DIR_IN_CONTAINER}",
                   image,
                   describe=f"Create container '{CONTAINER_NAME}' from {image}: MongoDB port 27017 published "
                            f"on 127.0.0.1:{port} only, data volume mounted at {DATA_DIR_IN_CONTAINER}")
        state["container_id"] = r.stdout.strip()
        state["image"] = image
        state["mongo_version"] = version
        state["port"] = port
        state["restored"] = False
        save_state(state)
        docker("start", state["container_id"], describe="Start the container (mongod starts on an empty database)")
    except DockerError as e:
        print(e)
        return False

    print(f"Container {state['container_id'][:12]} created from {image}.")
    return _wait_ready(state["container_id"])


# mongod refuses data files written by a newer version and exits with code 62
# ("Wrong mongod version ... featureCompatibilityVersion document ... version: \"8.0\"")
EXIT_NEED_NEWER_MONGOD = 62
_FCV_IN_LOG = re.compile(r'featureCompatibilityVersion\\?",\s*version:\s*\\?"(\d+\.\d+)')


def _required_mongo_version(container_id: str) -> Optional[str]:
    """MongoDB version the data files need, read from the stopped container's full log."""
    logs = docker("logs", container_id, check=False)
    match = _FCV_IN_LOG.search((logs.stdout or "") + (logs.stderr or ""))
    return match.group(1) if match else None


def _wait_ready(container_id: str) -> bool:
    print("Waiting for mongod to accept connections", end="", flush=True)
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        info = container_info(container_id)
        if not info or not info["State"]["Running"]:
            exit_code = info["State"]["ExitCode"] if info else None
            required = _required_mongo_version(container_id)
            if required:
                print(f" - mongod stopped (exit code {exit_code}): the data files were written by MongoDB "
                      f"{required} (featureCompatibilityVersion {required}), newer than this container's mongod.")
            else:
                print(f" - container stopped (exit code {exit_code}). Last log lines:")
                logs = docker("logs", "--tail", "40", container_id, check=False)
                print(((logs.stdout or "") + (logs.stderr or ""))[-4000:])
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
            docker("start", state["container_id"], describe="Start the stopped container")
        except DockerError as e:
            print(e)
            return False
    if _wait_ready(state["container_id"]):
        return True
    _offer_version_fix(state)
    return False


def _offer_version_fix(state: Dict[str, object]) -> None:
    """After mongod refused the data files, offer to recreate the container with the version they need."""
    if not state["container_id"]:
        return
    required = _required_mongo_version(state["container_id"])
    if not required or required == state["mongo_version"]:
        return
    print(f"\nThe backup needs MongoDB {required}; the container runs {state['image']}.")
    print(f"Fix: replace the container and its data volume with a mongo:{required} one (same port "
          f"{state['port']}) and restore the extracted backup into it again. The extracted files stay as they are.")
    if not questionary.confirm(f"Recreate the container with MongoDB {required} and restore again?",
                               default=True).ask():
        return
    port = state["port"]
    _remove_container_and_volume(state)
    if _create_and_start(state, required, port):
        restore_data(state)


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
        docker("stop", cid, describe="Stop the container so mongod releases the data files")
        docker("run", "--rm", "--label", LABEL, "-v", f"{state['volume']}:{DATA_DIR_IN_CONTAINER}",
               "--entrypoint", "sh", state["image"], "-c", f"find {DATA_DIR_IN_CONTAINER} -mindepth 1 -delete",
               describe=f"Empty the data volume '{state['volume']}' using a temporary helper container "
                        f"(removed automatically when done)")
        state["restored"] = state["db_verified"] = False
        state["found_counts"] = None
        save_state(state)
        docker("cp", str(Path(state["dbpath"])) + os.sep + ".", f"{cid}:{DATA_DIR_IN_CONTAINER}",
               describe=f"Copy the extracted backup files into the container's {DATA_DIR_IN_CONTAINER} "
                        f"(can take a few minutes)")
        docker("start", cid, describe="Start the container on the restored data")
    except DockerError as e:
        print(e)
        return

    if not _wait_ready(cid):
        _offer_version_fix(state)
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
        "const out = {fcv: null, dbs: []};\n"
        "try { out.fcv = db.adminCommand({getParameter: 1, featureCompatibilityVersion: 1})"
        ".featureCompatibilityVersion.version; } catch (e) { out.fcv = 'unknown'; }\n"
        "for (const d of db.adminCommand({listDatabases: 1}).databases) {\n"
        "  const s = db.getSiblingDB(d.name); const cols = {};\n"
        "  for (const c of s.getCollectionNames()) { cols[c] = s.getCollection(c).estimatedDocumentCount(); }\n"
        "  out.dbs.push({name: d.name, size: Number(d.sizeOnDisk), collections: cols});\n"
        "}\n"
        "print(JSON.stringify(out));\n"
    )
    result = _run_json(state, script, "Read-only query: list databases, featureCompatibilityVersion and "
                                      "document counts per collection")
    if result is None:
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
    state["db_verified"] = all_ok
    save_state(state)
    print(f"Connect Studio 3T to mongodb://127.0.0.1:{state['port']}/?directConnection=true")


# ---------------------------------------------------------------------------
# Recovery: identify user, find deleted documents, fix ownership, export
# (runbook sections "Find the affected user, folder and resources" and
# "Fix the ownership"; the production insert itself stays manual)
# ---------------------------------------------------------------------------

def _restored_db_ready(state: Dict[str, object]) -> bool:
    if not state["restored"]:
        print("Restore the backup data first.")
        return False
    if not state["container_id"] or not container_running(state["container_id"]):
        print("Container is not running - use 'Verify container is running' first.")
        return False
    return True


def _run_json(state: Dict[str, object], script: str, describe: str) -> Optional[object]:
    """Run a mongosh script that prints one JSON document on its last line and parse it."""
    r = mongosh(state["container_id"], script, describe=describe)
    if r.returncode != 0:
        print(f"Query failed: {(r.stderr or r.stdout).strip()[:1000]}")
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"Unexpected output: {r.stdout.strip()[:500]}")
        return None


def _choose_user_id(question: str, users: List[Dict[str, object]], exclude: Optional[str],
                    default: Optional[str]) -> Optional[str]:
    choices = [questionary.Choice(f"{u['userId']}  (license: {'assigned' if u['licensed'] else 'none'})",
                                  u["userId"]) for u in users if u["userId"] != exclude]
    choices.append(questionary.Choice("Enter manually...", "__manual__"))
    values = [c.value for c in choices]
    answer = questionary.select(question, choices=choices,
                                default=default if default in values else None).ask()
    if answer == "__manual__":
        answer = (questionary.text("User ID:").ask() or "").strip() or None
    return answer


def identify_user(state: Dict[str, object]) -> None:
    if not _restored_db_ready(state):
        return
    email = questionary.text("Affected user's email address:", default=state["email"] or "",
                             validate=lambda v: "@" in v.strip() or "Enter an email address").ask()
    if not email:
        return
    email = email.strip()
    users = _run_json(state, (
        f"const email = {json.dumps(email)};\n"
        f"const users = db.getSiblingDB({json.dumps(TEAM_SHARING_DB)}).getCollection('user')"
        ".find({email: email}).collation({locale: 'en', strength: 2}).toArray();\n"
        "print(JSON.stringify(users.map(u => ({userId: String(u.userId),\n"
        " licensed: u.license !== null && u.license !== undefined}))));\n"
    ), f"Read-only query: find all records in {TEAM_SHARING_DB}.user with this email (case-insensitive)")
    if users is None:
        return
    print(f"\nFound {len(users)} user record(s) for this email in {TEAM_SHARING_DB}.user:")
    for u in users:
        print(f"  {u['userId']}  (license: {'assigned' if u['licensed'] else 'none'})")
    if not users and not questionary.confirm("No user found. Enter the IDs manually?", default=False).ask():
        return

    unlicensed = next((u["userId"] for u in users if not u["licensed"]), None)
    old_id = _choose_user_id("Which ID lost its data (the one removed from the license)?", users, None,
                             state["old_user_id"] or unlicensed)
    if not old_id:
        return
    licensed = next((u["userId"] for u in users if u["licensed"] and u["userId"] != old_id), None)
    new_id = _choose_user_id("Which ID should own the recovered data?", users, old_id,
                             state["new_user_id"] or licensed)
    if not new_id:
        return
    if new_id == old_id:
        print("The old and new user ID must differ.")
        return

    if (old_id, new_id, email) != (state["old_user_id"], state["new_user_id"], state["email"]):
        state["found_counts"] = None
        state["exported_for"] = None
    state["email"], state["old_user_id"], state["new_user_id"] = email, old_id, new_id
    save_state(state)
    print(f"Recovering data of {old_id} -> {new_id}")


def _find_documents_js(state: Dict[str, object]) -> str:
    """mongosh code that collects every document the runbook recovers into `found`."""
    return (
        f"const TS = db.getSiblingDB({json.dumps(TEAM_SHARING_DB)});\n"
        f"const OLD = {json.dumps(state['old_user_id'])};\n"
        f"const NEW = {json.dumps(state['new_user_id'])};\n"
        f"const EMAIL = {json.dumps(state['email'])};\n"
        "const idStr = id => (id && id.toHexString) ? id.toHexString() : String(id);\n"
        "const folders = TS.getCollection('folder').find({'creator.userId': OLD}).toArray();\n"
        "const fids = folders.map(f => f._id);\n"
        "const resources = TS.getCollection('resource')"
        ".find({$or: [{folderId: {$in: fids}}, {'creator.userId': OLD}]}).toArray();\n"
        "const cids = resources.map(r => r.contentId).filter(c => c !== undefined && c !== null);\n"
        "const contents = TS.getCollection('resourceContent')"
        ".find({$or: [{folderId: {$in: fids}}, {_id: {$in: cids}}]}).toArray();\n"
        "const permissions = TS.getCollection('permission')"
        ".find({$or: [{folderId: {$in: fids}}, {'creator.userId': OLD}, {'addressee.email': EMAIL}]})"
        ".collation({locale: 'en', strength: 2}).toArray();\n"
        "const keyNames = fids.map(id => 'data-key-1-folder-key-' + idStr(id));\n"
        "const keys = db.getSiblingDB('ENCRYPTION_PROD').getCollection('__keyVault')"
        ".find({keyAltNames: {$in: keyNames}}).toArray();\n"
        "const found = {\n"
        "  'ENCRYPTION_PROD.__keyVault': keys,\n"
        f"  {json.dumps(TEAM_SHARING_DB + '.folder')}: folders,\n"
        f"  {json.dumps(TEAM_SHARING_DB + '.permission')}: permissions,\n"
        f"  {json.dumps(TEAM_SHARING_DB + '.resource')}: resources,\n"
        f"  {json.dumps(TEAM_SHARING_DB + '.resourceContent')}: contents,\n"
        "};\n"
    )


def find_documents(state: Dict[str, object]) -> None:
    if not _restored_db_ready(state):
        return
    if not state["old_user_id"] or not state["new_user_id"]:
        print("Identify the user IDs first.")
        return
    result = _run_json(state, _find_documents_js(state) + (
        "print(JSON.stringify({\n"
        "  counts: Object.fromEntries(Object.entries(found).map(([k, v]) => [k, v.length])),\n"
        "  folders: folders.map(f => ({id: idStr(f._id), name: String(f.name), deleted: f.deleted})),\n"
        "  missingKeys: keyNames.filter(n => !keys.some(k => (k.keyAltNames || []).includes(n))),\n"
        "}));\n"
    ), "Read-only query: find the old user's folders, their resources, resource contents, permissions "
       "and folder encryption keys")
    if result is None:
        return

    print(f"\nFolders created by {state['old_user_id']}:")
    for f in result["folders"]:
        print(f"  {f['id']}  {f['name']}" + ("  (marked deleted)" if f.get("deleted") else ""))
    if not result["folders"]:
        print("  none")
    print("\nDocuments to recover (production insert order):")
    for i, name in enumerate(EXPORT_ORDER, 1):
        print(f"  {i}. {name:<46} {result['counts'][name]:>6}")
    for key_name in result["missingKeys"]:
        print(f"! No __keyVault entry found for {key_name} - that folder's content cannot be decrypted.")
    if not any(result["counts"].values()):
        print("Nothing found for this user ID - check that the right ID was selected as the old one.")
        return
    state["found_counts"] = result["counts"]
    save_state(state)


def _prepare_export_dir(state: Dict[str, object]) -> Optional[Path]:
    export_dir = Path(state["workdir"]) / EXPORT_DIR_NAME
    if export_dir.exists():
        if not (export_dir / EXTRACT_MARKER).is_file():
            print(f"{export_dir} exists and was not created by this tool - move it away first.")
            return None
        approve("Delete the previous export", f"delete folder  {export_dir}  (and all files in it)")
        shutil.rmtree(export_dir, onerror=_force_remove)
    approve("Create the export folder", f"create folder  {export_dir}")
    export_dir.mkdir(parents=True)
    (export_dir / EXTRACT_MARKER).write_text("created by teamsharing_restore.py\n", encoding="utf-8")
    state["export_dir"] = str(export_dir)
    state["exported_for"] = None
    save_state(state)
    return export_dir


def fix_and_export(state: Dict[str, object]) -> None:
    if not _restored_db_ready(state):
        return
    if not state["found_counts"]:
        print("Find the affected documents first.")
        return
    print(f"Every value equal to {state['old_user_id']} is replaced with {state['new_user_id']}, both in the")
    print("local restored copy (so you can copy the documents from Studio 3T) and in the exported files.")
    print("Nothing is written to production.")
    export_dir = _prepare_export_dir(state)
    if not export_dir:
        return

    result = _run_json(state, _find_documents_js(state) + (
        "const isPlain = v => v !== null && typeof v === 'object' && !v._bsontype\n"
        "  && Object.prototype.toString.call(v) === '[object Object]';\n"
        "const fix = v => {\n"
        "  if (v === OLD) return NEW;\n"
        "  if (Array.isArray(v)) return v.map(fix);\n"
        "  if (isPlain(v)) { for (const k of Object.keys(v)) v[k] = fix(v[k]); }\n"
        "  return v;\n"
        "};\n"
        "const leftovers = {};\n"
        "for (const [name, docs] of Object.entries(found)) {\n"
        "  const [dbName, ...rest] = name.split('.');\n"
        "  const coll = db.getSiblingDB(dbName).getCollection(rest.join('.'));\n"
        "  found[name] = docs.map(d => fix(d));\n"
        "  for (const d of found[name]) coll.replaceOne({_id: d._id}, d);\n"
        "  leftovers[name] = String(found[name].filter(d => EJSON.stringify(d).includes(OLD)).length);\n"
        "}\n"
        "print(EJSON.stringify({docs: found, leftovers: leftovers}, {relaxed: false}));\n"
    ), f"WRITE to the LOCAL restored copy only: replace {state['old_user_id']} with {state['new_user_id']} "
       f"in the found documents (replaceOne per document) and return them for export")
    if result is None:
        return

    approve("Write the export files", "\n".join(
        f"write  {export_dir / f'{i}-{name}.json'}  ({len(result['docs'][name])} documents)"
        for i, name in enumerate(EXPORT_ORDER, 1)))
    print(f"\nExported to {export_dir} (MongoDB Extended JSON, one array per file):")
    total_left = 0
    for i, name in enumerate(EXPORT_ORDER, 1):
        docs = result["docs"][name]
        path = export_dir / f"{i}-{name}.json"
        path.write_text(json.dumps(docs, indent=2), encoding="utf-8")
        left = int(result["leftovers"][name])
        total_left += left
        note = f"  ! {left} document(s) still mention the old ID" if left else ""
        print(f"  {path.name:<54} {len(docs):>6} document(s){note}")
    if total_left:
        print("! The old ID still appears inside some values (not as a whole value). Review those documents.")
    else:
        print("No references to the old user ID remain.")
    state["exported_for"] = [state["old_user_id"], state["new_user_id"]]
    save_state(state)


def print_production_instructions(state: Dict[str, object]) -> None:
    print("\nLast step - restore on production (manual, per the runbook):")
    print("  1. Get access to the Team Sharing production database from the database owner.")
    print("  2. In Studio 3T, insert the documents in this order - either copy them from the local")
    print(f"     copy (mongodb://127.0.0.1:{state['port']}/?directConnection=true) or import the files in")
    print(f"     {state['export_dir']} (Import > JSON):")
    for i, name in enumerate(EXPORT_ORDER, 1):
        print(f"       {i}. {name}")
    print("  3. Double-check the target database for every collection before inserting.")
    print("  4. When done, run 'Clean up everything' - it also deletes the export folder.")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _force_remove(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_owned_dir(path_str: Optional[str], what: str) -> bool:
    """Delete a folder this tool created (it holds the marker file). Returns True if it is gone."""
    path = Path(path_str) if path_str else None
    if not path or not path.is_dir():
        return True
    if not (path / EXTRACT_MARKER).is_file():
        print(f"Not removing {path}: marker file missing, it was not created by this tool.")
        return False
    approve(f"Delete the {what}", f"delete folder  {path}  (and all files in it)")
    shutil.rmtree(path, onerror=_force_remove)
    print(f"Removed {path}")
    return True


def _remove_extract_dir(state: Dict[str, object]) -> None:
    if not _remove_owned_dir(state["extract_dir"], "extracted backup files"):
        return
    state["extract_dir"] = state["dbpath"] = None
    state["restored"] = False
    save_state(state)


def _remove_export_dir(state: Dict[str, object]) -> None:
    if not _remove_owned_dir(state["export_dir"], "exported recovery files"):
        return
    state["export_dir"] = state["exported_for"] = None
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
    if state["export_dir"]:
        print(f"  folder     {state['export_dir']}")
    print(f"  state      {STATE_FILE}")
    if not docker_ok and (state["container_id"] or state["volume"]):
        print("! Docker is not reachable - Docker objects cannot be removed now. Start Docker and run cleanup again.")
        return
    if not questionary.confirm("Proceed? This cannot be undone.", default=False).ask():
        return

    for c in containers:
        r = docker("rm", "-f", "-v", c, check=False, describe="Remove the container (force-stops it if running)")
        print(f"Removed container {c[:12]}" if r.returncode == 0 else f"! Could not remove container {c[:12]}")
        if c == state["container_id"] and r.returncode == 0:
            state["container_id"] = None
            save_state(state)
    for v in volumes:
        r = docker("volume", "rm", "-f", v, check=False,
                   describe="Delete the data volume and the restored database files in it")
        print(f"Removed volume {v}" if r.returncode == 0 else f"! Could not remove volume {v}: {r.stderr.strip()}")
        if v == state["volume"] and r.returncode == 0:
            state["volume"] = None
            save_state(state)
    for i in images:
        r = docker("rmi", i, check=False, describe=f"Delete the Docker image {i} (downloaded by this tool)")
        print(f"Removed image {i}" if r.returncode == 0 else f"! Could not remove image {i} (in use?)")
    state["images_pulled"] = [i for i in images if image_exists(i)]
    save_state(state)

    _remove_extract_dir(state)
    _remove_export_dir(state)

    leftovers = (state["images_pulled"] or state["extract_dir"] or state["export_dir"]
                 or state["container_id"] or state["volume"])
    if leftovers:
        print("! Some items could not be removed - state file kept so cleanup can be retried.")
        return
    approve("Delete the tool's state file", f"delete file  {STATE_FILE}")
    STATE_FILE.unlink(missing_ok=True)
    try:
        STATE_FILE.parent.rmdir()
    except OSError:
        pass
    state.clear()
    state.update(_empty_state())
    print("Cleanup complete - nothing left behind.")


# ---------------------------------------------------------------------------
# Wizard + menu
# ---------------------------------------------------------------------------

class Step(NamedTuple):
    key: str
    title: str
    needs_docker: bool
    run: Callable[[Dict[str, object]], object]
    is_done: Callable[[Dict[str, object]], bool]
    explain: str


STEPS = [
    Step("archive", "Select backup archive (.tar.gz)", False, select_archive,
         lambda s: bool(s["archive"]),
         "Pick the daily Atlas snapshot (.tar.gz, ~300 MB) you received for the recovery. Nothing is changed."),
    Step("workdir", "Select working folder", False, select_workdir,
         lambda s: bool(s["workdir"]),
         "Pick a folder with ~2 GB free space. The archive is extracted into a new subfolder there,\n"
         "and the recovered documents are exported there later. Nothing is changed yet."),
    Step("extract", "Extract archive", False, extract_archive,
         lambda s: bool(s["dbpath"]),
         "Unpacks the snapshot (~1 GB) into a new subfolder and finds the MongoDB data files."),
    Step("create", "Create MongoDB container", True, create_container,
         lambda s: bool(s["container_id"]),
         "Downloads the MongoDB image if needed, creates a data volume and a container that is\n"
         "reachable from this machine only. The version should match the backup (the runbook used 6.0;\n"
         "newer backups need 7.0 or 8.0). If it doesn't match, the restore step detects the required\n"
         "version and offers to recreate the container with it."),
    Step("verify", "Verify container is running", True, verify_container,
         lambda s: bool(s["container_id"]) and container_running(s["container_id"]),
         "Checks that the container runs and mongod answers; offers to start it if it is stopped."),
    Step("restore", "Restore data into container", True, restore_data,
         lambda s: bool(s["restored"]),
         "Stops the container, empties its data volume, copies the extracted backup files in and\n"
         "starts it again, then checks the databases."),
    Step("verify_db", "Verify databases", True, verify_databases,
         lambda s: bool(s["restored"] and s["db_verified"]),
         "Read-only check that all collections the runbook needs exist, with document counts."),
    Step("identify", "Identify the affected user IDs", True, identify_user,
         lambda s: bool(s["old_user_id"] and s["new_user_id"]),
         "Looks up the user's email in the user collection. You choose the old ID that lost its data\n"
         "(usually google-oauth2|..., no license) and the ID that should own it now (licensed)."),
    Step("find", "Find the deleted documents", True, find_documents,
         lambda s: bool(s["old_user_id"] and s["found_counts"]),
         "Read-only queries from the runbook: folders created by the old ID, their resources and\n"
         "resource contents, related permissions, and each folder's encryption key in __keyVault."),
    Step("export", "Fix ownership and export", True, fix_and_export,
         lambda s: bool(s["export_dir"] and s["exported_for"] == [s["old_user_id"], s["new_user_id"]]),
         "Replaces the old user ID with the new one in the found documents (local copy only) and\n"
         "writes them as JSON files, numbered in the order they must be inserted into production."),
]


def next_step(state: Dict[str, object]) -> Optional[int]:
    """Index of the first step that isn't done yet, or None when the workflow is complete."""
    for i, step in enumerate(STEPS):
        if not step.is_done(state):
            return i
    return None


def _print_progress(state: Dict[str, object], current: Optional[int]) -> None:
    print("\nWorkflow")
    for i, step in enumerate(STEPS):
        mark = "[x]" if step.is_done(state) else ("[>]" if i == current else "[ ]")
        print(f"  {mark} {i + 1}. {step.title}")


def run_step(step: Step, state: Dict[str, object]) -> bool:
    """Run one step; every command in it asks for approval. Returns False if the user cancelled."""
    reset_approvals()
    try:
        step.run(state)
    except StepCancelled as e:
        print(f"\nCancelled at: {e}. Nothing further was run; the step can be repeated later.")
        return False
    return True


def run_wizard(state: Dict[str, object], docker_ok: bool) -> None:
    """Walk the user through the remaining steps in order until done or they stop."""
    while True:
        index = next_step(state)
        if index is None:
            print("\nAll automated steps are done.")
            print_production_instructions(state)
            return
        step = STEPS[index]
        _print_progress(state, index)
        if step.needs_docker and not docker_ok:
            print(f"\nStep {index + 1} needs Docker, which is not reachable. Start Docker Desktop, "
                  f"then choose 'Re-check Docker' in the menu.")
            return
        print(f"\n=== Step {index + 1} of {len(STEPS)}: {step.title} ===")
        print(step.explain)
        if not run_step(step, state):
            return
        if step.is_done(state):
            following = next_step(state)
            if following is None:
                continue
            if not questionary.confirm(f"Continue with step {following + 1}: {STEPS[following].title}?",
                                       default=True).ask():
                return
        elif not questionary.confirm(f"Step {index + 1} is not complete. Try it again?", default=True).ask():
            return


def run_menu() -> None:
    state = load_state()
    docker_error = docker_available()
    if docker_error:
        print(f"! {docker_error}")
    reconcile(state, docker_error is None)

    index = next_step(state)
    if index is not None:
        verb = "Start" if index == 0 else "Resume"
        if questionary.confirm(f"{verb} the guided workflow at step {index + 1}: {STEPS[index].title}?",
                               default=True).ask():
            try:
                run_wizard(state, docker_error is None)
            except DockerError as e:
                print(f"! {e}")

    while True:
        docker_ok = docker_error is None
        print_status(state, docker_ok)
        index = next_step(state)
        _print_progress(state, index)
        if index is None:
            wizard_choice = questionary.Choice("> Workflow complete - show production restore instructions",
                                               "wizard")
        else:
            wizard_choice = questionary.Choice(f"> Continue guided workflow (step {index + 1}: {STEPS[index].title})",
                                               "wizard")
        choices = [wizard_choice, questionary.Separator("-- Run a single step --")]
        for i, step in enumerate(STEPS):
            mark = "[x]" if step.is_done(state) else "[ ]"
            disabled = "Docker unavailable" if step.needs_docker and not docker_ok else None
            choices.append(questionary.Choice(f"{mark} {i + 1}. {step.title}", step.key, disabled=disabled))
        choices += [
            questionary.Separator("-- Other --"),
            questionary.Choice("Clean up everything", "cleanup"),
            questionary.Choice("Re-check Docker", "recheck"),
            questionary.Choice("Exit", "exit"),
        ]
        action = questionary.select("What do you want to do?", choices=choices, default=wizard_choice).ask()
        if action in (None, "exit"):
            return
        try:
            if action == "wizard":
                run_wizard(state, docker_ok)
            elif action == "cleanup":
                reset_approvals()
                cleanup(state, docker_ok)
            elif action == "recheck":
                docker_error = docker_available()
                print(f"! {docker_error}" if docker_error else "Docker is available.")
                reconcile(state, docker_error is None)
            else:
                step = next(s for s in STEPS if s.key == action)
                print(f"\n=== {step.title} ===\n{step.explain}")
                run_step(step, state)
        except DockerError as e:
            print(f"! {e}")
        except StepCancelled as e:
            print(f"\nCancelled at: {e}. Nothing further was run.")


def main() -> int:
    try:
        run_menu()
    except KeyboardInterrupt:
        print("\nInterrupted - state is saved, run the tool again to continue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
