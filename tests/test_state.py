"""
Tests for the Cloud Sync plugin's persistent sync-state bookkeeping.

The state module records what was last uploaded or seen remotely so the
sync controller can decide whether the cloud copy is newer.  State is kept
under ``<data_dir>/cloudsync/state.json``.
"""

import json
from pathlib import Path

from openlp.plugins.cloudsync.lib import state
from openlp.plugins.cloudsync.lib.providers import RemoteBackup

EXPECTED_KEYS = {
    'local_fingerprint',
    'local_synced_utc',
    'remote_name',
    'remote_hostname',
    'remote_created_utc',
    'remote_sha256',
    'applied_remote_sha256',
}


def state_path(data_dir):
    return Path(data_dir) / 'cloudsync' / 'state.json'


def test_load_state_empty(tmp_path):
    result = state.load_state(tmp_path)
    assert set(result) == EXPECTED_KEYS
    assert all(value is None for value in result.values())


def test_load_state_ignores_unknown_keys(tmp_path):
    state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state_path(tmp_path).write_text(json.dumps({'local_fingerprint': 'abc', 'bogus': 1}), encoding='utf-8')
    result = state.load_state(tmp_path)
    assert set(result) == EXPECTED_KEYS
    assert result['local_fingerprint'] == 'abc'


def test_load_state_corrupt_json_returns_defaults(tmp_path):
    section = tmp_path / 'cloudsync'
    section.mkdir(parents=True, exist_ok=True)
    (section / 'state.json').write_text('{not valid json', encoding='utf-8')
    result = state.load_state(tmp_path)
    assert set(result) == EXPECTED_KEYS
    assert all(value is None for value in result.values())


def test_save_state_merges_and_round_trips(tmp_path):
    state.save_state(tmp_path, local_fingerprint='fp1', remote_name='archive.zip')
    state.save_state(tmp_path, remote_hostname='workstation')
    result = state.load_state(tmp_path)
    assert result['local_fingerprint'] == 'fp1'
    assert result['remote_name'] == 'archive.zip'
    assert result['remote_hostname'] == 'workstation'
    assert result['local_synced_utc'] is None
    # File really was written to disk.
    stored = json.loads(state_path(tmp_path).read_text(encoding='utf-8'))
    assert stored['remote_name'] == 'archive.zip'


def test_record_upload(tmp_path):
    metadata = {
        'name': 'openlp-library-workstation-2026.zip',
        'hostname': 'workstation',
        'created_utc': '2026-09-28T10:00:00Z',
        'sha256': 'deadbeef',
    }
    state.record_upload(tmp_path, 'fingerprint-1', metadata)
    result = state.load_state(tmp_path)
    assert result['local_fingerprint'] == 'fingerprint-1'
    assert result['local_synced_utc'] is not None
    assert result['local_synced_utc'].endswith('Z')
    assert result['remote_name'] == metadata['name']
    assert result['remote_hostname'] == metadata['hostname']
    assert result['remote_created_utc'] == metadata['created_utc']
    assert result['remote_sha256'] == metadata['sha256']


def test_record_remote_seen_leaves_local_state(tmp_path):
    state.save_state(tmp_path, local_fingerprint='local-fp', local_synced_utc='2026-01-01T00:00:00Z')
    backup = RemoteBackup(
        remote_id='id-1',
        name='openlp-library-server-2026.zip',
        hostname='server',
        created_utc='2026-09-28T12:00:00Z',
        sha256='cafef00d',
        size_bytes=123,
    )
    state.record_remote_seen(tmp_path, backup)
    result = state.load_state(tmp_path)
    # Local upload state untouched.
    assert result['local_fingerprint'] == 'local-fp'
    assert result['local_synced_utc'] == '2026-01-01T00:00:00Z'
    # Remote fields updated.
    assert result['remote_name'] == backup.name
    assert result['remote_hostname'] == backup.hostname
    assert result['remote_created_utc'] == backup.created_utc
    assert result['remote_sha256'] == backup.sha256


def test_state_file_lives_under_plugin_section_dir(tmp_path):
    state.save_state(tmp_path, local_fingerprint='fp')
    assert state_path(tmp_path).is_file()
    assert (tmp_path / 'state.json').exists() is False
