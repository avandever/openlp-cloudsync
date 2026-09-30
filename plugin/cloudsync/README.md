# Cloud Sync Plugin

Automatically synchronises your OpenLP library (songs, bibles, media library —
everything in the OpenLP data directory) with cloud storage in the background.

## How it works

- **On startup**: OpenLP compares your local library against the newest backup
  in cloud storage. If the cloud copy is newer, it is downloaded and applied at
  the next startup; if your local library changed, it is uploaded.
- **When a song is created or updated**: after a short wait (default 60
  seconds), the library is re-uploaded automatically.
- **Manually**: **Tools → Sync Library Now**, or the **Sync Now** button in
  the plugin's settings tab.

Backups are whole-library ZIP archives with SQLite-safe snapshots of the
databases. The last 10 backups are kept by default (configurable). If both
sides changed since the last sync, the newer one wins (last-write-wins);
a local backup is always kept before a cloud copy is applied.

## Installation

```bash
pip install "openlp[cloudsync]"
```

This pulls in the Google Drive client libraries
(`google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`).

## Google Drive setup

The plugin ships with its own OAuth client built in, so there is nothing
to configure on the Google side. Your credentials never leave your
machine.

1. In OpenLP, open **Settings → Cloud Sync**.
2. Click **Connect** — a browser window will open asking you to sign in
   and grant the requested access. The token is stored in your OpenLP data
   directory (`cloudsync/token.json`) and is never uploaded.

Scopes requested by the plugin: `drive.file` (create and manage files
the app itself uploaded) and `drive.metadata.readonly` (read metadata of
files in the sync folder). These are the least-privilege scopes the
plugin needs.

If you already use OpenLP Vault, you can share the same Drive folder —
the plugin only recognises its own backup files, so they will not
interfere with each other.

### Sharing one folder between machines or people

Two computers (or two people) can sync to the same Drive folder:

1. On the first machine, let the plugin create the folder normally
   (Settings → Cloud Sync → **Browse...**, pick or create it).
2. In Google Drive, share that folder with the other Google account
   (Editor access).
3. On the second machine, open **Browse...** — the folder appears under
   **Shared with me**. Pick it.

Only folders the plugin itself created are visible to it (that is what the
`drive.file` scope allows), so the folder must originate from the plugin —
a folder someone made by hand in the Drive website cannot be used, even if
shared.

## Settings

| Setting | Default | Description |
|---|---|---|
| Enable cloud sync | off | Master switch |
| Sync when OpenLP starts | on | Check/upload on startup |
| Sync after a song is created or updated | on | Debounced auto-upload |
| Wait before syncing after a song change | 60 s | Debounce window |
| Drive folder | `OpenLP` | Folder in Drive holding the backups |
| Keep this many backups | 10 | Older backups are pruned |

## How syncing works

- **Upload:** when your library changes (startup check, manual Sync now, or
  a debounced auto-upload after a song edit), the whole data directory is
  archived and uploaded to the Drive folder.
- **Download:** when the cloud has a newer backup and your library is
  unchanged, it is downloaded and applied **live** — no restart needed.
  Its songs are merged into your running library and the song list
  refreshes in place; its other files are copied over your data directory.
- **Conflict (both sides changed):** songs are merged song-by-song.  New
  songs on either side are kept; edits to the same song resolve by
  `last_modified` (newer wins); **deletions propagate** — a song deleted on
  one side is deleted on the other, unless the other side edited it (the
  edit wins, so no work is silently lost).  The merged library is uploaded
  so all machines converge.
- Sqlite databases other than the songs database (e.g. bibles) are not
  replaced while OpenLP is running, since they may be open in another
  plugin.

## Notes

- Sync state (local fingerprint, last sync time) is stored in your OpenLP
  data directory under `cloudsync/state.json`.
- After every successful sync the plugin snapshots your songs database as a
  merge base (`cloudsync/base_songs.sqlite`, excluded from uploads), so the
  next conflict can tell "deleted on one side" from "added on the other".
- The sync state records both the newest remote backup *seen* and the one
  actually *applied* to your library.  If a download was ever recorded as
  seen but never applied (as v12's staged downloads were), the next sync
  downloads it again instead of reporting "in sync" forever.
