"""Channel-independent usage ledger. Only explicitly registered Dev Wisp turns count.

Input includes cache reads/writes; reasoning is a subset of output. No message
content, credentials, or provider configuration is stored here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)
ENGINES = ('codex', 'claude-code', 'deepseek-harness')
COUNTERS = ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens', 'totalTokens')
_COLUMNS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', 'total_tokens')


def count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53 - 1 else None


def normalize_usage(raw: Any, *, fresh_input: bool, allow_zero: bool = False) -> dict[str, int | None]:
    raw = raw if isinstance(raw, dict) else {}
    aliases = {
        'inputTokens': ('inputTokens', 'input_tokens', 'prompt_tokens'),
        'outputTokens': ('outputTokens', 'output_tokens', 'completion_tokens'),
        'cacheReadTokens': ('cacheReadTokens', 'cachedInputTokens', 'cache_read_input_tokens', 'cacheReadInputTokens', 'prompt_cache_hit_tokens'),
        'cacheWriteTokens': ('cacheWriteTokens', 'cache_creation_input_tokens', 'cacheCreationInputTokens'),
        'reasoningTokens': ('reasoningTokens', 'reasoningOutputTokens', 'reasoning_output_tokens'),
        'totalTokens': ('totalTokens', 'total_tokens'),
    }
    values = {key: next((n for alias in names if (n := count(raw.get(alias))) is not None), None)
              for key, names in aliases.items()}
    for detail in ('input_tokens_details', 'prompt_tokens_details'):
        if isinstance(raw.get(detail), dict) and values['cacheReadTokens'] is None:
            values['cacheReadTokens'] = count(raw[detail].get('cached_tokens'))
    for detail in ('output_tokens_details', 'completion_tokens_details'):
        if isinstance(raw.get(detail), dict) and values['reasoningTokens'] is None:
            values['reasoningTokens'] = count(raw[detail].get('reasoning_tokens'))
    # Several compatible drivers synthesize an entirely zero-filled usage.
    # Keep those as unreported rather than claiming a successful measurement.
    if not allow_zero and not any(value for value in values.values() if value is not None):
        return dict.fromkeys(COUNTERS)
    if fresh_input and values['inputTokens'] is not None:
        values['inputTokens'] += (values['cacheReadTokens'] or 0) + (values['cacheWriteTokens'] or 0)
    if values['inputTokens'] is not None and values['outputTokens'] is not None:
        values['totalTokens'] = values['inputTokens'] + values['outputTokens']
    return values


async def track(store: LocalUsageStore | None, method: str, *args: Any, **kwargs: Any) -> Any:
    if store is None:
        return None
    try:
        changed = await asyncio.to_thread(getattr(store, method), *args, **kwargs)
    except (sqlite3.Error, OSError, ValueError):
        # Usage accounting must never break the actual conversation.
        logger.exception('Could not persist local chat usage (%s)', method)
        return None
    if changed and store.on_change is not None:
        try:
            for engine, thread_id in sorted(changed):
                await store.on_change(engine, thread_id)
        except Exception:
            logger.exception('Could not publish local chat usage (%s)', method)
    return changed


class LocalUsageStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.on_change: Callable[[str, str], Awaitable[None]] | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA foreign_keys=ON')
        with self._db:
            self._db.executescript('''
                CREATE TABLE IF NOT EXISTS usage_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS usage_turns (
                    engine TEXT NOT NULL, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    model TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER,
                    status TEXT NOT NULL DEFAULT 'inProgress',
                    PRIMARY KEY(engine, thread_id, turn_id)
                );
                CREATE INDEX IF NOT EXISTS usage_turns_time ON usage_turns(started_at, engine);
                CREATE INDEX IF NOT EXISTS usage_turns_thread ON usage_turns(thread_id, engine, started_at);
                CREATE TABLE IF NOT EXISTS usage_samples (
                    engine TEXT NOT NULL, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    sample_id TEXT NOT NULL, scope TEXT NOT NULL, model TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER,
                    PRIMARY KEY(engine, thread_id, turn_id, sample_id),
                    FOREIGN KEY(engine, thread_id, turn_id) REFERENCES usage_turns(engine, thread_id, turn_id)
                );
                CREATE TABLE IF NOT EXISTS usage_codex_snapshots (
                    thread_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
                );
            ''')
            self._db.execute("INSERT OR IGNORE INTO usage_meta VALUES ('enabled_at', ?)", (self._now(),))
            self._db.execute("INSERT OR IGNORE INTO usage_meta VALUES ('revision', 0)")
            # A previous process may have died without publishing completion.
            if self._db.execute("UPDATE usage_turns SET status='interrupted' WHERE status='inProgress'").rowcount:
                self._db.execute("UPDATE usage_meta SET value=value+1 WHERE key='revision'")
        if os.name != 'nt':
            os.chmod(path, 0o600)

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _changed(self, engine: str, thread_id: str) -> set[tuple[str, str]]:
        self._db.execute("UPDATE usage_meta SET value=value+1 WHERE key='revision'")
        return {(engine, thread_id)}

    def begin(self, engine: str, thread_id: str, turn_id: str, model: str | None = None) -> set[tuple[str, str]] | None:
        if engine not in ENGINES:
            raise ValueError('Unsupported usage engine')
        with self._lock, self._db:
            if self._db.execute('INSERT OR IGNORE INTO usage_turns(engine,thread_id,turn_id,model,started_at) VALUES (?,?,?,?,?)',
                                (engine, thread_id, turn_id, model or 'unknown', self._now())).rowcount:
                return self._changed(engine, thread_id)

    def finish(self, engine: str, thread_id: str, turn_id: str, status: str) -> set[tuple[str, str]] | None:
        with self._lock, self._db:
            if self._db.execute('UPDATE usage_turns SET finished_at=COALESCE(finished_at,?),status=? WHERE engine=? AND thread_id=? AND turn_id=? AND (status!=? OR finished_at IS NULL)',
                                (self._now(), status, engine, thread_id, turn_id, status)).rowcount:
                return self._changed(engine, thread_id)

    def interrupt(self, engine: str) -> set[tuple[str, str]] | None:
        with self._lock, self._db:
            threads = {row[0] for row in self._db.execute("SELECT thread_id FROM usage_turns WHERE engine=? AND status='inProgress'", (engine,))}
            self._db.execute("UPDATE usage_turns SET finished_at=COALESCE(finished_at,?),status='interrupted' WHERE engine=? AND status='inProgress'",
                             (self._now(), engine))
            if threads:
                self._db.execute("UPDATE usage_meta SET value=value+1 WHERE key='revision'")
                return {(engine, thread_id) for thread_id in threads}

    def record(self, engine: str, thread_id: str, turn_id: str, sample_id: str,
               model: str | None, usage: dict[str, int | None], *, scope: str = 'request') -> set[tuple[str, str]] | None:
        if not sample_id or scope not in ('request', 'summary'):
            return
        with self._lock, self._db:
            turn = self._db.execute('SELECT model FROM usage_turns WHERE engine=? AND thread_id=? AND turn_id=?',
                                    (engine, thread_id, turn_id)).fetchone()
            if turn is None:
                return  # A native/external turn is never implicitly imported.
            values = [count(usage.get(key)) for key in COUNTERS]
            old = self._db.execute('SELECT * FROM usage_samples WHERE engine=? AND thread_id=? AND turn_id=? AND sample_id=?',
                                   (engine, thread_id, turn_id, sample_id)).fetchone()
            if old:
                values = [max(a, b) if a is not None and b is not None else a if b is None else b
                          for a, b in zip(values, (old[key] for key in _COLUMNS))]
            if values[0] is not None and values[1] is not None:
                values[-1] = values[0] + values[1]
            resolved_model = model or (old['model'] if old else turn['model'])
            if resolved_model in ('default', ''):
                resolved_model = 'unknown'
            if old and old['model'] == resolved_model and values == [old[key] for key in _COLUMNS]:
                return
            self._db.execute('''INSERT INTO usage_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(engine,thread_id,turn_id,sample_id) DO UPDATE SET
                model=excluded.model,input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,
                cache_read_tokens=excluded.cache_read_tokens,cache_write_tokens=excluded.cache_write_tokens,
                reasoning_tokens=excluded.reasoning_tokens,total_tokens=excluded.total_tokens''',
                (engine, thread_id, turn_id, sample_id, scope, resolved_model, *values))
            return self._changed(engine, thread_id)

    def codex(self, thread_id: str, turn_id: str | None, token_usage: Any, *,
              owner: tuple[str, str] | None = None, model: str | None = None) -> set[tuple[str, str]] | None:
        if not isinstance(token_usage, dict):
            return
        total = normalize_usage(token_usage.get('total'), fresh_input=False, allow_zero=True)
        last = normalize_usage(token_usage.get('last'), fresh_input=False)
        snapshot = json.dumps({'total': total, 'last': last}, sort_keys=True)
        with self._lock, self._db:
            previous = self._db.execute('SELECT snapshot FROM usage_codex_snapshots WHERE thread_id=?', (thread_id,)).fetchone()
            if previous and previous['snapshot'] == snapshot:
                return
            self._db.execute('INSERT OR REPLACE INTO usage_codex_snapshots VALUES (?,?)', (thread_id, snapshot))
            if not turn_id:
                return  # A resumed thread's baseline, not new consumption.
            usage = last
            if not any(value is not None for value in last.values()):
                if previous is None:
                    return  # Cannot attribute a pre-existing cumulative total.
                old_total = json.loads(previous['snapshot'])['total']
                usage = {key: max(0, value - old_total[key])
                         if value is not None and old_total.get(key) is not None and value >= old_total[key]
                         else None for key, value in total.items()}
                if usage['inputTokens'] is not None and usage['outputTokens'] is not None:
                    usage['totalTokens'] = usage['inputTokens'] + usage['outputTokens']
            if not any(value for value in usage.values() if value is not None):
                return
            key = f'codex:{thread_id}:{turn_id}:' + hashlib.sha256(snapshot.encode()).hexdigest()
            target_thread, target_turn = owner or (thread_id, turn_id)
            return self.record('codex', target_thread, target_turn, key, model, usage)

    def query(self, *, since: int = 0, until: int | None = None, engine: str = '', model: str = '',
              thread_id: str = '', offset_minutes: int = 0, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        until = until if until is not None else self._now() + 1
        params = {'since': since, 'until': until, 'engine': engine, 'model': model, 'thread': thread_id,
                  'limit': limit, 'offset': offset, 'tz': f'{-offset_minutes:+d} minutes'}
        # Final CLI results and individual messages describe overlapping work.
        # Select one scope per turn, never add both. Prefer the complete result
        # when it covers at least as much reported work as individual messages.
        cte = '''WITH candidates AS (
            SELECT * FROM usage_turns WHERE started_at>=:since AND started_at<:until
                AND (:engine='' OR engine=:engine)
                AND (:thread='' OR thread_id=:thread)
        ), scores AS (
            SELECT s.engine,s.thread_id,s.turn_id,
                SUM(CASE WHEN scope='request' THEN COALESCE(total_tokens,COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) ELSE 0 END) request_tokens,
                SUM(CASE WHEN scope='summary' THEN COALESCE(total_tokens,COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) ELSE 0 END) summary_tokens,
                SUM(scope='summary' AND total_tokens IS NOT NULL) summaries
            FROM usage_samples s JOIN candidates t USING(engine,thread_id,turn_id)
            GROUP BY s.engine,s.thread_id,s.turn_id
        ), effective AS (
            SELECT s.* FROM usage_samples s JOIN scores USING(engine,thread_id,turn_id)
            WHERE s.scope=CASE WHEN summaries>0 AND summary_tokens>=request_tokens THEN 'summary' ELSE 'request' END
        ), samples AS (
            SELECT * FROM effective WHERE :model='' OR model=:model
        ), turns AS (
            SELECT t.*, GROUP_CONCAT(DISTINCT s.model) models,
                SUM(s.input_tokens) input_tokens,SUM(s.output_tokens) output_tokens,
                SUM(s.cache_read_tokens) cache_read_tokens,SUM(s.cache_write_tokens) cache_write_tokens,
                SUM(s.reasoning_tokens) reasoning_tokens,SUM(s.total_tokens) total_tokens,
                COUNT(s.sample_id) samples,
                SUM(CASE WHEN s.sample_id IS NOT NULL AND (s.input_tokens IS NULL OR s.output_tokens IS NULL) THEN 1 ELSE 0 END) partial
            FROM candidates t LEFT JOIN samples s USING(engine,thread_id,turn_id)
            WHERE :model='' OR s.sample_id IS NOT NULL OR (t.model=:model AND NOT EXISTS(
                SELECT 1 FROM effective e WHERE e.engine=t.engine AND e.thread_id=t.thread_id AND e.turn_id=t.turn_id))
            GROUP BY t.engine,t.thread_id,t.turn_id
        ) '''
        sums = ','.join(f'SUM({column}) AS {column}' for column in _COLUMNS)
        with self._lock:
            def rows(sql: str) -> list[dict[str, Any]]:
                return [dict(row) for row in self._db.execute(cte + sql, params)]

            totals = rows(f'''SELECT COUNT(*) turns,{sums},
                SUM(total_tokens IS NOT NULL) reported_turns,
                SUM(total_tokens IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND status!='inProgress') missing_turns,
                SUM((partial>0 OR total_tokens IS NULL) AND (input_tokens IS NOT NULL OR output_tokens IS NOT NULL OR total_tokens IS NOT NULL)) partial_turns,
                SUM(status='inProgress') active_turns FROM turns''')[0]
            engines = rows(f'SELECT engine,COUNT(*) turns,{sums} FROM turns GROUP BY engine ORDER BY SUM(total_tokens) DESC')
            models = rows(f'''SELECT s.model,{','.join('SUM(s.'+c+') AS '+c for c in _COLUMNS)},COUNT(DISTINCT s.engine||':'||s.thread_id||':'||s.turn_id) turns
                FROM samples s GROUP BY s.model ORDER BY SUM(s.total_tokens) DESC''')
            daily = rows(f'''SELECT strftime('%Y-%m-%d',started_at/1000,'unixepoch',:tz) day,
                COUNT(*) turns,{sums} FROM turns GROUP BY day ORDER BY day''')
            recent = rows('SELECT * FROM turns ORDER BY started_at DESC,engine,thread_id,turn_id LIMIT :limit OFFSET :offset')
            model_options = [row[0] for row in self._db.execute('SELECT DISTINCT model FROM usage_samples ORDER BY model')]
            enabled_at = self._db.execute("SELECT value FROM usage_meta WHERE key='enabled_at'").fetchone()[0]
            revision = self._db.execute("SELECT value FROM usage_meta WHERE key='revision'").fetchone()[0]

        def camel(row: dict[str, Any]) -> dict[str, Any]:
            return {key.split('_')[0] + ''.join(part.title() for part in key.split('_')[1:]): value
                    for key, value in row.items()}

        return {'enabledAt': enabled_at, 'revision': revision, 'threadId': thread_id or None, 'totals': camel(totals), 'engines': [camel(row) for row in engines],
                'models': [camel(row) for row in models], 'daily': [camel(row) for row in daily],
                'turns': [camel(row) for row in recent], 'modelOptions': model_options, 'offset': offset, 'limit': limit}


class ClaudeUsage:
    """Merge partial messages with final messages and per-result summaries."""
    def __init__(self, store: LocalUsageStore | None, thread_id: str, turn_id: str, model: str | None) -> None:
        self.store, self.thread_id, self.turn_id, self.model = store, thread_id, turn_id, model
        self.current: dict[str, tuple[str, str | None]] = {}
        self.result_index = 0

    async def observe(self, record: dict[str, Any]) -> None:
        if self.store is None:
            return
        parent = str(record.get('parent_tool_use_id') or 'root')
        kind = record.get('type')
        message = record.get('message')
        if kind == 'assistant' and isinstance(message, dict) and isinstance(message.get('id'), str):
            await self._record(message['id'], message.get('model'), message.get('usage'))
        elif kind == 'stream_event':
            event = record.get('event', {})
            if not isinstance(event, dict):
                return
            if event.get('type') == 'message_start':
                message = event.get('message', {})
                if isinstance(message, dict) and isinstance(message.get('id'), str):
                    self.current[parent] = (message['id'], message.get('model'))
                    await self._record(message['id'], message.get('model'), message.get('usage'))
            elif event.get('type') == 'message_delta' and parent in self.current:
                message_id, model = self.current[parent]
                await self._record(message_id, model, event.get('usage'))
        elif kind == 'result' and parent == 'root':
            # Native modelUsage is cumulative across all inputs accepted by
            # this CLI process, including subagents. Update one sample/model.
            # Main-loop `usage` is a per-result fallback on older hosts.
            self.result_index += 1
            key = str(record.get('uuid') or f'result-{self.result_index}')
            models = record.get('modelUsage')
            if isinstance(models, dict) and models:
                for model, raw in models.items():
                    await self._record(f'result:model:{model}', model, raw, scope='summary')
            elif isinstance(record.get('usage'), dict):
                await self._record(f'result:{key}', self.model, record['usage'], scope='summary')

    async def _record(self, key: str, model: str | None, raw: Any, *, scope: str = 'request') -> None:
        await track(self.store, 'record', 'claude-code', self.thread_id, self.turn_id,
                    key, model, normalize_usage(raw, fresh_input=True), scope=scope)
