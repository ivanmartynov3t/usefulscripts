# studio3t-license-tool

A diagnostics/cleanup tool for **Studio 3T** desktop app license state on
Windows. It answers "why isn't my license showing up?" by finding every
place the app can persist license-related data, and lets you clear stale
entries so a fresh license import isn't shadowed by leftover state.

## Requirements

- Windows (registry-backed checks only run there; on other OSes it still
  reports the redundant file-store locations)
- Python 3.9+, no third-party dependencies

## Usage

With [uv](https://docs.astral.sh/uv/) (recommended - no venv setup needed):

```bash
uv run studio3t_license_tool.py
```

Or with plain Python:

```bash
python studio3t_license_tool.py
```

Both launch an interactive menu. Non-interactive subcommands are also
available:

```bash
# List everything currently present
python studio3t_license_tool.py list
python studio3t_license_tool.py list --json

# Preview what a cleanup would remove (dry run is the default)
python studio3t_license_tool.py clean --all
python studio3t_license_tool.py clean --ids 3,7,12

# Actually delete
python studio3t_license_tool.py clean --all --yes
```

`clean` always dry-runs unless you pass `--yes`; org-managed Group Policy
entries are never deletable.

## Example output

```
Found 5 present item(s) (of 56 locations checked):

[Registered license (unified)]
  #1   Legacy license installed via 'Add a legacy license' (any edition)
        registry: HKCU\SOFTWARE\JavaSoft\Prefs\3t\mongochef\enterprise [license_v2]  (654 bytes)
        License ID: 0E92D0890A788A00EEA2DE979DF23008841A03A2
        Edition(s): ENTERPRISE
        Valid: 25.08.2025 - 25.08.2026

[Trusted store: t3_account_store]
  #9   Login / auth tokens (T3AccountStore) (registry copy)
        registry: HKCU\SOFTWARE\JavaSoft\Prefs\3t\mongochef\enterprise [...]  (4644 bytes)
```

## Privacy

This tool never prints the raw content of any license/token value, and
never extracts or displays customer name/company - only presence, size,
and non-identifying metadata (a SHA-1 hash used as a license id, edition,
validity dates, quantity, timebomb flag).

---

## How Studio 3T actually stores license state on Windows

Everything below is a plain-language writeup of what this script encodes,
based on reading the app's own source. It explains *why* a stale license
can survive reinstalls, and why re-importing a license doesn't always fix
things.

### Why there are so many storage locations

The desktop app never trusts a single place to hold license/token data.
For every logical "key" it wants to persist (the registered license, an
account login token, a usage token, trial state, ...), it writes the same
value out to **multiple independent stores** and, on read, tries them in
order until one succeeds. This is deliberate redundancy/tamper-resistance,
not a bug - but it means a `clean`/uninstall/reinstall that only clears
*one* of those locations leaves the license effectively still installed,
because the app will happily fall back to a leftover copy.

On Windows, a "trusted" key (login tokens, usage tokens) is written to
**7 locations**: one Windows Registry value plus 6 separate files. A
"registered/legacy" license is simpler - Registry only, via
`java.util.prefs` (which on Windows is itself backed by the registry).

### 1. Registered/legacy license (`Add a legacy license`)

Single Windows Registry value, read/written through Java's
`java.util.prefs.Preferences` API. There are actually **4 possible
locations** the app might read from before falling back to "no license",
due to product history (a unified format replacing older per-edition
ones):

| Purpose | Registry path | Value name |
|---|---|---|
| Current/unified license (any edition) | `HKCU\SOFTWARE\JavaSoft\Prefs\3t\mongochef\enterprise` | `license_v2` |
| Old per-edition license (Enterprise) | `HKCU\SOFTWARE\JavaSoft\Prefs\3t\mongochef\enterprise` | `license` |
| Old per-edition license (Pro) | `HKCU\SOFTWARE\JavaSoft\Prefs\3t\mongochef\pro` | `license` |
| Old per-edition license (Core) | `HKCU\SOFTWARE\JavaSoft\Prefs\3t\mongochef\core` | `license` |
| Pre-migration fallback (Enterprise) | `HKCU\SOFTWARE\JavaSoft\Prefs\3t\enterprise\mongochef` | `license_v2` |
| Pre-migration fallback (non-Enterprise) | `HKCU\SOFTWARE\JavaSoft\Prefs\t3\dataman\mongodb\app` | `license` |

The stored value is a signed document, ASCII, structured as
`version\content\signature`, all three segments base64-encoded and
separated by backslashes:

- `version` - currently unused by decoding, reserved
- `content` - base64 of a `|`-delimited plaintext record (see below)
- `signature` - base64 of a SHA256withRSA signature over `content`,
  verified against the app's embedded root certificate

The `content` record comes in two formats depending on a leading `vN`
marker:

- **v0** (legacy, 9 fields): `softwareId|?|quantity|timebomb|customerName|customerCompany|validFrom|validUntil|?`
- **v1** (current, 11 fields): `v1|softwareId|quantity(or "sl" for site license)|timebomb|uniqueId|validFrom|validUntil|notes|customerName|customerCompany|terminator`

Dates are `dd.MM.yyyy`. `softwareId` is a set of digits, each mapping to
an edition (a key can grant more than one edition at once):

| Digit | Edition |
|---|---|
| `2` | Core |
| `5` | Enterprise |
| `1`, `3`, `7` | Pro |
| `8` | Professional |

This tool computes a stable, non-identifying **license id** as
`sha1(raw_registry_value).hexdigest().upper()` purely so you can tell two
entries apart without printing their content.

**Timebomb expiry rule**: if `timebomb=true`, the license is expired once
the *app's start date, in the user's local timezone*, is after
`validUntil` - a plain calendar-day comparison, no NTP or build-time
involved. (Non-timebombed licenses instead compare against the app's
*build* date at UTC - a different, rarer rule for free-upgrade-period
licenses.)

**Read-only Group Policy override**: `HKCU`/`HKLM` under
`SOFTWARE\Policies\3T\Studio3T` (`license_v2` value) is checked *before*
any of the above, for centrally-managed enterprise deployments. It's
never a cleanup target - it's organization-managed and read-only by
design.

### 2. Trusted store data (login tokens, usage tokens)

Login/auth tokens and the account-based license usage token both go
through a generic "trusted store" mechanism that writes the *same*
encrypted blob to **7 redundant locations** at once: 1 registry value +
6 files. On read, the app tries them in this order and uses the first
one it finds.

The storage key isn't the logical name directly - it's derived:

```
hashed_key = sanitize( lower( base64( md5(logical_key_name) ) ), "-" )
```

i.e. MD5-hash the logical key name, base64-encode the digest, lowercase
it, then replace every character that isn't `[a-zA-Z0-9.-]` with `-`
(mirrors Guava's `BaseEncoding` + a Java `replaceAll`). The known logical
key names in this codebase:

| Logical key | Purpose |
|---|---|
| `t3_account_store` | Login / auth tokens |
| `usage_token_store` | Account-based license usage token |
| `trusted-store` | Generic trusted app settings (not license data) |

The 7 physical locations for a given hashed key:

| # | Location | Path pattern |
|---|---|---|
| 1 | Registry | `HKCU\SOFTWARE\JavaSoft\Prefs\3t\mongochef\enterprise [<hashed_key>]` |
| 2 | Home directory file | `%USERPROFILE%\.3T\studio-3t\<hashed_key>\settings.dat` |
| 3 | Home directory (cache variant) | `%USERPROFILE%\.cache\<md5(class-path-segments)>\<md5(hashed_key)>` |
| 4 | Local AppData | `%LOCALAPPDATA%\t3\dataman\mongodb\app\<hashed_key>\data.dat` |
| 5 | Local AppData (variant) | `%LOCALAPPDATA%\<md5(class-path-segments)>\<hashed_key>\{1C957182-3C66-4883-11CD-E6C41BA710CA}` |
| 6 | Temp directory | `%TEMP%\t3\dataman\mongodb\app\<hashed_key>.tmp` |
| 7 | Temp directory (variant) | `%TEMP%\<md5(class-path-segments)>\<md5(hashed_key)>` |
| 8 | Public directory | `C:\Users\Public\t3\dataman\mongodb\app\<hashed_key><java_hashcode(username)>` |

(`class-path-segments` is the literal string `t3/dataman/mongodb/app`,
used as a namespacing component in several of the derived paths, with
`.`s substituted for `/`s before hashing.)

`<java_hashcode(username)>` reproduces Java's `String.hashCode()`
(`h = 31*h + c` over each char, wrapped to a 32-bit signed int) applied
to the lowercased OS username - used to make the Public-directory
filename collision-resistant across multiple users on a shared machine.

### 3. Trial license state

Same redundant-store scheme as above (registry + the same 7 file
locations), but with a different key formula, computed per edition:

```
key_name  = "t3/dataman/mongodb/app/2018/2/<softwareId(edition)>"
hashed_key = sanitize( lower( base64( md5(key_name) ) ), "-" )
```

checked for `enterprise`, `pro`, and `core` (the only editions that
actually have per-edition trial managers in this codebase).

### Why re-importing a license doesn't always "fix" it

Because of the redundancy above, importing a new license only overwrites
the *primary* write path - it doesn't necessarily clear every stale copy
in every fallback location, and the app's own candidate-selection logic
can end up preferring a leftover entry (e.g. a stale, expired copy) over
what you just imported, depending on read order and which fallbacks
still resolve. Concretely:

1. List first (`list` / interactive option 1) to see what's actually
   present across all 56 checked locations before touching anything.
2. Clean the *specific* stale ids you don't want (`clean --ids ...`),
   rather than blindly wiping everything, if you want to keep e.g. a
   valid account-based login while only clearing a bad legacy license.
3. Re-launch the app and re-import.

If a license still won't show up after a full clean + reimport, the
problem is elsewhere in the app's license-resolution logic (candidate
ranking, built-in/manifest-embedded licenses being force-reinstalled on
startup, etc.) rather than leftover storage - this tool only deals with
*storage*, not resolution logic.
