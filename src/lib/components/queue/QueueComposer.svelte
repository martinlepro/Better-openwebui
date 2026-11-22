<script lang="ts">
	import { createEventDispatcher, getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { config, localAgentPrefs, models } from '$lib/stores';
	import { getChatList } from '$lib/apis/chats';
	import { createQueueItem, type QueueItemOptions } from '$lib/apis/chat-queue';
	import { validateLocalAgentCwd, type LocalAgentInfo } from '$lib/apis/local-agent';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Terminal from '$lib/components/icons/Terminal.svelte';

	const i18n: any = getContext('i18n');
	const dispatch = createEventDispatcher();

	export let agentInfo: LocalAgentInfo | null = null;

	type Priority = 'high' | 'normal' | 'low';
	const PRIORITY_VALUE: Record<Priority, number> = { high: 10, normal: 100, low: 500 };

	let mode: 'new' | 'existing' = 'new';
	let chatId = '';
	let modelId = '';
	let title = '';
	let prompt = '';
	let priority: Priority = 'normal';

	let agentEnabled = false;
	let agentCwd = '';
	let agentApproval: 'ask' | 'edits' | 'auto' = 'ask';
	let cwdError = '';
	let cwdChecking = false;

	let chats: { id: string; title: string }[] = [];
	let submitting = false;

	$: modelOptions = ($models ?? []).filter((m: any) => !m?.info?.meta?.hidden);
	$: agentAvailable = !!($config?.features as any)?.enable_local_agent && !!agentInfo?.available;
	$: canSubmit =
		!!prompt.trim() && (mode === 'existing' ? !!chatId : !!modelId) && !submitting && !cwdError;

	onMount(async () => {
		const list = await getChatList(localStorage.token, 1).catch(() => null);
		chats = (list ?? []).map((c: any) => ({ id: c.id, title: c.title }));
		modelId = modelOptions[0]?.id ?? '';
		// Start from what the chat input currently uses. Running without approvals is always an explicit choice.
		agentEnabled = $localAgentPrefs.enabled && agentAvailable;
		agentCwd = $localAgentPrefs.cwd;
		agentApproval = $localAgentPrefs.approval;
	});

	const checkCwd = async () => {
		cwdError = '';
		if (!agentEnabled || !agentCwd.trim()) return;
		cwdChecking = true;
		try {
			const res = await validateLocalAgentCwd(localStorage.token, agentCwd);
			agentCwd = res.path;
		} catch (error) {
			cwdError = `${error}`;
		} finally {
			cwdChecking = false;
		}
	};

	const submit = async () => {
		if (!canSubmit) return;
		submitting = true;
		try {
			const options: QueueItemOptions = {};
			if (agentEnabled && agentAvailable) {
				options.local_agent = {
					enabled: true,
					cwd: agentCwd.trim() || undefined,
					approval: agentApproval
				};
			}
			await createQueueItem(localStorage.token, {
				prompt: prompt.trim(),
				title: title.trim() || undefined,
				chat_id: mode === 'existing' ? chatId : null,
				model_id: modelId || null,
				priority: PRIORITY_VALUE[priority],
				options: Object.keys(options).length ? options : null
			});
			toast.success($i18n.t('Added to the queue'));
			prompt = '';
			title = '';
			dispatch('added');
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			submitting = false;
		}
	};

	const field =
		'w-full rounded-xl border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden transition focus:border-gray-300 dark:border-gray-800 dark:bg-gray-900 dark:focus:border-gray-600';
	const label = 'mb-1 block text-[0.75rem] font-medium text-gray-500 dark:text-gray-400';
</script>

<form
	class="flex flex-col gap-3 rounded-2xl border border-gray-100 bg-white p-4 dark:border-gray-850 dark:bg-gray-900"
	on:submit|preventDefault={submit}
>
	<div
		class="flex items-center gap-1 rounded-full bg-gray-100 p-0.5 text-[0.8125rem] dark:bg-gray-800 self-start"
	>
		<button
			type="button"
			class="rounded-full px-3 py-1 transition {mode === 'new'
				? 'bg-white shadow-xs dark:bg-gray-700'
				: 'text-gray-500'}"
			on:click={() => (mode = 'new')}>{$i18n.t('New conversation')}</button
		>
		<button
			type="button"
			class="rounded-full px-3 py-1 transition {mode === 'existing'
				? 'bg-white shadow-xs dark:bg-gray-700'
				: 'text-gray-500'}"
			on:click={() => (mode = 'existing')}>{$i18n.t('Existing conversation')}</button
		>
	</div>

	<div class="grid gap-3 md:grid-cols-2">
		{#if mode === 'existing'}
			<div>
				<label class={label} for="queue-chat">{$i18n.t('Conversation')}</label>
				<select id="queue-chat" class={field} bind:value={chatId}>
					<option value="">{$i18n.t('Choose a conversation')}</option>
					{#each chats as c (c.id)}
						<option value={c.id}>{c.title}</option>
					{/each}
				</select>
			</div>
		{:else}
			<div>
				<label class={label} for="queue-title">{$i18n.t('Title')} ({$i18n.t('optional')})</label>
				<input
					id="queue-title"
					class={field}
					bind:value={title}
					placeholder={$i18n.t('Refactor the parser')}
				/>
			</div>
		{/if}

		<div>
			<label class={label} for="queue-model">
				{$i18n.t('Model')}{mode === 'existing'
					? ` (${$i18n.t("default: the conversation's model")})`
					: ''}
			</label>
			<select id="queue-model" class={field} bind:value={modelId}>
				{#if mode === 'existing'}<option value="">—</option>{/if}
				{#each modelOptions as m (m.id)}
					<option value={m.id}>{m.name}</option>
				{/each}
			</select>
		</div>
	</div>

	<div>
		<label class={label} for="queue-prompt">{$i18n.t('Message to send')}</label>
		<textarea
			id="queue-prompt"
			class="{field} min-h-28 resize-y"
			placeholder={$i18n.t('What should the assistant do?')}
			bind:value={prompt}
		></textarea>
	</div>

	<div class="flex flex-wrap items-end gap-3">
		<div>
			<label class={label} for="queue-priority">{$i18n.t('Priority')}</label>
			<select id="queue-priority" class={field} bind:value={priority}>
				<option value="high">{$i18n.t('High')}</option>
				<option value="normal">{$i18n.t('Normal')}</option>
				<option value="low">{$i18n.t('Low')}</option>
			</select>
		</div>
	</div>

	{#if agentAvailable}
		<div class="rounded-xl border border-gray-100 p-3 dark:border-gray-800">
			<label class="flex cursor-pointer items-center gap-2 text-sm font-medium">
				<input type="checkbox" bind:checked={agentEnabled} on:change={checkCwd} />
				<Terminal className="size-4" strokeWidth="2" />
				{$i18n.t('Local agent')}
				<span class="font-normal text-gray-500"
					>— {$i18n.t('edit files and run commands on this computer')}</span
				>
			</label>

			{#if agentEnabled}
				<div class="mt-3 grid gap-3 md:grid-cols-2">
					<div>
						<label class={label} for="queue-cwd">{$i18n.t('Working folder')}</label>
						<div class="relative">
							<input
								id="queue-cwd"
								class={field}
								bind:value={agentCwd}
								on:blur={checkCwd}
								placeholder={agentInfo?.default_cwd ?? ''}
							/>
							{#if cwdChecking}<div class="absolute right-2.5 top-2.5">
									<Spinner className="size-4" />
								</div>{/if}
						</div>
						{#if cwdError}<p class="mt-1 text-xs text-red-500">{cwdError}</p>{/if}
					</div>
					<div>
						<label class={label} for="queue-approval">{$i18n.t('Approvals')}</label>
						<select id="queue-approval" class={field} bind:value={agentApproval}>
							<option value="auto">{$i18n.t('Autonomous — nothing asks (unattended)')}</option>
							<option value="edits">{$i18n.t('Edits run, commands ask')}</option>
							<option value="ask">{$i18n.t('Ask for every change')}</option>
						</select>
					</div>
				</div>
				<p class="mt-2 text-[0.75rem] text-gray-500 dark:text-gray-400">
					{#if agentApproval === 'auto'}
						{$i18n.t(
							'The assistant acts on its own inside the allowed folders. Dangerous commands stay blocked.'
						)}
					{:else}
						{$i18n.t(
							'When an action needs approval the item waits and you are notified; the rest of the queue continues.'
						)}
					{/if}
				</p>
			{/if}
		</div>
	{/if}

	<div class="flex items-center justify-end">
		<button
			type="submit"
			class="inline-flex items-center gap-1.5 rounded-full bg-black px-4 py-1.5 text-sm text-white transition hover:bg-gray-800 disabled:opacity-40 dark:bg-white dark:text-black dark:hover:bg-gray-200"
			disabled={!canSubmit}
		>
			{#if submitting}<Spinner className="size-3.5" />{:else}<Plus
					className="size-3.5"
					strokeWidth="2.5"
				/>{/if}
			{$i18n.t('Add to the queue')}
		</button>
	</div>
</form>
