import { WEBUI_API_BASE_URL } from '$lib/constants';

export type QueueStatus =
	| 'pending'
	| 'running'
	| 'needs_approval'
	| 'paused'
	| 'done'
	| 'failed'
	| 'cancelled';

export type LocalAgentOptions = {
	enabled: boolean;
	cwd?: string;
	approval?: 'ask' | 'edits' | 'auto';
};

export type QueueItemOptions = {
	features?: Record<string, boolean>;
	tool_approval_mode?: 'ask' | 'auto' | 'full';
	local_agent?: LocalAgentOptions;
	runtime?: { user_message_id?: string; assistant_message_id?: string };
	trigger?: Record<string, unknown>;
};

export type QueueItem = {
	id: string;
	user_id: string;
	chat_id: string | null;
	title: string | null;
	prompt: string;
	model_id: string | null;
	kind: 'message' | 'resume';
	priority: number;
	status: QueueStatus;
	attempts: number;
	next_attempt_at: number | null;
	last_error: string | null;
	result_message_id: string | null;
	options: QueueItemOptions | null;
	created_at: number;
	updated_at: number;
	started_at: number | null;
	finished_at: number | null;
};

export type QueueSettings = {
	auto_resume: boolean;
	resume_message: string;
	notify_limit_reached: boolean;
	notify_item_done: boolean;
	notify_queue_done: boolean;
	notify_needs_approval: boolean;
	notify_item_failed: boolean;
};

export type QueueState = {
	paused: boolean;
	limit_until: number | null;
	limit_kind: string | null;
	limit_message: string | null;
	batch_active: boolean;
	running: boolean;
	settings: QueueSettings;
};

export type QueueSnapshot = { items: QueueItem[]; state: QueueState; now: number };

export type QueueItemForm = {
	prompt: string;
	title?: string;
	chat_id?: string | null;
	model_id?: string | null;
	priority?: number;
	options?: QueueItemOptions | null;
};

const request = async <T>(token: string, path: string, init: RequestInit = {}): Promise<T> => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/queue${path}`, {
		...init,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`,
			...(init.headers ?? {})
		}
	});
	if (!res.ok) {
		let detail: any = null;
		try {
			detail = await res.json();
		} catch {
			// not JSON
		}
		throw detail?.detail ?? detail ?? res.statusText;
	}
	return res.json();
};

const json = (method: string, body?: unknown): RequestInit => ({
	method,
	body: body === undefined ? undefined : JSON.stringify(body)
});

export const getQueue = (token: string) => request<QueueSnapshot>(token, '/');

export const createQueueItem = (token: string, form: QueueItemForm) =>
	request<QueueItem>(token, '/items', json('POST', form));

export const updateQueueItem = (
	token: string,
	id: string,
	form: Partial<QueueItemForm> & { status?: QueueStatus }
) => request<QueueItem>(token, `/items/${id}`, json('PATCH', form));

export const deleteQueueItem = (token: string, id: string) =>
	request<{ status: boolean }>(token, `/items/${id}`, json('DELETE'));

export const reorderQueue = (token: string, ids: string[]) =>
	request<{ status: boolean }>(token, '/items/reorder', json('POST', { ids }));

export const runQueueItemNow = (token: string, id: string) =>
	request<QueueItem>(token, `/items/${id}/run-now`, json('POST'));

export const clearFinishedQueueItems = (token: string) =>
	request<{ removed: number }>(token, '/clear-finished', json('POST'));

export const pauseQueue = (token: string) => request<QueueState>(token, '/pause', json('POST'));
export const resumeQueue = (token: string) => request<QueueState>(token, '/resume', json('POST'));
export const clearQueueLimit = (token: string) =>
	request<QueueState>(token, '/limit/clear', json('POST'));

export const updateQueueSettings = (token: string, settings: Partial<QueueSettings>) =>
	request<QueueState>(token, '/settings', json('PUT', settings));

export const armChatResume = (token: string, chatId: string, messageId?: string) =>
	request<{
		item: QueueItem;
		limit: { kind: string; reset_at: number | null; message: string } | null;
		state: QueueState;
	}>(token, '/resume-chat', json('POST', { chat_id: chatId, message_id: messageId }));
