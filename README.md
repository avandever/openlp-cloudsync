# OpenLP Cloud Sync

A community plugin for [OpenLP](https://openlp.org/) that keeps your church's
song library in sync across machines via Google Drive.

Install it on each computer, sign in with Google, and hit **Sync Now**.
New songs, edits, and deletions propagate to every other machine on its next
sync. It also keeps a rolling history of backups on Drive, so a bad sync is
always recoverable.

## How syncing works

- **Whole-library backups.** Each sync uploads a ZIP of the OpenLP data
  directory to a `OpenLP Cloud Sync` folder in your Google Drive, with a
  `cloudsync-meta.json` sidecar (timestamp, hostname, song hashes).
- **True three-way merge.** After every sync the plugin stores a snapshot of
  the song list. On the next sync it compares *base → local → remote*:
  - Songs added or edited on either side are merged.
  - A song missing from the remote backup counts as **deleted on remote**
    only if it was present in the *uploader's own* base snapshot. A machine
    that simply never had the song can't delete it by syncing.
  - Backups that predate base snapshots merge additively and never delete.
- **Last-write-wins for edits**, with the pre-sync state always kept as a
  local backup first.
- **Pruning.** The newest 10 backups are kept by default (configurable).

## Install

**Windows:** download `CloudSync-Setup-<version>.exe` from
[Releases](../../releases), double-click it, then restart OpenLP. No admin
rights needed. If OpenLP doesn't list the plugin, enable it under
*Settings → Plugins*.

**macOS / Linux:** download the release zip, extract it, and run
`./install.sh`.

Then open the Cloud Sync settings tab and sign in with Google.

## Google Cloud setup (for building your own copy)

The plugin authenticates with Google via an OAuth desktop client whose
credentials live in `plugin/cloudsync/client.json`. That file is
**gitignored** — create your own:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   project and an **OAuth client ID** of type *Desktop app*.
2. Copy `plugin/cloudsync/client.json.example` to
   `plugin/cloudsync/client.json` and fill in your client ID and secret.
3. Add the scopes the plugin requests (see
   `plugin/cloudsync/lib/providers.py`):
   - `https://www.googleapis.com/auth/drive.file` — read/write files the app
     created (its own backups).
   - `https://www.googleapis.com/auth/drive.readonly` — read the sync folder's
     other backups. This is a **restricted** scope: publishing the app beyond
     personal/testing use requires Google's restricted-scope verification,
     including an annual third-party security assessment (CASA).

While the app is in testing mode, each Google account must be added as a
test user in the Cloud Console, and sign-in tokens expire after 7 days.

## Building the installer

```bash
# Windows installer (NSIS; runs on Linux/macOS too)
cp -r plugin/cloudsync installer/cloudsync
cd installer && makensis cloudsync-installer.nsi
```

Pushing a tag like `v13.5` runs the [release workflow](.github/workflows/release.yml),
which builds the `.exe` and the portable zip and attaches both to the GitHub
release.

## Running the tests

```bash
cd /path/to/openlp-source   # the plugin mirrors openlp/plugins/cloudsync/
cp -r plugin/cloudsync openlp/plugins/
cp -r tests tests-openlp-cloudsync   # or point pytest at tests/
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -q
```

## Project layout

```
plugin/cloudsync/      the OpenLP plugin (drop into
                       <OpenLP data>/contrib/plugins/)
  lib/                 auth, backup/restore, 3-way song merge,
                       Google Drive provider, sync controller
  cloudsyncplugin.py   plugin bootstrap
  client.json.example  OAuth client template (real file is gitignored)
installer/             NSIS + Inno Setup scripts, PowerShell and shell
                       installers
tests/                 pytest suite
```

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE).
