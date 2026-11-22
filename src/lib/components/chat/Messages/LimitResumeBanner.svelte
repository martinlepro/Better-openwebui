<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { goto } from '$app/navigation';

	import { config } from '$lib/stores';
	import { armChatResume, getQueue } from '$lib/apis/chat-queue';
	import { formatClock, formatDuration, looksLikeLimitError } from '$lib/utils/queueEvents';

	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n: any = getContext('i18n');

	/** The failed reply's error (string or object). */
	export let error: unknown = null;
	export let chatId = '';
	export let messageId = '';

	let armed = false;
	let resumeAt: number | null = null;
	let busy = false;
	let now = Date.now() / 1000;
	let clockOffset = 0;
	let timer: ReturnType<typeof setInterval>;

	$: visible =
		!!($config?.features as any)?.enable_chat_queue && !!chatId && looksLikeLimitError(error);
	$: remaining = resumeAt ? Math.max(0, resumeAt - (now + clockOffset)) : 0;

	// Already armed (e.g. by the auto-resume setting, or earlier)?
	onMount(async () => {
		if (!visible) return;
		timer = setInterval(() => (now = Date.now() / 1000), 1000);
		try {
			const snapshot = await getQueue(localStorage.token);
			clockOffset = snapshot.now - Date.now() / 1000;
			armed = snapshot.items.some(
				(i) =>
					i.chat_id === chatId &&
					i.kind === 'resume' &&
					['pending', 'running', 'paused'].includes(i.status)
			);
			if (armed && snapshot.state.limit_until) resumeAt = snapshot.state.limit_until;
		} catch {
			// stay silent: the button still works
		}
	});
	onDestroy(() => clearInterval(timer));

	const arm = async () => {
		busy = true;
		try {
			const res = await armChatResume(localStorage.token, chatId, messageId);
			armed = true;
			resumeAt = res.state.limit_until;
			toast.success(
				resumeAt
					? $i18n.t('Resume scheduled at {{time}}.', { time: formatClock(resumeAt) })
					: $i18n.t('Resume scheduled.')
			);
		} catch (e) {
			toast.error(`${e}`);
		} finally {
			busy = false;
		}
	};
</script>

{#if visible}
	<div
		class="mt-2 flex flex-wrap items-center gap-2 rounded-2xl border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-[0.8125rem] text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100"
		role="status"
	>
		<div class="min-w-0 flex-1">
			{#if armed}
				<span class="font-medium">{$i18n.t('Resume scheduled')}</span>
				{#if resumeAt}
					— {$i18n.t('a message will be sent at {{time}} (in {{duration}}).', {
						time: formatClock(resumeAt),
						duration: formatDuration(remaining)
					})}
				{:else}
					— {$i18n.t('a message will be sent as soon as the limit is over.')}
				{/if}
			{:else}
				{$i18n.t('Limit reached. Send a message automatically as soon as it is over?')}
			{/if}
		</div>
		{#if armed}
			<button
				type="button"
				class="rounded-full border border-amber-300 px-3 py-1 text-xs hover:bg-white/60 dark:border-amber-500/40"
				on:click={() => goto('/queue')}>{$i18n.t('Open the queue')}</button
			>
		{:else}
			<button
				type="button"
				class="inline-flex items-center gap-1.5 rounded-full bg-amber-600 px-3.5 py-1 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50"
				disabled={busy}
				on:click={arm}
			>
				{#if busy}<Spinner className="size-3" />{/if}
				{$i18n.t('Resume automatically')}
			</button>
		{/if}
	</div>
{/if}
