# teamsharing-restore

Interactive helper for the Ops runbook *"How to recover a user's Team
Sharing data in case it was erroneously deleted"*. It takes a MongoDB Atlas
snapshot archive, restores it into a local MongoDB **Docker** container you
can open in Studio 3T, and removes everything again when you are done.

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

Workflow steps, in order:

1. **Select backup archive** - the `.tar.gz` / `.tgz` snapshot downloaded from Atlas
2. **Select working folder** - the archive is extracted into a new
   `teamsharing-restore-<archive name>` subfolder here
3. **Extract archive** - also locates the folder holding the `WiredTiger` files
4. **Create MongoDB container** - pulls `mongo:<version>` if needed, creates a
   labelled volume and container, publishes the port on `127.0.0.1` only
   (default `50000`). The version must match the backup's
   featureCompatibilityVersion (6.0 at the time the runbook was written);
   if it doesn't, the container stops and the tool points at the log lines
5. **Verify container is running** - starts it if stopped, waits for `ping`
6. **Restore data into container** - stops the container, clears the volume,
   copies the extracted data files in, starts it again and runs step 7
7. **Verify databases** - lists databases, featureCompatibilityVersion and
   document counts of the collections the runbook needs
   (`TEAM_SHARING_ENCRYPTED_PROD.{user,folder,resource,resourceContent,permission}`,
   `ENCRYPTION_PROD.__keyVault`)

Separate menu entry, run when the recovery is finished:

- **Clean up everything** - removes the container, its volume, the image
  (only if this tool pulled it), the extracted folder and the state file.
  Deleting the original archive is offered separately (default: no)

Then connect Studio 3T to `mongodb://127.0.0.1:50000/?directConnection=true`
and follow the runbook from *"Find the affected user, folder and resources"*.

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
- MongoDB in the container runs **without authentication** and is reachable
  from `127.0.0.1` only. Do not change the port binding to expose it.
- Cleanup only deletes a folder that contains the marker file this tool
  writes when it creates it, so a wrong path in the state file cannot
  delete unrelated data.
