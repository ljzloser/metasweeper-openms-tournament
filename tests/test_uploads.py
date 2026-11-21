import sqlite3
import time

import pytest

from openms_tournament.uploads import UploadQueue


def test_queue_deduplicates_per_account_and_survives_interruption(tmp_path):
    path = tmp_path / "uploads.sqlite3"
    queue = UploadQueue(path)
    assert queue.enqueue(7, b"replay", ("TOKEN",), time.time() + 60)
    assert not queue.enqueue(7, b"replay", ("TOKEN",), None)
    assert queue.enqueue(8, b"replay", ("TOKEN",), None)
    job = queue.next_job(7)
    queue.start(job)
    recovered = UploadQueue(path)
    assert recovered.next_job(7).raw_data == b"replay"
    assert recovered.next_job(8).user_id == 8


@pytest.mark.parametrize("result", ["duplicate", "succeeded"])
def test_terminal_upload_discards_payload_and_is_never_retried(tmp_path, result):
    queue = UploadQueue(tmp_path / "uploads.sqlite3")
    queue.enqueue(7, b"replay", ("TOKEN",), None)
    job = queue.next_job(7)
    queue.start(job)
    queue.finish(job, result, "done", video_id=123 if result == "succeeded" else None)
    queue.retry_failed(7)
    assert queue.next_job(7) is None
    assert not queue.enqueue(7, b"replay", ("TOKEN",), None)
    with sqlite3.connect(queue.path) as database:
        assert database.execute("SELECT raw_data FROM uploads").fetchone()[0] is None
    assert queue.records()[0]["status"] == result
    assert UploadQueue(queue.path).next_job(7) is None


def test_retries_are_bounded_and_manual_retry_only_affects_current_account(tmp_path):
    queue = UploadQueue(tmp_path / "uploads.sqlite3")
    queue.enqueue(7, b"replay", ("TOKEN",), None)
    now = time.time() + 100
    for attempt in range(3):
        job = queue.next_job(7, now)
        assert job.attempts == attempt
        queue.start(job)
        queue.fail(job, "timeout", retryable=True)
    assert queue.next_job(7, now) is None
    queue.retry_failed(8)
    assert queue.next_job(7, now) is None
    queue.retry_failed(7)
    assert queue.next_job(7, now).attempts == 0


def test_expired_pending_or_failed_replay_is_not_sent_even_after_manual_retry(tmp_path):
    queue = UploadQueue(tmp_path / "uploads.sqlite3")
    queue.enqueue(7, b"replay", ("TOKEN",), time.time() + 5)
    job = queue.next_job(7)
    queue.fail(job, "quota", retryable=False)
    queue.retry_failed(7)
    assert queue.next_job(7, time.time() + 6) is None
    assert queue.records() == []
    queue.retry_failed(7)
    assert queue.next_job(7) is None


def test_login_failure_keeps_bytes_without_using_up_retry_budget(tmp_path):
    queue = UploadQueue(tmp_path / "uploads.sqlite3")
    queue.enqueue(7, b"replay", ("TOKEN",), None)
    job = queue.next_job(7)
    queue.start(job)
    queue.fail(job, "login expired", retryable=False, needs_login=True)
    restored = queue.next_job(7)
    assert restored.raw_data == b"replay" and restored.attempts == 0


def test_rate_limit_waits_for_server_deadline_without_consuming_upload_retry_budget(tmp_path):
    queue = UploadQueue(tmp_path / 'uploads.sqlite3')
    now = time.time()
    queue.enqueue(7, b'replay', ('TOKEN',), None, (30,))
    job = queue.next_job(7)
    queue.start(job)
    queue.fail(job, 'rate limited', retryable=True, retry_after=600, rate_limited=True)
    assert queue.next_job(7, now + 599) is None
    restored = queue.next_job(7, now + 601)
    assert restored.attempts == 0 and restored.raw_data == b'replay'


def test_pending_uploads_prioritize_nearest_deadline(tmp_path):
    queue = UploadQueue(tmp_path / "uploads.sqlite3")
    now = time.time()
    queue.enqueue(7, b"later", ("LATER",), now + 120)
    queue.enqueue(7, b"sooner", ("SOON",), now + 60)
    assert queue.next_job(7).raw_data == b"sooner"


