import { describe, expect, it } from 'vitest';
import {
	describeQueueEvent,
	formatClock,
	formatDuration,
	isQueueEvent,
	isSilentQueueEvent,
	looksLikeLimitError
} from './queueEvents';

// Minimal i18n: returns the key with {{vars}} substituted, like i18next does for missing translations.
const t = (key: string, options: Record<string, unknown> = {}) =>
	key.replace(/\{\{(\w+)\}\}/g, (_, name) => String(options[name] ?? ''));

const NOW = new Date(2026, 10, 21, 10, 0, 0); // local time, 21 Nov 2026 10:00

describe('formatDuration', () => {
	it('formats hours, minutes and seconds', () => {
		expect(formatDuration(11520)).toBe('3h 12m');
		expect(formatDuration(3600)).toBe('1h 00m');
		expect(formatDuration(2700)).toBe('45m');
		expect(formatDuration(30)).toBe('30s');
		expect(formatDuration(0)).toBe('0s');
	});
	it('never goes negative or NaN', () => {
		expect(formatDuration(-5)).toBe('0s');
		expect(formatDuration(NaN)).toBe('0s');
	});
});

describe('formatClock', () => {
	it('shows only the time for today', () => {
		const ts = new Date(2026, 10, 21, 15, 42).getTime() / 1000;
		expect(formatClock(ts, NOW)).toBe('15:42');
	});
	it('adds the date for another day', () => {
		const ts = new Date(2026, 10, 22, 3, 5).getTime() / 1000;
		expect(formatClock(ts, NOW)).toBe('22/11 03:05');
	});
	it('handles missing values', () => {
		expect(formatClock(null)).toBe('?');
		expect(formatClock(undefined)).toBe('?');
	});
});

describe('event helpers', () => {
	it('recognises queue events', () => {
		expect(isQueueEvent('queue:item_done')).toBe(true);
		expect(isQueueEvent('chat:completion')).toBe(false);
		expect(isQueueEvent(undefined)).toBe(false);
		expect(isSilentQueueEvent('queue:update')).toBe(true);
		expect(isSilentQueueEvent('queue:item_done')).toBe(false);
	});
});

describe('describeQueueEvent', () => {
	it('limit reached: shows when it resumes and how many are waiting', () => {
		const reset = new Date(2026, 10, 21, 15, 42).getTime() / 1000;
		const n = describeQueueEvent(
			'queue:limit_reached',
			{ reset_at: reset, wait_seconds: 20520, known_reset: true, pending: 3 },
			t,
			NOW
		)!;
		expect(n.title).toBe('Limit reached');
		expect(n.body).toBe('Resuming automatically at 15:42 (in 5h 42m). 3 waiting.');
		expect(n.level).toBe('warning');
		expect(n.path).toBe('/queue');
		expect(n.sound && n.system).toBe(true);
	});

	it('limit reached with unknown reset says so', () => {
		const n = describeQueueEvent(
			'queue:limit_reached',
			{ reset_at: NOW.getTime() / 1000 + 60, wait_seconds: 60, known_reset: false, pending: 0 },
			t,
			NOW
		)!;
		expect(n.body).toContain('Reset time unknown');
		expect(n.body).not.toContain('waiting');
	});

	it('item done: preview and changed files, links to the chat', () => {
		const n = describeQueueEvent(
			'queue:item_done',
			{ title: 'Parser', preview: 'I refactored   the\nparser.', files: ['a.py', 'b.py'], chat_id: 'abc 1' },
			t
		)!;
		expect(n.title).toBe('Conversation finished');
		expect(n.body).toBe('Parser\nI refactored the parser.\n2 file(s) changed');
		expect(n.path).toBe('/c/abc%201');
		expect(n.level).toBe('success');
	});

	it('item done without chat id falls back to the queue page', () => {
		expect(describeQueueEvent('queue:item_done', { title: 'x' }, t)!.path).toBe('/queue');
	});

	it('queue done: success vs partial failure', () => {
		const ok = describeQueueEvent('queue:queue_done', { done: 4, failed: 0 }, t)!;
		expect(ok.body).toBe('4 conversation(s) finished.');
		expect(ok.level).toBe('success');
		const partial = describeQueueEvent('queue:queue_done', { done: 3, failed: 1 }, t)!;
		expect(partial.body).toBe('3 finished, 1 failed.');
		expect(partial.level).toBe('warning');
	});

	it('approval and failure link to the right place', () => {
		const a = describeQueueEvent('queue:needs_approval', { title: 'Cleanup', chat_id: 'c1' }, t)!;
		expect(a.body).toBe('"Cleanup" needs your approval to run an action.');
		expect(a.path).toBe('/c/c1');
		const f = describeQueueEvent('queue:item_failed', { title: 'Boom', error: 'Invalid API key' }, t)!;
		expect(f.body).toBe('"Boom" failed: Invalid API key');
		expect(f.level).toBe('error');
	});

	it('limit cleared is quiet', () => {
		const n = describeQueueEvent('queue:limit_cleared', {}, t)!;
		expect(n.sound).toBe(false);
		expect(n.system).toBe(false);
	});

	it('unknown and silent events produce nothing', () => {
		expect(describeQueueEvent('queue:update', {}, t)).toBeNull();
		expect(describeQueueEvent('queue:nope', {}, t)).toBeNull();
	});

	it('tolerates missing data', () => {
		expect(describeQueueEvent('queue:item_done', null, t)).not.toBeNull();
		expect(describeQueueEvent('queue:limit_reached', undefined, t)).not.toBeNull();
	});
});

describe('looksLikeLimitError', () => {
	it('detects provider limit messages', () => {
		for (const text of [
			'rate_limit_error: This request would exceed the rate limit',
			'429 Too Many Requests',
			'Claude usage limit reached. Your limit will reset at 5pm',
			"You've hit your limit · resets 9am",
			'Your credit balance is too low to access the Anthropic API',
			'overloaded_error',
			'You have reached your specified API usage limits. You will regain access on 2026-12-01'
		]) {
			expect(looksLikeLimitError(text), text).toBe(true);
		}
	});
	it('handles object errors', () => {
		expect(looksLikeLimitError({ error: { type: 'rate_limit_error' } })).toBe(true);
	});
	it('ignores unrelated errors', () => {
		for (const text of [
			'Invalid API key',
			'maximum context length is 200000 tokens',
			'prompt is too long: 250000 tokens',
			'Model not found',
			''
		]) {
			expect(looksLikeLimitError(text), text).toBe(false);
		}
		expect(looksLikeLimitError(null)).toBe(false);
	});
});
