# IntelliShell helper scripts

Generic, parameter-driven scripts for running a Team Sharing recovery from a
Studio 3T IntelliShell. They contain **no customer data**: fill in the
parameters at the top of each file for the case at hand. The per-case insert
scripts (pre-flight, one insert per collection, post-check, rollback) are not
kept here - `../intellishell_scripts.py` generates them from the export, and
they hold customer documents and key material, so they stay in the export
folder that cleanup deletes.

| Script | Access | Use |
|---|---|---|
| `00-shell-selftest.js` | none (pure JavaScript) | Run first in the shell you will use. Embeds a synthetic 6 722-byte value four ways and reports whether each survives. If check A fails, don't insert binary data from that shell. |
| `01-diagnose-document.js` | read-only | After a doubtful insert: shell, cluster, folder counts, and the stored byte size of one document's binary field against the size you expected. |
| `02-remove-broken-document.js` | deletes one document | Removes a document that a failed insert wrote incompletely, and only that: refuses if it looks intact, belongs to another folder, is referenced by a resource, or the connection isn't production. `CONFIRM = true` required. |

Lessons these encode (from a real recovery, Nov 2026):

- IntelliShell runs a script **statement by statement and continues after a
  failed statement**. A guard that `throw`s does not stop later statements.
  Use a flag (`ABORT`) that every later statement checks.
- A single 200 KB `insertMany([...])` statement with 9 KB lines produced one
  document whose 6 722-byte binary had become 7 bytes. One short statement per
  value, and a read-back size check after each insert, avoids this.
- `printjson` output like `"inserted" : 1.0` does not mean the legacy shell;
  IntelliShell's banner names the real one (`Using Mongosh: x.y.z`).
- `hex_md5` is not available in mongosh; use a checksum you ship yourself
  (the generator uses FNV-1a with `Math.imul`).
- Always verify stored binaries with `$binarySize` after writing; a document
  can be present and still be wrong.
