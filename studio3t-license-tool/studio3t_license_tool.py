#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
Studio 3T license diagnostics / cleanup tool.

Detects every location where Studio 3T's desktop app (data-man-mongodb-ent)
can persist license-related state, based on a read of its source:

  - Registered/legacy license  -> t3.common.lic.registered.RegisteredLicenseManager
    (Windows registry only, via java.util.prefs; single storage key per node)
  - Account login tokens       -> t3.common.auth.T3AccountStore
  - Account-based usage token  -> t3.common.lic.accountbased.UsageTokenStore
    (both of the above go through t3.license.services.TrustedDataStore, which
    writes the same encrypted blob to up to 7 redundant locations on Windows)
  - Trial license state        -> t3.common.lic.trial.TrialLicenseManager
    (same redundant-store scheme as TrustedDataStore, different key formula)
  - Windows Group Policy       -> t3.common.lic.io.windows.GroupPolicyLicenseStore
    (read-only, org-managed - never a cleanup target)

This tool never prints the raw content of any license/token value, and never
extracts or displays customer name/company - only presence, size, and
non-identifying metadata (license id hash, edition, validity dates,
quantity), per the rule that license keys, credentials, and customer PII
must never be logged or displayed.

Usage:
    python studio3t_license_tool.py list [--json]
    python studio3t_license_tool.py clean --all [--yes] [--dry-run]
    python studio3t_license_tool.py clean --ids 3,7,12 [--yes] [--dry-run]

Without --yes, `clean` always runs as a dry run and only prints what it
would remove.

