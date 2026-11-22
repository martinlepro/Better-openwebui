/**
 * Turns `queue:*` socket events into notifications (toast / browser notification / sound).
 * Pure functions only: no stores, no DOM. The root layout does the displaying.
 */

export type Translate = (key: string, options?: Record<string, unknown>) => string;

export type QueueEventData = {
	event?: string;
	timestamp?: number;
	title?: string;
	preview?: string;
	files?: string[];
	error?: string;
	kind?: string;
	reset_at?: number | null;
	wait_seconds?: number;
	known_reset?: boolean;
	pending?: number;
	done?: number;
	failed?: number;
	chat_id?: string | null;
	item_id?: string | null;
};

export type QueueNotice = {
	title: string;
	body: string;
	/** Where clicking the notification goes. */
	path: string;
	level: 'info' | 'success' | 'warning' | 'error';
	/** Play the notification sound. */
	sound: boolean;
	/** Also raise an operating-system notification when the tab is in the background. */
	system: boolean;
	/** Toast lifetime in ms. */
	duration: number;
};

export const QUEUE_EVENT_PREFIX = 'queue:';

export const isQueueEvent = (type: unknown): type is string =>
	typeof type === 'string' && type.startsWith(QUEUE_EVENT_PREFIX);

/** Events that only mean "reload the list". */
export const isSilentQueueEvent = (type: string): boolean => type === 'queue:update';

/** 11520 -> "3h 12m", 2700 -> "45m", 30 -> "30s" */
export const formatDuration = (seconds: number): string => {
	const total = Math.max(0, Math.floor(seconds || 0));
	const hours = Math.floor(total / 3600);
	const minutes = Math.floor((total % 3600) / 60);
	const secs = total % 60;
	if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
	if (minutes > 0) return `${minutes}m`;
	return `${secs}s`;
};

/** "15:42" if the time is today, otherwise "21/11 15:42". `now` is injectable for tests. */
export const formatClock = (epochSeconds: number | null | undefined, now = new Date()): string => {
	if (!epochSeconds) return '?';
	const date = new Date(epochSeconds * 1000);
	const hhmm = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
	const sameDay =
		date.getFullYear() === now.getFullYear() &&
		date.getMonth() === now.getMonth() &&
		date.getDate() === now.getDate();
	if (sameDay) return hhmm;
	return `${String(date.getDate()).padStart(2, '0')}/${String(date.getMonth() + 1).padStart(2, '0')} ${hhmm}`;
};

const clip = (text: string, size: number): string => {
	const value = (text ?? '').replace(/\s+/g, ' ').trim();
	return value.length <= size ? value : `${value.slice(0, size - 1).trimEnd()}…`;
};

const chatPath = (data: QueueEventData): string =>
	data.chat_id ? `/c/${encodeURIComponent(data.chat_id)}` : '/queue';

export const describeQueueEvent = (
	type: string,
	data: QueueEventData | null | undefined,
	t: Translate,
	now = new Date()
): QueueNotice | null => {
	const d: QueueEventData = data ?? {};

	switch (type) {
		case 'queue:limit_reached': {
			const waiting = d.pending ?? 0;
			const duration = formatDuration(d.wait_seconds ?? 0);
			const time = formatClock(d.reset_at, now);
			return {
				title: t('Limit reached'),
				body:
					(d.known_reset === false
						? t('Reset time unknown, trying again at {{time}} (in {{duration}}).', {
								time,
								duration
							})
						: t('Resuming automatically at {{time}} (in {{duration}}).', { time, duration })) +
					(waiting > 0 ? ` ${t('{{count}} waiting.', { count: waiting })}` : ''),
				path: '/queue',
				level: 'warning',
				sound: true,
				system: true,
				duration: 15000
			};
		}

		case 'queue:item_done': {
			const files = d.files?.length ?? 0;
			const preview = clip(d.preview ?? '', 200);
			const lines = [
				d.title || t('Conversation'),
				preview,
				files > 0 ? t('{{count}} file(s) changed', { count: files }) : ''
			].filter(Boolean);
			return {
				title: t('Conversation finished'),
				body: lines.join('\n'),
				path: chatPath(d),
				level: 'success',
				sound: true,
				system: true,
				duration: 12000
			};
		}

		case 'queue:queue_done':
			return {
				title: t('Queue finished'),
				body:
					(d.failed ?? 0) > 0
						? t('{{done}} finished, {{failed}} failed.', { done: d.done ?? 0, failed: d.failed })
						: t('{{count}} conversation(s) finished.', { count: d.done ?? 0 }),
				path: '/queue',
				level: (d.failed ?? 0) > 0 ? 'warning' : 'success',
				sound: true,
				system: true,
				duration: 15000
			};

		case 'queue:needs_approval':
			return {
				title: t('Approval required'),
				body: t('"{{title}}" needs your approval to run an action.', {
					title: d.title || t('Conversation')
				}),
				path: chatPath(d),
				level: 'warning',
				sound: true,
				system: true,
				duration: 20000
			};

		case 'queue:item_failed':
			return {
				title: t('Conversation failed'),
				body: t('"{{title}}" failed: {{error}}', {
					title: d.title || t('Conversation'),
					error: clip(d.error ?? '', 160)
				}),
				path: '/queue',
				level: 'error',
				sound: true,
				system: true,
				duration: 15000
			};

		case 'queue:limit_cleared':
			return {
				title: t('Limit is over'),
				body: t('Resuming the queue.'),
				path: '/queue',
				level: 'info',
				sound: false,
				system: false,
				duration: 5000
			};

		default:
			return null;
	}
};

/** Mirrors backend/open_webui/utils/usage_limit.py closely enough to decide whether to offer the button. */
const LIMIT_PATTERNS = [
	/rate[_ -]?limit/i,
	/too many requests/i,
	/usage limit/i,
	/limit (?:was |has been )?(?:reached|exceeded|hit)/i,
	/(?:reached|hit|exceeded) (?:your|the) (?:[\w-]+ ){0,3}limit/i,
	/(?:you've|you have) (?:reached|hit|used).{0,40}(?:limit|quota|cap)/i,
	/quota (?:exceeded|exhausted|reached)/i,
	/exceeded your current quota/i,
	/regain access/i,
	/overloaded/i,
	/credit balance is too low/i,
	/\b429\b/,
	/\b529\b/
];
const NOT_A_LIMIT =
	/context[ _-]?(?:length|window)|maximum context|prompt is too long|input is too long/i;

export const looksLikeLimitError = (error: unknown): boolean => {
	let text = '';
	if (typeof error === 'string') text = error;
	else if (error && typeof error === 'object') {
		try {
			text = JSON.stringify(error);
		} catch {
			text = String(error);
		}
	}
	if (!text || NOT_A_LIMIT.test(text)) return false;
	return LIMIT_PATTERNS.some((pattern) => pattern.test(text));
};
