<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { getPageContext } from '$lib/utils/page';
	import {
		chatQueueState,
		chatQueueTick,
		config,
		mobile,
		models,
		showSidebar,
		user
	} from '$lib/stores';
	import {
		clearFinishedQueueItems,
		clearQueueLimit,
		deleteQueueItem,
		getQueue,
		pauseQueue,
		reorderQueue,
		resumeQueue,
		runQueueItemNow,
		updateQueueItem,
		type QueueItem,
		type QueueState
	} from '$lib/apis/chat-queue';
	import { getLocalAgentInfo, type LocalAgentInfo } from '$lib/apis/local-agent';
	import { formatClock, formatDuration } from '$lib/utils/queueEvents';

	import SidebarIcon from '$lib/components/icons/Sidebar.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import QueueComposer from './QueueComposer.svelte';
	import QueueItemRow from './QueueItemRow.svelte';
	import QueueSettingsPanel from './QueueSettingsPanel.svelte';
	import LocalAgentAdmin from './LocalAgentAdmin.svelte';
	import LocalAgentProcesses from './LocalAgentProcesses.svelte';

	const i18n: any = getContext('i18n');
	const { goto, tab: pageTab } = getPageContext();

	const ACTIVE = ['running', 'needs_approval', 'pending', 'paused'];
	const STATUS_RANK: Record<string, number> = {
		running: 0,
		needs_approval: 1,
		pending: 2,
		paused: 3
	};

	let items: QueueItem[] = [];
	let state: QueueState | null = null;
	let clockOffset = 0; // server time - browser time, so the countdown is right even if clocks differ
	let now = Date.now() / 1000;
	let loading = true;
	let showSettings = false;
	let showFinished = false;
	let agentInfo: LocalAgentInfo | null = null;
	let busy = false;

	let ticker: ReturnType<typeof setInterval>;
	let poller: ReturnType<typeof setInterval>;

	$: serverNow = now + clockOffset;
	$: active = items
		.filter((i) => ACTIVE.includes(i.status))
		.sort(
			(a, b) =>
				(STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9) ||
				a.priority - b.priority ||
				a.created_at - b.created_at
		);
	$: waiting = active.filter((i) => i.status === 'pending' || i.status === 'paused');
	$: finished = items
		.filter((i) => !ACTIVE.includes(i.status))
		.sort((a, b) => (b.finished_at ?? b.updated_at) - (a.finished_at ?? a.updated_at));
	$: limitActive = !!state?.limit_until && state.limit_until > serverNow;
	$: limitRemaining = limitActive && state?.limit_until ? state.limit_until - serverNow : 0;
	$: modelNames = Object.fromEntries(($models ?? []).map((m: any) => [m.id, m.name]));

	const load = async () => {
		try {
			const snapshot = await getQueue(localStorage.token);
			items = snapshot.items;
			state = snapshot.state;
			chatQueueState.set(snapshot.state);
			clockOffset = snapshot.now - Date.now() / 1000;
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			loading = false;
		}
	};

	// Any queue:* socket event bumps this store: reload right away.
	let firstTick = true;
	$: if ($chatQueueTick !== undefined) {
		if (firstTick) firstTick = false;
		else void load();
	}

	onMount(async () => {
		await load();
		ticker = setInterval(() => (now = Date.now() / 1000), 1000);
		poller = setInterval(load, 15000); // safety net if a socket event is missed
		if (($config?.features as any)?.enable_local_agent) {
			agentInfo = await getLocalAgentInfo(localStorage.token).catch(() => null);
		}
	});
	onDestroy(() => {
		clearInterval(ticker);
		clearInterval(poller);
	});

	const run = async (action: () => Promise<unknown>) => {
		if (busy) return;
		busy = true;
		try {
			await action();
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			busy = false;
			await load();
		}
	};

	const move = (item: QueueItem, direction: number) =>
		run(async () => {
			const order = active.map((i) => i.id);
			const from = order.indexOf(item.id);
			const to = from + direction;
			if (from < 0 || to < 0 || to >= order.length) return;
			[order[from], order[to]] = [order[to], order[from]];
			await reorderQueue(localStorage.token, order);
		});

	const save = (item: QueueItem, e: CustomEvent) =>
		run(() =>
			updateQueueItem(localStorage.token, item.id, {
				title: e.detail.title,
				prompt: e.detail.prompt,
				priority: e.detail.priority
			})
		);

	const openChat = (item: QueueItem) => item.chat_id && goto(`/c/${item.chat_id}`);

	const togglePause = () =>
		run(async () => {
			state = state?.paused
				? await resumeQueue(localStorage.token)
				: await pauseQueue(localStorage.token);
		});

	const skipLimit = () =>
		run(async () => {
			await clearQueueLimit(localStorage.token);
			toast.success($i18n.t('Resuming the queue.'));
		});

	const clearHistory = () =>
		run(async () => {
			await clearFinishedQueueItems(localStorage.token);
		});

	$: headline = !state
		? ''
		: state.paused
			? $i18n.t('Queue paused')
			: limitActive
				? $i18n.t('Waiting for the limit to reset')
				: state.running
					? $i18n.t('Running')
					: waiting.length > 0
						? $i18n.t('Starting…')
						: $i18n.t('Idle');