Requires Python 3.9+. No third-party dependencies - run directly with
`python studio3t_license_tool.py` or `uv run studio3t_license_tool.py`.
"""

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winreg


# ---------------------------------------------------------------------------
# Constants mirrored from the Java source
# ---------------------------------------------------------------------------

# t3.common.lic.io.LicenseStore.LICENSE_CLASS_PATH_SEGMENTS
LICENSE_CLASS_PATH_SEGMENTS = "t3/dataman/mongodb/app"

# t3.common.lic.LicenseManager: registeredManager = new RegisteredLicenseManager(meta, "license_v2")
UNIFIED_LICENSE_KEY = "license_v2"
# t3.common.lic.registered.RegisteredLicenseManager(meta, Edition): storageKey = "license"
LEGACY_LICENSE_KEY = "license"

# Editions that actually have a per-edition RegisteredLicenseManager / TrialLicenseManager
# (t3.common.lic.LicenseManager.getLegacyManagerByEdition only handles these three)
LEGACY_EDITIONS = ["enterprise", "pro", "core"]

# All Preferences nodes LicenseService/LicenseManager could plausibly touch
ALL_PREFS_EDITIONS = ["enterprise", "pro", "core", "professional", "free", "robo"]

# t3.utils.Edition.getSoftwareId()
SOFTWARE_ID = {"core": 2, "enterprise": 5, "pro": 7, "free": 9, "robo": 6, "professional": 8}

# t3.license.services.TrustedDataStore uniqueKeyName values in this codebase
TRUSTED_STORE_KEYS = {
    "t3_account_store": "Login / auth tokens (T3AccountStore)",
    "usage_token_store": "Account-based license usage token (UsageTokenStore)",
    "trusted-store": "Generic trusted app settings (DefaultTrustedPropertyStore - not license data)",
}

GROUP_POLICY_PATH = r"SOFTWARE\Policies\3T\Studio3T"


# ---------------------------------------------------------------------------
# Java-compatible hashing helpers
# ---------------------------------------------------------------------------

def _md5_b64(s: str) -> str:
    """Guava's BaseEncoding.base64().encode(MessageDigest.getInstance("MD5").digest(...))."""
    digest = hashlib.md5(s.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def _sanitize(s: str, replacement: str) -> str:
    """Java's s.replaceAll("[^a-zA-Z0-9.-]", replacement)."""
    return re.sub(r"[^a-zA-Z0-9.\-]", replacement, s)


def _java_string_hashcode(s: str) -> int:
    """Java's String.hashCode(): h = 31*h + c, as a 32-bit signed int."""
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h


def trusted_store_key(unique_key_name: str) -> str:
    """t3.license.services.TrustedDataStore.generateUniqueKeyName()."""
    return _sanitize(_md5_b64(unique_key_name).lower(), "-")


def trial_key(edition: str) -> str:
    """t3.common.lic.trial.TrialLicenseManager.generateUniqueKeyName()."""
    key_name = f"{LICENSE_CLASS_PATH_SEGMENTS}/2018/2/{SOFTWARE_ID[edition]}"
    return _sanitize(_md5_b64(key_name).lower(), "-")


# ---------------------------------------------------------------------------
# Registered/legacy license decoding (t3.common.lic.registered.*)
#
# Read-only, best-effort decoding of the *already installed* license document
# for display purposes. This deliberately does NOT verify the RSA signature
# (t3.common.lic.registered.SignedStringContainer uses SHA256withRSA against
# RootCertificates' public key) - that check only matters for the app
# deciding whether to trust/activate a key, not for reading back metadata
# from a key that is already sitting, already-trusted, on this machine.
#
# The raw signed key string itself is never printed anywhere below.
# ---------------------------------------------------------------------------

# t3.common.lic.registered.RegisteredLicense.getEditionsFromSoftwareId()
_SOFTWARE_ID_DIGIT_TO_EDITION = {
    "2": "CORE", "5": "ENTERPRISE", "1": "PRO", "3": "PRO", "7": "PRO", "8": "PROFESSIONAL",
}


def license_id_from_raw_key(raw_key: str) -> str:
    """RegisteredLicense.getLicenseId(): sha1(licenseKey bytes) uppercased."""
    return hashlib.sha1(raw_key.encode("utf-8")).hexdigest().upper()


def _editions_from_software_id(software_id: str) -> List[str]:
    seen = []
    for digit in software_id:
        edition = _SOFTWARE_ID_DIGIT_TO_EDITION.get(digit)
        if edition and edition not in seen:
            seen.append(edition)
    return seen


def decode_registered_license_info(raw_key: str) -> Dict[str, object]:
    """
    Best-effort decode of a RegisteredLicenseManager value into display-only
    metadata: license id, edition(s), validity dates, customer/company name.

    Mirrors SignedStringContainer's parsing (version\\content\\signature, all
    base64) and RegisteredLicense.fromStringVersion0/1's '|'-delimited content
    format, without verifying the RSA signature.
    """
    info: Dict[str, object] = {"license_id": license_id_from_raw_key(raw_key)}

    cleaned = re.sub(r"[^0-9a-zA-Z/=+\\]", "", raw_key)
    segments = cleaned.split("\\")
    if len(segments) != 3:
        info["parse_error"] = "unexpected key format (not version\\content\\signature)"
        return info

    try:
        content = base64.b64decode(segments[1]).decode("utf-8")
    except Exception:
        info["parse_error"] = "could not base64/utf-8 decode license content"
        return info

    parts = content.split("|")
    version = 0
    if parts and parts[0].startswith("v"):
        try:
            version = int(parts[0][1:])
        except ValueError:
            pass

    try:
        if version == 0 and len(parts) == 9:
            software_id, quantity = parts[0], parts[2]
            info["timebomb"] = parts[3] == "true"
            # Deliberately not captured: customer name/company (parts[4]/parts[5]).
            # This tool must never surface that content - see module docstring.
            valid_from, valid_until = parts[6], parts[7]
        elif version == 1 and len(parts) == 11:
            software_id = parts[1]
            quantity = "volume" if parts[2] == "sl" else parts[2]
            info["timebomb"] = parts[3] == "true"
            info["unique_id"] = parts[4]
            valid_from, valid_until = parts[5], parts[6]
            # Deliberately not captured: customer name/company (parts[8]/parts[9]).
            # This tool must never surface that content - see module docstring.
        else:
            info["parse_error"] = f"unrecognized content format (version={version}, {len(parts)} fields)"
            return info

        info["editions"] = _editions_from_software_id(software_id)
        info["quantity"] = quantity
        info["valid_from"] = valid_from
        info["valid_until"] = valid_until
    except (IndexError, ValueError) as ex:
        info["parse_error"] = f"failed to parse content fields: {ex}"

    return info


# ---------------------------------------------------------------------------
# File path providers, mirroring t3.common.lic.io.* / t3.common.lic.io.windows.*
# ---------------------------------------------------------------------------

def _home_dir() -> Path:
    return Path.home()


def home_directory_file(key: str) -> Path:
    """HomeDirectoryFileNameProvider -> <home>/.3T/studio-3t/<key>/settings.dat"""
    return _home_dir() / ".3T" / "studio-3t" / _sanitize(key, "_") / "settings.dat"


def home_directory2_file(key: str) -> Path:
    """HomeDirectory2FileNameProvider -> <home>/.cache/<md5(segments)>/<md5(key)>"""
    settings_dir = _sanitize(_md5_b64(LICENSE_CLASS_PATH_SEGMENTS.replace("/", ".")), "-")
    file_name = _sanitize(_md5_b64(key), "")
    return _home_dir() / ".cache" / settings_dir / file_name


def local_appdata_file(key: str) -> Path:
    """LocalAppDataFileNameProvider -> <home>/AppData/Local/t3/dataman/mongodb/app/<key>/data.dat"""
    base = _home_dir() / "AppData" / "Local"
    return base.joinpath(*LICENSE_CLASS_PATH_SEGMENTS.split("/")) / _sanitize(key, "_") / "data.dat"


def local_appdata2_file(key: str) -> Path:
    """LocalAppData2FileNameProvider -> <home>/AppData/Local/<md5(segments)>/<key>/{GUID}"""
    base = _home_dir() / "AppData" / "Local"
    settings_dir = _sanitize(_md5_b64(LICENSE_CLASS_PATH_SEGMENTS.replace("/", ".")), "-")
    return base / settings_dir / _sanitize(key, "_") / "{1C957182-3C66-4883-11CD-E6C41BA710CA}"


def temp_dir() -> Path:
    return Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")


def temp_directory_file(key: str) -> Path:
    """TempDirectoryFileNameProvider -> <temp>/t3/dataman/mongodb/app/<key>.tmp"""
    return temp_dir().joinpath(*LICENSE_CLASS_PATH_SEGMENTS.split("/")) / (_sanitize(key, "_") + ".tmp")


def temp_directory2_file(key: str) -> Path:
    """TempDirectory2FileNameProvider -> <temp>/<md5(segments)>/<md5(key)>"""
    settings_dir = _sanitize(_md5_b64(LICENSE_CLASS_PATH_SEGMENTS.replace("/", ".")), "-")
    file_name = _sanitize(_md5_b64(key), "-")
    return temp_dir() / settings_dir / file_name


def public_directory_file(key: str) -> Path:
    """PublicDirectoryFileNameProvider -> C:/Users/Public/t3/dataman/mongodb/app/<key><hashcode(username)>"""
    base = _home_dir().parent / "Public"
    username = getpass.getuser().lower()
    file_name = _sanitize(key, "_") + str(_java_string_hashcode(username))
    return base.joinpath(*LICENSE_CLASS_PATH_SEGMENTS.split("/")) / file_name


# The 6 file-based providers FileLicenseStoreFactory adds on Windows
# (the 7th instance in that factory is PreferencesLicenseStore, handled separately below)
WINDOWS_FILE_PROVIDERS: List[Callable[[str], Path]] = [
    home_directory_file,
    home_directory2_file,
    local_appdata_file,
    local_appdata2_file,
    temp_directory_file,
    temp_directory2_file,
    public_directory_file,
]


# ---------------------------------------------------------------------------
# Registry helpers (Windows only)
# ---------------------------------------------------------------------------

def _hive_name(hive) -> str:
    return {winreg.HKEY_CURRENT_USER: "HKCU", winreg.HKEY_LOCAL_MACHINE: "HKLM"}.get(hive, str(hive))


def registry_read(hive, path: str, name: str) -> Optional[str]:
    if not IS_WINDOWS:
        return None
    try:
        with winreg.OpenKey(hive, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except FileNotFoundError:
        return None
    except OSError:
        return None


def registry_delete(hive, path: str, name: str) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Item model
# ---------------------------------------------------------------------------

@dataclass
class Item:
    id: int
    category: str
    description: str
    kind: str  # "registry" or "file"
    location: str  # human-readable location, no secrets
    present: bool
    size: Optional[int] = None
    deletable: bool = True
    note: str = ""
    info: Optional[Dict[str, object]] = None
    _delete: Optional[Callable[[], bool]] = field(default=None, repr=False)

    def delete(self) -> bool:
        if self._delete is None:
            return False
        return self._delete()


def _make_registry_item(id_, category, description, hive, path, name, deletable=True, note="",
                         decode_license=False) -> Item:
    value = registry_read(hive, path, name) if IS_WINDOWS else None
    present = value is not None
    size = len(value) if present else None
    location = f"{_hive_name(hive)}\\{path} [{name}]" if IS_WINDOWS else f"(non-Windows) {path} [{name}]"

    info = None
    if present and decode_license:
        try:
            info = decode_registered_license_info(value)
        except Exception as ex:  # best-effort - never let decoding break detection
            info = {"parse_error": f"decode failed: {ex}"}

    def _delete():
        return registry_delete(hive, path, name)

    return Item(
        id=id_, category=category, description=description, kind="registry",
        location=location, present=present, size=size,
        deletable=deletable and IS_WINDOWS, note=note, info=info,
        _delete=_delete if deletable else None,
    )


def _make_file_item(id_, category, description, path: Path, deletable=True, note="") -> Item:
    present = path.exists()
    size = path.stat().st_size if present else None

    def _delete():
        try:
            path.unlink()
            return True
        except OSError:
            return False

    return Item(
        id=id_, category=category, description=description, kind="file",
        location=str(path), present=present, size=size,
        deletable=deletable, note=note,
        _delete=_delete if deletable else None,
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_all() -> List[Item]:
    items: List[Item] = []
    next_id = 1

    def add(item_factory, *args, **kwargs):
        nonlocal next_id
        item = item_factory(next_id, *args, **kwargs)
        next_id += 1
        items.append(item)

    if not IS_WINDOWS:
        print("NOTE: registry-backed detection is only implemented for Windows; "
              "only redundant file-store checks will run on this OS.", file=sys.stderr)

    # 1. Unified registered/legacy license: single node, always Enterprise-scoped
    add(
        _make_registry_item, "Registered license (unified)",
        "Legacy license installed via 'Add a legacy license' (any edition)",
        winreg.HKEY_CURRENT_USER if IS_WINDOWS else None,
        r"SOFTWARE\JavaSoft\Prefs\3t\mongochef\enterprise",
        UNIFIED_LICENSE_KEY, decode_license=True,
    )

    # 2. Old per-edition legacy license keys, one node per edition
    for edition in LEGACY_EDITIONS:
        add(
            _make_registry_item, "Registered license (legacy per-edition)",
            f"Old-style '{edition}' edition registered license",
            winreg.HKEY_CURRENT_USER if IS_WINDOWS else None,
            rf"SOFTWARE\JavaSoft\Prefs\3t\mongochef\{edition}",
            LEGACY_LICENSE_KEY, decode_license=True,
        )

    # 3. Pre-migration LegacyPreferencesLicenseStore fallback nodes
    add(
        _make_registry_item, "Registered license (pre-migration fallback)",
        "Old Enterprise Preferences node (LegacyPreferencesLicenseStore)",
        winreg.HKEY_CURRENT_USER if IS_WINDOWS else None,
        r"SOFTWARE\JavaSoft\Prefs\3t\enterprise\mongochef",
        UNIFIED_LICENSE_KEY, decode_license=True,
    )
    add(
        _make_registry_item, "Registered license (pre-migration fallback)",
        "Old non-Enterprise Preferences node (LegacyPreferencesLicenseStore)",
        winreg.HKEY_CURRENT_USER if IS_WINDOWS else None,
        r"SOFTWARE\JavaSoft\Prefs\t3\dataman\mongodb\app",
        LEGACY_LICENSE_KEY, decode_license=True,
    )

    # 4. Group Policy - org-managed, read-only, never deletable
    for hive in ([winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE] if IS_WINDOWS else [None]):
        add(
            _make_registry_item, "Group Policy license",
            "Enterprise Group Policy license (organization-managed)",
            hive, GROUP_POLICY_PATH, UNIFIED_LICENSE_KEY,
            deletable=False, note="org-managed - not deletable by this tool",
        )

    # 5. Trusted-store-backed data: login tokens, usage token, generic trusted settings
    for store_key, description in TRUSTED_STORE_KEYS.items():
        hashed_key = trusted_store_key(store_key)
        category = f"Trusted store: {store_key}"

        # PreferencesLicenseStore(Edition.ENTERPRISE) is one of FileLicenseStoreFactory's instances
        add(
            _make_registry_item, category, f"{description} (registry copy)",
            winreg.HKEY_CURRENT_USER if IS_WINDOWS else None,
            r"SOFTWARE\JavaSoft\Prefs\3t\mongochef\enterprise",
            hashed_key,
        )

        if IS_WINDOWS:
            for provider in WINDOWS_FILE_PROVIDERS:
                path = provider(hashed_key)
                add(_make_file_item, category, f"{description} ({provider.__name__})", path)

    # 6. Trial license state (per edition that actually has a TrialLicenseManager)
    for edition in LEGACY_EDITIONS:
        hashed_key = trial_key(edition)
        category = f"Trial license ({edition})"

        add(
            _make_registry_item, category, f"Trial license state for '{edition}' (registry copy)",
            winreg.HKEY_CURRENT_USER if IS_WINDOWS else None,
            rf"SOFTWARE\JavaSoft\Prefs\3t\mongochef\{edition}",
            hashed_key,
        )

        if IS_WINDOWS:
            for provider in WINDOWS_FILE_PROVIDERS:
                path = provider(hashed_key)
                add(_make_file_item, category, f"Trial license state for '{edition}' ({provider.__name__})", path)

    return items


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_license_info(info: Dict[str, object]) -> str:
    lines = [f"        License ID: {info.get('license_id')}"]
    if "parse_error" in info:
        lines.append(f"        (basic info unavailable: {info['parse_error']})")
        return "\n".join(lines)

    editions = info.get("editions") or []
    if editions:
        lines.append(f"        Edition(s): {', '.join(editions)}")
    if info.get("valid_from") or info.get("valid_until"):
        lines.append(f"        Valid: {info.get('valid_from', '?')} - {info.get('valid_until', '?')}")
    if info.get("quantity") is not None:
        lines.append(f"        Quantity: {info['quantity']}")
    if info.get("timebomb"):
        lines.append("        Timebomb mode: true")
    return "\n".join(lines)


def cmd_list(items: List[Item], as_json: bool) -> None:
    present_items = [i for i in items if i.present]

    if as_json:
        print(json.dumps([
            {
                "id": i.id, "category": i.category, "description": i.description,
                "kind": i.kind, "location": i.location, "size": i.size,
                "deletable": i.deletable, "note": i.note, "info": i.info,
            }
            for i in present_items
        ], indent=2))
        return

    if not present_items:
        print("No license-related state found in any known location.")
        return

    print(f"Found {len(present_items)} present item(s) (of {len(items)} locations checked):\n")
    last_category = None
    for item in present_items:
        if item.category != last_category:
            print(f"\n[{item.category}]")
            last_category = item.category
        flags = "" if item.deletable else "  (read-only, org-managed)"
        size_str = f"{item.size} bytes" if item.size is not None else "?"
        print(f"  #{item.id:<3} {item.description}")
        print(f"        {item.kind}: {item.location}  ({size_str}){flags}")
        if item.info:
            print(_format_license_info(item.info))

    print(
        "\nNo values are printed - only presence and size, per policy on never logging "
        "license/credential content.\n"
        "Run with 'clean --ids <id,id,...>' or 'clean --all' to remove entries (dry run by default)."
    )


def cmd_clean(items: List[Item], ids: Optional[List[int]], clean_all: bool, dry_run: bool, assume_yes: bool) -> None:
    if clean_all:
        targets = [i for i in items if i.present]
    else:
        id_set = set(ids or [])
        targets = [i for i in items if i.id in id_set]
        missing = id_set - {i.id for i in items}
        if missing:
            print(f"Unknown id(s), ignored: {sorted(missing)}", file=sys.stderr)

    if not targets:
        print("Nothing to clean.")
        return

    skipped_not_present = [i for i in targets if not i.present]
    skipped_not_deletable = [i for i in targets if i.present and not i.deletable]
    to_delete = [i for i in targets if i.present and i.deletable]

    for i in skipped_not_present:
        print(f"  skip  #{i.id} - not present: {i.description}")
    for i in skipped_not_deletable:
        print(f"  skip  #{i.id} - {i.note or 'not deletable'}: {i.description}")

    if not to_delete:
        print("Nothing deletable in the selection.")
        return

    really_delete = assume_yes and not dry_run

    verb = "Would remove" if not really_delete else "Removing"
    for i in to_delete:
        print(f"  {verb:<10} #{i.id:<3} [{i.kind}] {i.location}")

    if not really_delete:
        print(
            f"\nDry run - {len(to_delete)} item(s) would be removed. "
            "Re-run with --yes to actually delete them."
        )
        return

    ok, failed = 0, 0
    for i in to_delete:
        if i.delete():
            ok += 1
        else:
            failed += 1
            print(f"  FAILED to remove #{i.id}: {i.location}", file=sys.stderr)

    print(f"\nDone. Removed {ok} item(s), {failed} failure(s).")


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _confirm(text: str) -> bool:
    return _prompt(f"{text} [y/N]: ").lower() in ("y", "yes")


def _menu_list() -> None:
    # Re-detect right now, not whatever was current when the menu was drawn -
    # state can change between opening the menu and picking this option
    # (e.g. you import a license in the app while the tool sits idle).
    items = detect_all()
    print()
    cmd_list(items, as_json=False)


def _menu_clean(clean_all: bool) -> None:
    items = detect_all()
    present = [i for i in items if i.present]
    if not present:
        print("\nNothing detected - nothing to clean.")
        return

    ids: Optional[List[int]] = None
    if not clean_all:
        cmd_list(items, as_json=False)
        raw = _prompt("\nEnter item id(s) to clean, comma-separated (or blank to cancel): ")
        if not raw:
            print("Cancelled.")
            return
        try:
            ids = [int(x) for x in raw.split(",")]
        except ValueError:
            print("Could not parse that as a list of ids. Cancelled.")
            return

    print("\n--- Dry run preview ---")
    cmd_clean(items, ids, clean_all, dry_run=True, assume_yes=False)

    if not _confirm("\nActually delete the item(s) shown above?"):
        print("Cancelled - nothing was deleted.")
        return

    # Re-detect immediately before the real deletion too: the dry-run preview
    # above may be stale by the time the user confirms (they could take a
    # while to answer), and ids/locations must reflect what's on disk now.
    items = detect_all()
    print()
    cmd_clean(items, ids, clean_all, dry_run=False, assume_yes=True)


def _menu_export_json() -> None:
    items = detect_all()
    default_path = "studio3t_license_state.json"
    path = _prompt(f"Output file path [{default_path}]: ") or default_path
    present_items = [i for i in items if i.present]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([
            {
                "id": i.id, "category": i.category, "description": i.description,
                "kind": i.kind, "location": i.location, "size": i.size,
                "deletable": i.deletable, "note": i.note, "info": i.info,
            }
            for i in present_items
        ], fh, indent=2)
    print(f"Wrote {len(present_items)} item(s) to {path}")


def run_menu() -> None:
    print("=" * 60)
    print(" Studio 3T License Diagnostics")
    print("=" * 60)
    if not IS_WINDOWS:
        print(" (running on a non-Windows OS - registry checks are skipped)")

    while True:
        # Fresh detection just for the summary count shown in the menu header.
        # Each option below re-detects again itself right when it's chosen,
        # so nothing here is reused stale across the prompt.
        header_items = detect_all()
        present_count = sum(1 for i in header_items if i.present)

        print(f"\n{present_count} item(s) currently present, {len(header_items)} location(s) checked.\n")
        print("  1) List detected license state")
        print("  2) Clean selected item(s)")
        print("  3) Clean ALL detected item(s)")
        print("  4) Export detected state to a JSON file")
        print("  5) Exit")

        choice = _prompt("\nChoose an option [1-5]: ")

        if choice == "1":
            _menu_list()
        elif choice == "2":
            _menu_clean(clean_all=False)
        elif choice == "3":
            _menu_clean(clean_all=True)
        elif choice == "4":
            _menu_export_json()
        elif choice == "5" or choice.lower() in ("q", "quit", "exit"):
            print("Bye.")
            return
        else:
            print("Not a valid option, try again.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("menu", help="Interactive menu (also the default when run with no arguments)")

    p_list = sub.add_parser("list", help="Detect and list all present license-related state")
    p_list.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    p_clean = sub.add_parser("clean", help="Remove selected (or all) detected license-related state")
    group = p_clean.add_mutually_exclusive_group(required=True)
    group.add_argument("--ids", type=str, help="Comma-separated item ids to remove, e.g. 3,7,12")
    group.add_argument("--all", action="store_true", help="Remove every present, deletable item")
    p_clean.add_argument("--yes", action="store_true", help="Actually perform deletion (otherwise dry run)")
    p_clean.add_argument("--dry-run", action="store_true", help="Force dry run even if --yes is given")

    args = parser.parse_args()

    if args.command is None or args.command == "menu":
        run_menu()
        return 0

    items = detect_all()

    if args.command == "list":
        cmd_list(items, args.json)
    elif args.command == "clean":
        ids = [int(x) for x in args.ids.split(",")] if args.ids else None
        cmd_clean(items, ids, args.all, args.dry_run, args.yes)

    return 0


if __name__ == "__main__":
    sys.exit(main())
