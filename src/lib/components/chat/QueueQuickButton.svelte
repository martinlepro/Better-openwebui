<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { goto } from '$app/navigation';

	import { chatQueueState, chatQueueTick, config } from '$lib/stores';
	import { clearQueueLimit, getQueue, updateQueueSettings, type QueueItem } from '$lib/apis/chat-queue';
	import { formatClock, formatDuration } from '$lib/utils/queueEvents';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import QueueList from '$lib/components/icons/QueueList.svelte';

	const i18n: any = getContext('i18n');

	let show = false;
	let now = Date.now() / 1000;
	let clockOffset = 0;
	let items: QueueItem[] = [];
	let ticker: ReturnType<typeof setInterval>;

	$: featureOn = ($config?.features as any)?.enable_chat_queue ?? false;
	$: state = $chatQueueState;
	$: serverNow = now + clockOffset;
	$: limitActive = !!state?.limit_until && state.limit_until > serverNow;
	$: remaining = limitActive && state?.limit_until ? state.limit_until - serverNow : 0;
	$: autoResume = state?.settings?.auto_resume ?? false;
	$: openCount = items.filter((i) => ['pending', 'running', 'needs_approval'].includes(i.status)).length;

	const load = async () => {
		if (!featureOn) return;
		try {
			const snapshot = await getQueue(localStorage.token);
			items = snapshot.items;
			chatQueueState.set(snapshot.state);
			clockOffset = snapshot.now - Date.now() / 1000;
		} catch {
			// the queue is optional here: stay silent
		}
	};

	let firstTick = true;
	$: if ($chatQueueTick !== undefined) {
		if (firstTick) firstTick = false;
		else void load();
	}

	onMount(() => {
		void load();
		ticker = setInterval(() => (now = Date.now() / 1000), 1000);
	});
	onDestroy(() => clearInterval(ticker));

	const setAutoResume = async (value: boolean) => {
		try {
			const next = await updateQueueSettings(localStorage.token, { auto_resume: value });
			chatQueueState.set(next);
			toast.success(
				value
					? $i18n.t('Auto-resume is on: a message is sent as soon as a limit is over.')
					: $i18n.t('Auto-resume is off.')
			);
		} catch (error) {
			toast.error(`${error}`);
			await load();
		}
	};

	const skip = async () => {
		try {
			chatQueueState.set(await clearQueueLimit(localStorage.token));
		} catch (error) {
			toast.error(`${error}`);
		}
	};
</script>

{#if featureOn}
	<Dropdown bind:show side="top" align="start" contentClass="w-[21rem] max-w-[92vw]">
		<Tooltip
			content={limitActive
				? $i18n.t('Limit reached — resuming in {{duration}}', { duration: formatDuration(remaining) })
				: $i18n.t('Queue and auto-resume')}
			placement="top"
		>
			<button
				type="button"
				aria-label={$i18n.t('Queue and auto-resume')}
				class="group flex max-w-full items-center gap-1.5 overflow-hidden rounded-full p-[0.375rem] text-sm transition-colors duration-300 focus:outline-hidden {limitActive
					? 'border border-amber-200/60 bg-amber-50 text-amber-700 hover:bg-amber-100 dark:border-amber-500/20 dark:bg-amber-400/10 dark:text-amber-300'
					: autoResume
						? 'border border-violet-200/50 bg-violet-50 text-violet-600 hover:bg-violet-100 dark:border-violet-500/20 dark:bg-violet-400/10 dark:text-violet-300'
						: 'bg-transparent text-gray-600 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800'}"
			>
				<QueueList className="size-3.5" strokeWidth="2" />
				{#if limitActive}
					<span class="pr-1 text-xs tabular-nums">{formatDuration(remaining)}</span>
				{:else if openCount > 0}
					<span class="pr-0.5 text-xs tabular-nums">{openCount}</span>
				{/if}
			</button>
		</Tooltip>

		<div slot="content" class="flex flex-col gap-3 rounded-2xl border border-gray-100 bg-white p-3.5 text-sm shadow-lg dark:border-gray-800 dark:bg-gray-900">
			<div class="flex items-start justify-between gap-3">
				<div>
					<div class="font-medium">{$i18n.t('Resume automatically after a limit')}</div>
					<div class="text-[0.75rem] text-gray-500 dark:text-gray-400">
						{$i18n.t('When a reply stops on a usage limit, a message is sent to continue as soon as the limit is over.')}
					</div>
				</div>
				<Switch state={autoResume} ariaLabel={$i18n.t('Resume automatically after a limit')} on:change={(e) => setAutoResume(e.detail)} />
			</div>

			{#if limitActive && state}
				<div class="rounded-xl bg-amber-50 px-3 py-2 text-[0.8125rem] text-amber-900 dark:bg-amber-500/10 dark:text-amber-100">
					<div class="font-medium">{$i18n.t('Limit reached')}</div>
					<div>
						{$i18n.t('Resuming automatically at {{time}} (in {{duration}}).', {
							time: formatClock(state.limit_until),
							duration: formatDuration(remaining)
						})}
					</div>
					<button type="button" class="mt-1.5 text-xs underline underline-offset-2" on:click={skip}>
						{$i18n.t('The limit is over — resume now')}
					</button>
				</div>
			{/if}

			<div class="flex items-center justify-between border-t border-gray-100 pt-2.5 dark:border-gray-800">
				<span class="text-[0.75rem] text-gray-500">
					{openCount > 0 ? $i18n.t('{{count}} in the queue', { count: openCount }) : $i18n.t('The queue is empty')}
				</span>
				<button
					type="button"
					class="rounded-full border border-gray-200 px-3 py-1 text-xs hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800"
					on:click={() => {
						show = false;
						goto('/queue');
					}}
				>
					{$i18n.t('Open the queue')}
				</button>
			</div>
		</div>
	</Dropdown>
{/if}