@pytest.mark.parametrize('status', ['pending', 'retry', 'failed', 'succeeded', 'duplicate'])
def test_scope_cleanup_removes_all_metadata_and_bytes_for_past_tournaments(tmp_path, status):
    queue = UploadQueue(tmp_path / 'uploads.sqlite3')
    queue.enqueue(7, b'old-replay', ('OLD',), None, (30,))
    job = queue.next_job(7)
    if status in ('succeeded', 'duplicate'):
        queue.finish(job, status, 'old metadata', video_id=123)
    elif status in ('retry', 'failed'):
        queue.fail(job, 'failure', retryable=status == 'retry')
    queue.enqueue(8, b'current-replay', ('NEW',), None, (31,))
    assert queue.retain_tournaments({31}) == 1
    assert [row['user_id'] for row in queue.records()] == [8]
    with sqlite3.connect(queue.path) as database:
        assert database.execute('SELECT digest FROM uploads WHERE user_id=7').fetchall() == []
    assert UploadQueue(queue.path).next_job(7) is None


def test_multi_tournament_replay_is_retained_until_last_related_tournament_disappears(tmp_path):
    queue = UploadQueue(tmp_path / 'uploads.sqlite3')
    queue.enqueue(7, b'replay', ('ONE', 'TWO'), None, (30, 31))
    assert queue.retain_tournaments({31}) == 0
    assert queue.next_job(7).raw_data == b'replay'
    assert queue.retain_tournaments(set()) == 1
    assert queue.records() == []


def test_deadline_cleanup_removes_success_and_other_accounts_even_without_login(tmp_path):
    queue = UploadQueue(tmp_path / 'uploads.sqlite3')
    deadline = time.time() + 10
    queue.enqueue(7, b'success', ('ONE',), deadline, (30,))
    queue.finish(queue.next_job(7), 'succeeded', 'old', video_id=123)
    queue.enqueue(8, b'failed', ('TWO',), deadline, (31,))
    queue.fail(queue.next_job(8), 'failure', retryable=False)
    assert queue.purge_expired(deadline + 1) == 2
    assert queue.records() == []


def test_scope_cleanup_releases_database_file_space(tmp_path):
    queue = UploadQueue(tmp_path / 'uploads.sqlite3')
    queue.enqueue(7, b'large-replay' * 100_000, ('OLD',), None, (30,))
    original_size = queue.path.stat().st_size
    queue.retain_tournaments(set())
    assert queue.records() == []
    assert queue.path.stat().st_size < original_size // 4


def test_upgrade_legacy_queue_recovers_current_matches_and_discards_unmatched_records(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as database:
        database.execute('''CREATE TABLE uploads (
            digest TEXT NOT NULL,user_id INTEGER NOT NULL,filename TEXT NOT NULL,tokens TEXT NOT NULL,
            deadline REAL,created REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,retry_after REAL NOT NULL DEFAULT 0,raw_data BLOB,
            video_id INTEGER,website_state TEXT NOT NULL DEFAULT '',message TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(digest,user_id))''')
        database.execute('''INSERT INTO uploads (digest,user_id,filename,tokens,created,raw_data)
            VALUES ('one',7,'one.evf','["CURRENT"]',0,X'01'),('two',7,'two.evf','["OLD"]',0,X'02')''')
    queue = UploadQueue(path)
    assert queue.retain_tournaments({30}, user_id=7, token_tournaments={'CURRENT': 30}) == 1
    assert queue.records()[0]['filename'] == 'one.evf'
    assert queue.retain_tournaments(set()) == 1


def test_late_upload_result_cannot_restore_a_deleted_record(tmp_path):
    queue = UploadQueue(tmp_path / 'uploads.sqlite3')
    queue.enqueue(7, b'replay', ('OLD',), None, (30,))
    job = queue.next_job(7)
    queue.start(job)
    queue.retain_tournaments(set())
    queue.finish(job, 'succeeded', 'late response', video_id=123)
    assert queue.records() == []