</script>

<svelte:head>
	<title>{$i18n.t('Queue')}</title>
</svelte:head>

<div
	class="flex h-full max-h-full w-full max-w-full flex-col transition-width duration-200 ease-in-out {$showSidebar &&
	!pageTab
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''}"
>
	<div class="flex h-full min-h-0 flex-col">
		<div class="shrink-0 px-1.5 pb-1 md:px-2.5">
			<div class="flex items-center gap-0.5 md:gap-1">
				{#if $mobile && !pageTab}
					<div class="{$showSidebar ? 'md:hidden' : ''} flex flex-none items-center">
						<Tooltip content={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}>
							<button
								id="sidebar-toggle-button"
								class="flex size-7 items-center justify-center text-gray-400 transition"
								aria-label={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
								on:click={() => showSidebar.set(!$showSidebar)}
								type="button"
							>
								<SidebarIcon className="size-4" />
							</button>
						</Tooltip>
					</div>
				{/if}

				<div class="flex w-full min-w-0 items-center gap-2 py-1">
					<span class="min-w-fit px-1 text-sm select-none">{$i18n.t('Queue')}</span>
					<span class="text-sm text-gray-500">{active.length || ''}</span>
					{#if headline}
						<span
							class="ml-1 rounded-full px-2 py-0.5 text-[0.6875rem] {limitActive
								? 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300'
								: state?.paused
									? 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
									: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300'}"
						>
							{headline}
						</span>
					{/if}
					<div class="ml-auto flex items-center gap-1.5">
						<button
							type="button"
							class="rounded-full border border-gray-200 px-3 py-1 text-xs transition hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:hover:bg-gray-800"
							disabled={busy || !state}
							on:click={togglePause}
						>
							{state?.paused ? $i18n.t('Resume the queue') : $i18n.t('Pause the queue')}
						</button>
						<button
							type="button"
							class="rounded-full border border-gray-200 px-3 py-1 text-xs transition hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800 {showSettings
								? 'bg-gray-50 dark:bg-gray-800'
								: ''}"
							on:click={() => (showSettings = !showSettings)}
						>
							{$i18n.t('Settings')}
						</button>
					</div>
				</div>
			</div>
		</div>

		<div class="min-h-0 flex-1 overflow-y-auto px-3 pb-8 md:px-5">
			<div class="mx-auto flex max-w-3xl flex-col gap-4 pt-2">
				{#if limitActive && state}
					<div
						class="flex flex-wrap items-center gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100"
						role="status"
					>
						<div class="min-w-0 flex-1">
							<div class="text-sm font-medium">
								{$i18n.t('Limit reached — the queue is waiting')}
							</div>
							<div class="text-[0.8125rem]">
								{$i18n.t('Resuming automatically at {{time}} (in {{duration}}).', {
									time: formatClock(state.limit_until),
									duration: formatDuration(limitRemaining)
								})}
							</div>
							{#if state.limit_message}
								<div class="mt-0.5 line-clamp-2 break-words text-[0.6875rem] opacity-70">
									{state.limit_message}
								</div>
							{/if}
						</div>
						<button
							type="button"
							class="rounded-full border border-amber-300 bg-white/70 px-3 py-1 text-xs hover:bg-white dark:border-amber-500/40 dark:bg-transparent dark:hover:bg-amber-500/10"
							disabled={busy}
							on:click={skipLimit}
						>
							{$i18n.t('The limit is over — resume now')}
						</button>
					</div>
				{/if}

				{#if showSettings && state}
					<QueueSettingsPanel value={state.settings} on:changed={load} />
					{#if $user?.role === 'admin' && ($config?.features as any)}
						<LocalAgentAdmin />
					{/if}
				{/if}

				<QueueComposer {agentInfo} on:added={load} />

				{#if agentInfo?.available}
					<LocalAgentProcesses />
				{/if}

				<div>
					<div class="mb-2 flex items-center justify-between px-1">
						<h2 class="text-sm font-medium">{$i18n.t('Up next')}</h2>
						{#if waiting.length > 1}
							<span class="text-[0.6875rem] text-gray-400"
								>{$i18n.t('Runs from top to bottom, highest priority first.')}</span
							>
						{/if}
					</div>

					{#if loading}
						<div class="flex justify-center py-8"><Spinner className="size-5" /></div>
					{:else if active.length === 0}
						<div
							class="rounded-2xl border border-dashed border-gray-200 px-4 py-8 text-center text-sm text-gray-500 dark:border-gray-800"
						>
							{$i18n.t(
								'The queue is empty. Add a conversation above — it is sent automatically, even while you are away.'
							)}
						</div>
					{:else}
						<div class="flex flex-col gap-2">
							{#each active as item (item.id)}
								{@const idx = active.indexOf(item)}
								<QueueItemRow
									{item}
									{now}
									position={item.status === 'running' ? null : idx + 1}
									first={idx === 0 || active[idx - 1]?.status === 'running'}
									last={idx === active.length - 1}
									modelName={item.model_id ? (modelNames[item.model_id] ?? item.model_id) : ''}
									{limitActive}
									on:move={(e) => move(item, e.detail.direction)}
									on:runnow={() => run(() => runQueueItemNow(localStorage.token, item.id))}
									on:pause={() =>
										run(() => updateQueueItem(localStorage.token, item.id, { status: 'paused' }))}
									on:resume={() =>
										run(() => updateQueueItem(localStorage.token, item.id, { status: 'pending' }))}
									on:cancel={() =>
										run(() =>
											updateQueueItem(localStorage.token, item.id, { status: 'cancelled' })
										)}
									on:save={(e) => save(item, e)}
									on:open={() => openChat(item)}
								/>
							{/each}
						</div>
					{/if}
				</div>

				{#if finished.length > 0}
					<div>
						<div class="mb-2 flex items-center justify-between px-1">
							<button
								type="button"
								class="flex items-center gap-1 text-sm font-medium"
								on:click={() => (showFinished = !showFinished)}
							>
								<ChevronDown
									className="size-3.5 transition {showFinished ? '' : '-rotate-90'}"
									strokeWidth="2.5"
								/>
								{$i18n.t('Finished')} ({finished.length})
							</button>
							{#if showFinished}
								<button
									type="button"
									class="text-xs text-gray-500 hover:text-gray-800 dark:hover:text-gray-200"
									disabled={busy}
									on:click={clearHistory}>{$i18n.t('Clear history')}</button
								>
							{/if}
						</div>
						{#if showFinished}
							<div class="flex flex-col gap-2">
								{#each finished as item (item.id)}
									<QueueItemRow
										{item}
										{now}
										modelName={item.model_id ? (modelNames[item.model_id] ?? item.model_id) : ''}
										on:open={() => openChat(item)}
										on:runnow={() => run(() => runQueueItemNow(localStorage.token, item.id))}
										on:retry={() =>
											run(() =>
												updateQueueItem(localStorage.token, item.id, { status: 'pending' })
											)}
										on:save={(e) => save(item, e)}
										on:delete={() => run(() => deleteQueueItem(localStorage.token, item.id))}
									/>
								{/each}
							</div>
						{/if}
					</div>
				{/if}
			</div>
		</div>
	</div>
</div>
