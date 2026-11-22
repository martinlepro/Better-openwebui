<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import type { QueueItem } from '$lib/apis/chat-queue';
	import { formatClock, formatDuration } from '$lib/utils/queueEvents';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import Play from '$lib/components/icons/Play.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import ArrowUpCircle from '$lib/components/icons/ArrowUpCircle.svelte';
	import ChatBubble from '$lib/components/icons/ChatBubble.svelte';
	import Terminal from '$lib/components/icons/Terminal.svelte';

	const i18n: any = getContext('i18n');
	const dispatch = createEventDispatcher();

	export let item: QueueItem;
	export let position: number | null = null;
	export let first = false;
	export let last = false;
	export let modelName = '';
	/** The provider limit is active: waiting items are held back until it resets. */
	export let limitActive = false;
	export let now = Date.now() / 1000;

	let editing = false;
	let draftTitle = '';
	let draftPrompt = '';
	let draftPriority = 100;

	$: active = ['pending', 'running', 'needs_approval', 'paused'].includes(item.status);
	$: editable = ['pending', 'paused', 'failed', 'cancelled'].includes(item.status);
	$: agent = item.options?.local_agent;

	const PRIORITIES = [
		{ value: 10, label: 'High' },
		{ value: 100, label: 'Normal' },
		{ value: 500, label: 'Low' }
	];

	$: priorityLabel = item.priority <= 10 ? 'High' : item.priority <= 100 ? 'Normal' : 'Low';

	const STATUS_STYLE: Record<string, string> = {
		pending: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
		paused: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400',
		running: 'bg-sky-50 text-sky-600 dark:bg-sky-500/10 dark:text-sky-300',
		needs_approval: 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300',
		done: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300',
		failed: 'bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-300',
		cancelled: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
	};

	$: statusLabel =
		{
			pending: $i18n.t('Waiting'),
			paused: $i18n.t('Paused'),
			running: $i18n.t('Running'),
			needs_approval: $i18n.t('Needs approval'),
			done: $i18n.t('Done'),
			failed: $i18n.t('Failed'),
			cancelled: $i18n.t('Cancelled')
		}[item.status] ?? item.status;

	$: retryIn =
		item.status === 'pending' && item.next_attempt_at && item.next_attempt_at > now
			? item.next_attempt_at - now
			: 0;

	const startEdit = () => {
		draftTitle = item.title ?? '';
		draftPrompt = item.prompt;
		draftPriority = item.priority <= 10 ? 10 : item.priority <= 100 ? 100 : 500;
		editing = true;
	};

	const save = () => {
		if (!draftPrompt.trim()) return;
		dispatch('save', { title: draftTitle, prompt: draftPrompt, priority: draftPriority });
		editing = false;
	};

	const iconButton =
		'flex size-6 items-center justify-center rounded-lg text-gray-500 transition hover:bg-gray-100 hover:text-gray-800 disabled:opacity-30 disabled:hover:bg-transparent dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100';
</script>

<div
	class="group rounded-2xl border border-gray-100 bg-white px-3 py-2.5 dark:border-gray-850 dark:bg-gray-900"
	data-queue-item={item.id}
