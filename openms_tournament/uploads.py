"""Persistent replay outbox. Terminal results discard queued bytes, not saved game files."""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .analysis import LEVEL_NAMES, ScoreVideo


@dataclass(frozen=True)
class UploadJob:
    digest: str
    user_id: int
    filename: str
    tokens: tuple[str, ...]
    deadline: float | None
    attempts: int
    raw_data: bytes = field(repr=False)
    level: str | None = None
    timems: int | None = None
    bv: int | None = None


class UploadQueue:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            # Return freed pages to the filesystem instead of retaining old replay-sized files.
            if database.execute('PRAGMA auto_vacuum').fetchone()[0] != 1:
                database.execute('PRAGMA auto_vacuum=FULL')
                database.execute('VACUUM')
            database.execute("""CREATE TABLE IF NOT EXISTS uploads (
                digest TEXT NOT NULL, user_id INTEGER NOT NULL, filename TEXT NOT NULL,
                tokens TEXT NOT NULL, deadline REAL, created REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                retry_after REAL NOT NULL DEFAULT 0, raw_data BLOB, video_id INTEGER,
                website_state TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                tournament_ids TEXT NOT NULL DEFAULT '[]', PRIMARY KEY (digest, user_id))""")
            columns = {row['name'] for row in database.execute('PRAGMA table_info(uploads)')}
            if 'tournament_ids' not in columns:
                database.execute("ALTER TABLE uploads ADD COLUMN tournament_ids TEXT NOT NULL DEFAULT '[]'")
            for column, kind in (('level', 'TEXT'), ('timems', 'INTEGER'), ('bv', 'INTEGER'), ('clicks', 'INTEGER')):
                if column not in columns:
                    database.execute(f"ALTER TABLE uploads ADD COLUMN {column} {kind}")
            database.execute("""CREATE TABLE IF NOT EXISTS local_wins (
                digest TEXT NOT NULL, user_id INTEGER NOT NULL, tournament_ids TEXT NOT NULL, deadline REAL,
                level TEXT NOT NULL, timems INTEGER NOT NULL, bv INTEGER NOT NULL, clicks INTEGER,
                raw_data BLOB, video_id INTEGER, PRIMARY KEY (digest, user_id))""")
            database.execute("""INSERT OR IGNORE INTO local_wins
                (digest,user_id,tournament_ids,level,timems,bv,clicks,video_id)
                SELECT digest,user_id,tournament_ids,COALESCE(level,''),
                       CASE WHEN level IS NOT NULL AND bv IS NOT NULL THEN COALESCE(timems,0) ELSE 0 END,
                       COALESCE(bv,0),clicks,video_id
                FROM uploads WHERE tournament_ids<>'[]' AND (
                    (level IS NOT NULL AND timems>0 AND bv IS NOT NULL) OR (status='succeeded' AND video_id>0)
                )""")
            database.execute("UPDATE uploads SET status='pending', message='恢复中断的上传，等待确认' WHERE status='uploading'")
        self.purge_expired()

    @contextmanager
    def _connect(self):
        database = sqlite3.connect(self.path)
        database.row_factory = sqlite3.Row
        database.execute('PRAGMA secure_delete=ON')
        try:
            with database:
                yield database
        finally:
            database.close()

    def enqueue(self, user_id: int, raw_data: bytes, tokens: tuple[str, ...], deadline: float | None,
                tournament_ids: tuple[int, ...] = (), *, level: str | None = None, timems: int | None = None,
                bv: int | None = None, clicks: int | None = None) -> bool:
        digest = hashlib.sha256(raw_data).hexdigest()
        now = time.time()
        filename = datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%d-%H%M%S-') + digest[:12] + '.evf'
        with self._connect() as database:
            cursor = database.execute("""INSERT OR IGNORE INTO uploads
                (digest,user_id,filename,tokens,deadline,created,raw_data,tournament_ids,level,timems,bv,clicks)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (digest, user_id, filename, json.dumps(tokens), deadline, now, raw_data, json.dumps(tournament_ids),
                 level, timems, bv, clicks))
            return cursor.rowcount == 1

    def record_win(self, user_id: int, digest: str, tournament_ids: tuple[int, ...],
                   deadline: float | None, level: str, timems: int, bv: int, clicks: int,
                   raw_data: bytes | None) -> bool:
        with self._connect() as database:
            cursor = database.execute("""INSERT OR IGNORE INTO local_wins
                (digest,user_id,tournament_ids,deadline,level,timems,bv,clicks,raw_data,video_id)
                VALUES (?,?,?,?,?,?,?,?,?,(SELECT video_id FROM uploads WHERE digest=? AND user_id=?))""",
                (digest, user_id, json.dumps(tournament_ids), deadline, level, timems, bv, clicks, raw_data,
                 digest, user_id))
            return cursor.rowcount == 1

    def score_videos(self, user_id: int, tournament_id: int) -> tuple[ScoreVideo, ...]:
        with self._connect() as database:
            rows = database.execute("""SELECT rowid,digest,tournament_ids,level,timems,bv,clicks
                FROM local_wins WHERE user_id=? AND timems>0 AND (deadline IS NULL OR deadline>?)""",
                (user_id, time.time())).fetchall()
        return tuple(ScoreVideo(-row['rowid'], row['level'], '', 'local', row['timems'], row['bv'], row['clicks'],
                                row['digest']) for row in rows if tournament_id in json.loads(row['tournament_ids']))

    def restore_legacy_scores(self, user_id: int, tournament_id: int, videos: tuple[ScoreVideo, ...]):
        available = {video.id: video for video in videos if video.timems > 0 and video.level in LEVEL_NAMES}
        if not available:
            return
        with self._connect() as database:
            missing = database.execute("""SELECT rowid,video_id,tournament_ids FROM local_wins
                WHERE user_id=? AND timems=0 AND video_id IS NOT NULL AND (deadline IS NULL OR deadline>?)""",
                (user_id, time.time())).fetchall()
            for row in missing:
                video = available.get(row['video_id'])
                if video is not None and tournament_id in json.loads(row['tournament_ids']):
                    database.execute("""UPDATE local_wins SET level=?,timems=?,bv=?,clicks=?
                        WHERE rowid=? AND timems=0""", (video.level, video.timems, video.bv, video.clicks, row['rowid']))

    def local_replay(self, user_id: int, digest: str) -> tuple[bytes | None, int | None]:
        with self._connect() as database:
            row = database.execute("""SELECT COALESCE(local_wins.raw_data, uploads.raw_data), local_wins.video_id
                FROM local_wins LEFT JOIN uploads USING (user_id,digest)
                WHERE local_wins.user_id=? AND local_wins.digest=?
                  AND (local_wins.deadline IS NULL OR local_wins.deadline>?)""",
                (user_id, digest, time.time())).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def purge_expired(self, now: float | None = None) -> int:
        """Remove every expired task, including success/history and other accounts."""
        now = time.time() if now is None else now
        with self._connect() as database:
            database.execute("DELETE FROM local_wins WHERE deadline<=?", (now,))
            return database.execute("DELETE FROM uploads WHERE deadline<=? OR status='expired'", (now,)).rowcount

    def retain_tournaments(self, tournament_ids: set[int], *, user_id: int | None = None,
                           token_tournaments: dict[str, int] | None = None,
                           tournament_deadlines: dict[int, float | None] | None = None) -> int:
        """Called only with a successfully fetched authoritative tournament list."""
        removed = 0
        with self._connect() as database:
            rows = database.execute('SELECT digest,user_id,tournament_ids,tokens FROM uploads').fetchall()
            for row in rows:
                scopes = set(json.loads(row['tournament_ids']))
                # Upgrade records produced before tournament IDs were stored.
                if not scopes and row['user_id'] == user_id and token_tournaments is not None:
                    scopes = {token_tournaments[token] for token in json.loads(row['tokens'])
                              if token in token_tournaments}
                active = scopes.intersection(tournament_ids)
                if active:
                    database.execute('UPDATE uploads SET tournament_ids=? WHERE digest=? AND user_id=?',
                                     (json.dumps(sorted(active)), row['digest'], row['user_id']))
                else:
                    removed += database.execute('DELETE FROM uploads WHERE digest=? AND user_id=?',
                                                (row['digest'], row['user_id'])).rowcount
            deadlines = tournament_deadlines or {}
            for row in database.execute('SELECT rowid,tournament_ids,deadline FROM local_wins').fetchall():
                active = sorted(tournament_ids.intersection(json.loads(row['tournament_ids'])))
                if not active:
                    database.execute('DELETE FROM local_wins WHERE rowid=?', (row['rowid'],))
                    continue
                end_times = [deadlines.get(tournament_id) for tournament_id in active]
                deadline = max(end_times) if None not in end_times else row['deadline']
                database.execute('UPDATE local_wins SET tournament_ids=?, deadline=? WHERE rowid=?',
                                 (json.dumps(active), deadline, row['rowid']))
        return removed

    def discard(self, job: UploadJob):
        with self._connect() as database:
            database.execute('DELETE FROM uploads WHERE digest=? AND user_id=?', (job.digest, job.user_id))

    def next_job(self, user_id: int, now: float | None = None) -> UploadJob | None:
        now = time.time() if now is None else now
        self.purge_expired(now)
        with self._connect() as database:
            row = database.execute("""SELECT * FROM uploads WHERE user_id=? AND status IN ('pending','retry')
                AND retry_after<=? AND raw_data IS NOT NULL
                ORDER BY COALESCE(deadline, 1e30), created LIMIT 1""", (user_id, now)).fetchone()
        if row is None:
            return None
        return UploadJob(row['digest'], row['user_id'], row['filename'], tuple(json.loads(row['tokens'])),
                         row['deadline'], row['attempts'], row['raw_data'], row['level'], row['timems'], row['bv'])

    def start(self, job: UploadJob):
        with self._connect() as database:
            database.execute("UPDATE uploads SET status='uploading', attempts=attempts+1, message='正在上传' WHERE digest=? AND user_id=?",
                             (job.digest, job.user_id))

    def finish(self, job: UploadJob, status: str, message: str, *, video_id: int | None = None, state: str = ''):
        if status not in ('succeeded', 'duplicate'):
            raise ValueError('Invalid terminal upload status')
        with self._connect() as database:
            database.execute("""UPDATE uploads SET status=?, raw_data=NULL, message=?, video_id=?, website_state=?
                WHERE digest=? AND user_id=?""", (status, message, video_id, state, job.digest, job.user_id))
            database.execute("""UPDATE local_wins SET raw_data=NULL, video_id=COALESCE(?,video_id)
                WHERE digest=? AND user_id=?""", (video_id, job.digest, job.user_id))

    def fail(self, job: UploadJob, message: str, *, retryable: bool, needs_login: bool = False,
             retry_after: float | None = None, rate_limited: bool = False):
        attempts = job.attempts + 1
        automatic = needs_login or rate_limited or (retryable and attempts < 3)
        delay = 0 if needs_login else (10 if attempts == 1 else 30)
        if retry_after is not None:
            delay = max(1, retry_after)
        with self._connect() as database:
            database.execute("UPDATE uploads SET status=?, message=?, retry_after=?, attempts=? WHERE digest=? AND user_id=?",
                             ('retry' if automatic else 'failed', message, time.time() + delay,
                              job.attempts if needs_login or rate_limited else attempts, job.digest, job.user_id))

    def retry_failed(self, user_id: int):
        with self._connect() as database:
            database.execute("""UPDATE uploads SET status='pending', attempts=0, retry_after=0, message='等待重试'
                WHERE user_id=? AND status='failed' AND raw_data IS NOT NULL""", (user_id,))

    def records(self) -> list[dict]:
        with self._connect() as database:
            # Never load replay bytes just to refresh the history UI.
            return [dict(row) for row in database.execute("""SELECT created,user_id,filename,status,video_id,
                website_state,message FROM uploads ORDER BY created DESC, rowid DESC""")]
