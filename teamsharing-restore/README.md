# teamsharing-restore

Guided wizard for the Ops runbook *"How to recover a user's Team Sharing
data in case it was erroneously deleted"*. It restores a MongoDB Atlas
snapshot into a local MongoDB **Docker** container, finds the user's deleted
documents, re-assigns them to the user's current ID and exports them ready
for the (manual) production insert - then removes everything again.

## Requirements

- Docker Desktop (running)
- Python 3.9+ and [`questionary`](https://pypi.org/project/questionary/)

## Usage

With [uv](https://docs.astral.sh/uv/) (recommended - installs `questionary` automatically):

```bash
uv run teamsharing_restore.py
```

Or with plain Python:

```bash
pip install questionary
python teamsharing_restore.py
```

The tool works as a **guided wizard**: on start it offers to begin (or
resume) the workflow at the first unfinished step, runs it, and asks before
moving on to the next one. You can stop at any point; the menu that follows
shows progress (`[x]` done, `[ ]` open), keeps "Continue guided workflow"
as the default choice, and still lets you run any single step again (e.g.
recreate the container with another MongoDB version).

**Nothing runs without your OK.** Each step first explains what it does.
Before every command - `docker` commands, `mongosh` scripts (shown in full),
creating, writing or deleting files and folders - the tool prints its
purpose and the exact command and asks:

```
--- Next action: Stop the container so mongod releases the data files
    docker stop 3cad91456b45...
? Run it?
 » Yes, run it
   Yes, and run the remaining commands of this step without asking
   No, cancel this step
```

Cancelling stops the step right there; it can be repeated later. Only
internal read-only status checks (`docker info` / `inspect` / `ps`, the
mongod `ping` while waiting for startup) run without asking.

Workflow steps, in order:

1. **Select working folder** - the folder holding the downloaded backup
   (defaults to `Downloads`); the archive is extracted into a new
   `teamsharing-restore-<archive name>` subfolder here
2. **Select backup archive** - lists the `.tar.gz` / `.tgz` files found in
   the working folder (newest first, with size and date); "Another file..."
   lets you enter any other path
3. **Restore backup into local MongoDB** - one step that:
   - extracts the archive and locates the folder holding the `WiredTiger` files
   - pulls `mongo:<version>` if needed (default 7.0), creates a labelled
     volume and container (not started yet), port published on `127.0.0.1`
     only (default `50000`)
   - copies the backup files into the container and starts MongoDB once
   - if the data files are newer than the container's mongod (exit code 62),
     reads the required version from the log and offers to recreate the
     container with it and load the backup again
   - verifies the databases: featureCompatibilityVersion and document counts
     of the collections the runbook needs
     (`TEAM_SHARING_ENCRYPTED_PROD.{user,folder,resource,resourceContent,permission}`,
     `ENCRYPTION_PROD.__keyVault`)

   Parts already done are skipped: after a restart it only starts the
   stopped container.
4. **Identify the affected user IDs** - looks the email up in `user`
   (case-insensitive); you pick the old ID that lost its data (typically
   `google-oauth2|...`, no license) and the ID that should own it now.
   If the user has only one ID (data deleted, not moved to another
   account), choose **Same ID** - the documents are then recovered
   unchanged
5. **Find the deleted documents** - the runbook's queries, for all of the
   old user's folders at once:

   | Collection | Query |
   |---|---|
   | `folder` | `{"creator.userId": OLD}` |
   | `resource` | `{$or: [{folderId: {$in: folderIds}}, {"creator.userId": OLD}]}` |
   | `resourceContent` | `{$or: [{folderId: {$in: folderIds}}, {_id: {$in: resource contentIds}}]}` |
   | `permission` | `{$or: [{folderId: {$in: folderIds}}, {"creator.userId": OLD}, {"addressee.email": EMAIL}]}` |
   | `ENCRYPTION_PROD.__keyVault` | `{keyAltNames: {$in: ["data-key-1-folder-key-<folderId>", ...]}}` |

   Warns if a folder has no encryption key.
6. **Fix ownership and export** - replaces every value equal to the old ID
    with the new ID, in the **local** restored copy and in JSON files
    (MongoDB Extended JSON, canonical) written to
    `<working folder>/teamsharing-recovery-export/`, numbered in the
    production insert order: `1-…__keyVault`, `2-…folder`,
    `3-…permission`, `4-…resource`, `5-…resourceContent`. Documents that
    still mention the old ID inside a longer value are flagged for review.
    With **Same ID** nothing is replaced: the documents are exported as they
    are in the backup and the local copy is not modified.
7. **Rehearse the production restore** - a model of the production insert
   in a second container (same MongoDB version, same backup, **no port
   published**, ~1 GB extra disk) that plays "production":
   1. runs the runbook queries on its own copy of the backup and applies
      the same ID fix, giving the **expected** documents - independent of
      the export
   2. deletes those documents (simulating the erroneous deletion)
   3. inserts the export files in production order, insert only
   4. compares every stored document with the expected one and every
      collection count with the backup

   It fails if the export misses a document, holds an extra one, or any
   document differs from the backup:

   ```
   Collection                                  expected export deleted inserted identical total before total after
   ENCRYPTION_PROD.__keyVault                         1      1       1        1         1            1           1
   TEAM_SHARING_ENCRYPTED_PROD.resource              51     51      51       51        51        12614       12614
   ...
   Rehearsal passed: ...
   ```

   The rehearsal container can be removed right away; cleanup removes it
   too.

The production insert itself stays manual, as in the runbook: with
production access, insert the documents in the order above (insert only,
no upsert/overwrite) - copy them in Studio 3T from the local copy
(`mongodb://127.0.0.1:50000/?directConnection=true`) or import the export
files. The wizard prints these instructions at the end.

Separate menu entry, run when the recovery is finished:

- **Clean up everything** - removes the container, its volume, the image
  (only if this tool pulled it), the extracted folder, the export folder and
  the state file. If an export folder exists, it first asks whether to keep
  it (default - it holds the documents for the production insert), delete
  it, or cancel. The backup archive itself is never deleted. Removing the
  container with `-v` also removes the unnamed `/data/configdb` volume the
  mongo image creates. Afterwards it checks that every removed item is really
  gone (and that no labelled containers or volumes remain) and prints the result.

## Restarts

Everything the tool creates is recorded in
`%LOCALAPPDATA%\teamsharing-restore\state.json`
(`~/.local/state/teamsharing-restore/state.json` elsewhere). On every start
it checks whether the recorded container, volume, extracted folder and
archive still exist and drops references to anything that was removed in
the meantime. Docker objects carry the label
`usefulscripts.teamsharing-restore=1`, so cleanup also finds them if the
state file is lost.

## Security notes

- The backup contains production customer data. Keep it on an encrypted
  disk and run cleanup as soon as the recovery is finished.
- The export folder contains customer documents **and folder encryption key
  material** from `__keyVault`. Don't copy it anywhere else; cleanup deletes it.
- The state file holds the affected user's email and user IDs until cleanup.
- The tool never connects to production; it only writes to the local
  container and the local export folder.
- MongoDB in the container runs **without authentication** and is reachable
  from `127.0.0.1` only. Do not change the port binding to expose it.
- Cleanup only deletes a folder that contains the marker file this tool
  writes when it creates it, so a wrong path in the state file cannot
  delete unrelated data.