>
	<div class="flex items-start gap-2.5">
		{#if active && item.status !== 'running' && position !== null}
			<div class="flex shrink-0 flex-col items-center pt-0.5">
				<button
					type="button"
					class={iconButton}
					disabled={first}
					aria-label={$i18n.t('Move up')}
					on:click={() => dispatch('move', { direction: -1 })}
				>
					<ChevronUp className="size-3.5" strokeWidth="2.5" />
				</button>
				<span class="text-[0.6875rem] tabular-nums text-gray-400 dark:text-gray-500">{position}</span>
				<button
					type="button"
					class={iconButton}
					disabled={last}
					aria-label={$i18n.t('Move down')}
					on:click={() => dispatch('move', { direction: 1 })}
				>
					<ChevronDown className="size-3.5" strokeWidth="2.5" />
				</button>
			</div>
		{:else if item.status === 'running'}
			<div class="flex size-6 shrink-0 items-center justify-center pt-1 text-sky-500">
				<Spinner className="size-4" />
			</div>
		{/if}

		<div class="min-w-0 flex-1">
			<div class="flex flex-wrap items-center gap-1.5">
				<span class="min-w-0 truncate text-[0.875rem] font-medium text-gray-900 dark:text-gray-100">
					{item.title || item.prompt.slice(0, 60)}
				</span>
				<span class="rounded-full px-2 py-0.5 text-[0.6875rem] {STATUS_STYLE[item.status]}">
					{statusLabel}
				</span>
				{#if item.kind === 'resume'}
					<span
						class="rounded-full bg-violet-50 px-2 py-0.5 text-[0.6875rem] text-violet-600 dark:bg-violet-500/10 dark:text-violet-300"
					>
						{$i18n.t('Resume after limit')}
					</span>
				{/if}
				{#if active}
					<span class="text-[0.6875rem] text-gray-400 dark:text-gray-500">
						{$i18n.t('Priority')}: {$i18n.t(priorityLabel)}
					</span>
				{/if}
				{#if agent?.enabled}
					<Tooltip
						content={`${$i18n.t('Local agent')} · ${agent.cwd || ''} · ${$i18n.t('approval')}: ${agent.approval ?? 'ask'}`}
					>
						<span
							class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[0.6875rem] text-gray-600 dark:bg-gray-800 dark:text-gray-300"
						>
							<Terminal className="size-3" strokeWidth="2" />
							{$i18n.t('Local agent')}
						</span>
					</Tooltip>
				{/if}
			</div>

			{#if editing}
				<div class="mt-2 flex flex-col gap-2">
					<input
						class="w-full rounded-xl border border-gray-100 bg-transparent px-2.5 py-1.5 text-sm outline-hidden dark:border-gray-800"
						placeholder={$i18n.t('Title')}
						bind:value={draftTitle}
					/>
					<textarea
						class="min-h-24 w-full resize-y rounded-xl border border-gray-100 bg-transparent px-2.5 py-1.5 text-sm outline-hidden dark:border-gray-800"
						bind:value={draftPrompt}
					></textarea>
					<div class="flex items-center gap-2">
						<select
							class="rounded-xl border border-gray-100 bg-transparent px-2 py-1 text-sm outline-hidden dark:border-gray-800 dark:bg-gray-900"
							bind:value={draftPriority}
						>
							{#each PRIORITIES as p}
								<option value={p.value}>{$i18n.t('Priority')}: {$i18n.t(p.label)}</option>
							{/each}
						</select>
						<div class="ml-auto flex gap-1.5">
							<button
								type="button"
								class="rounded-full px-3 py-1 text-xs text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
								on:click={() => (editing = false)}>{$i18n.t('Cancel')}</button
							>
							<button
								type="button"
								class="rounded-full bg-black px-3 py-1 text-xs text-white hover:bg-gray-800 disabled:opacity-40 dark:bg-white dark:text-black dark:hover:bg-gray-200"
								disabled={!draftPrompt.trim()}
								on:click={save}>{$i18n.t('Save')}</button
							>
						</div>
					</div>
				</div>
			{:else}
				<p class="mt-0.5 line-clamp-2 whitespace-pre-wrap break-words text-[0.8125rem] text-gray-600 dark:text-gray-400">
					{item.prompt}
				</p>
			{/if}

			<div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[0.6875rem] text-gray-400 dark:text-gray-500">
				{#if modelName}<span>{modelName}</span>{/if}
				{#if item.attempts > 1}<span>{$i18n.t('{{count}} attempts', { count: item.attempts })}</span>{/if}
				{#if item.status === 'pending' && limitActive}
					<span class="text-amber-600 dark:text-amber-400">{$i18n.t('Held until the limit is over')}</span>
				{:else if retryIn > 0}
					<span>{$i18n.t('Retrying in {{duration}}', { duration: formatDuration(retryIn) })}</span>
				{/if}
				{#if item.finished_at}<span>{formatClock(item.finished_at)}</span>{/if}
			</div>

			{#if item.last_error && ['failed', 'cancelled', 'pending', 'done'].includes(item.status)}
				<p class="mt-1 line-clamp-2 break-words text-[0.6875rem] text-red-500/90 dark:text-red-400/90">
					{item.last_error}
				</p>
			{/if}
		</div>

		<div class="flex shrink-0 items-center gap-0.5 opacity-70 transition group-hover:opacity-100">
			{#if item.chat_id}
				<Tooltip content={$i18n.t('Open conversation')}>
					<button type="button" class={iconButton} aria-label={$i18n.t('Open conversation')} on:click={() => dispatch('open')}>
						<ChatBubble className="size-3.5" strokeWidth="2" />
					</button>
				</Tooltip>
			{/if}
			{#if ['pending', 'paused', 'failed', 'cancelled'].includes(item.status)}
				<Tooltip content={$i18n.t('Run first')}>
					<button type="button" class={iconButton} aria-label={$i18n.t('Run first')} on:click={() => dispatch('runnow')}>
						<ArrowUpCircle className="size-4" strokeWidth="2" />
					</button>
				</Tooltip>
			{/if}
			{#if item.status === 'pending'}
				<Tooltip content={$i18n.t('Pause this item')}>
					<button type="button" class={iconButton} aria-label={$i18n.t('Pause this item')} on:click={() => dispatch('pause')}>
						<span class="text-[0.65rem] font-bold leading-none">II</span>
					</button>
				</Tooltip>
			{:else if item.status === 'paused'}
				<Tooltip content={$i18n.t('Resume this item')}>
					<button type="button" class={iconButton} aria-label={$i18n.t('Resume this item')} on:click={() => dispatch('resume')}>
						<Play className="size-3.5" />
					</button>
				</Tooltip>
			{/if}
			{#if item.status === 'failed' || item.status === 'cancelled'}
				<Tooltip content={$i18n.t('Retry')}>
					<button type="button" class={iconButton} aria-label={$i18n.t('Retry')} on:click={() => dispatch('retry')}>
						<ArrowPath className="size-3.5" strokeWidth="2" />
					</button>
				</Tooltip>
			{/if}
			{#if editable && !editing}
				<Tooltip content={$i18n.t('Edit')}>
					<button type="button" class={iconButton} aria-label={$i18n.t('Edit')} on:click={startEdit}>
						<Pencil className="size-3.5" strokeWidth="2" />
					</button>
				</Tooltip>
			{/if}
			{#if active}
				<Tooltip content={$i18n.t('Cancel')}>
					<button type="button" class={iconButton} aria-label={$i18n.t('Cancel')} on:click={() => dispatch('cancel')}>
						<XMark className="size-3.5" strokeWidth="2.5" />
					</button>
				</Tooltip>
			{:else}
				<Tooltip content={$i18n.t('Delete')}>
					<button type="button" class={iconButton} aria-label={$i18n.t('Delete')} on:click={() => dispatch('delete')}>
						<GarbageBin className="size-3.5" strokeWidth="2" />
					</button>
				</Tooltip>
			{/if}
		</div>
	</div>
</div>
