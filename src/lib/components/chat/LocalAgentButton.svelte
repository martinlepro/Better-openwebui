<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { config, localAgentPrefs } from '$lib/stores';
	import {
		getLocalAgentInfo,
		validateLocalAgentCwd,
		type LocalAgentInfo
	} from '$lib/apis/local-agent';

	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Terminal from '$lib/components/icons/Terminal.svelte';

	const i18n: any = getContext('i18n');

	let show = false;
	let info: LocalAgentInfo | null = null;
	let cwdDraft = '';
	let cwdError = '';

	$: featureOn = !!($config?.features as any)?.enable_local_agent;
	$: active = featureOn && $localAgentPrefs.enabled;
	$: folderName =
		($localAgentPrefs.cwd || info?.default_cwd || '').split(/[\\/]/).filter(Boolean).pop() ?? '';

	onMount(async () => {
		if (!featureOn) return;
		info = await getLocalAgentInfo(localStorage.token).catch(() => null);
		cwdDraft = $localAgentPrefs.cwd;
	});

	const commitCwd = async () => {
		cwdError = '';
		const value = cwdDraft.trim();
		if (!value) {
			localAgentPrefs.update((p) => ({ ...p, cwd: '' }));
			return;
		}
		try {
			const res = await validateLocalAgentCwd(localStorage.token, value);
			cwdDraft = res.path;
			localAgentPrefs.update((p) => ({ ...p, cwd: res.path }));
		} catch (error) {
			cwdError = `${error}`;
		}
	};

	const toggle = (value: boolean) => {
		if (value && info && !info.available) {
			toast.error($i18n.t('The local agent is not available.'));
			return;
		}
		localAgentPrefs.update((p) => ({ ...p, enabled: value }));
	};

	const APPROVALS = [
		{
			value: 'ask',
			label: 'Ask for every change',
			hint: 'You approve each edit and command before it runs.'
		},
		{
			value: 'edits',
			label: 'Edits run, commands ask',
			hint: 'Files are changed freely; commands and servers need your approval.'
		},
		{
			value: 'auto',
			label: 'Autonomous',
			hint: 'Nothing asks. Everything the assistant does is real and immediate.'
		}
	] as const;

	$: hint = APPROVALS.find((a) => a.value === $localAgentPrefs.approval)?.hint ?? '';
</script>

{#if featureOn}
	<Dropdown bind:show side="top" align="start" contentClass="w-[22rem] max-w-[92vw]">
		<Tooltip content={$i18n.t('Local agent')} placement="top">
			<button
				type="button"
				aria-label={$i18n.t('Local agent')}
				aria-pressed={active}
				class="group flex max-w-full items-center gap-1.5 overflow-hidden rounded-full p-[0.375rem] text-sm transition-colors duration-300 focus:outline-hidden {active
					? 'border border-emerald-200/50 bg-emerald-50 text-emerald-600 hover:bg-emerald-100 dark:border-emerald-500/20 dark:bg-emerald-400/10 dark:text-emerald-300 dark:hover:bg-emerald-700/10'
					: 'bg-transparent text-gray-600 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800'}"
			>
				<Terminal className="size-3.5" strokeWidth="2" />
				{#if active && folderName}
					<span class="max-w-28 truncate pr-1 text-xs">{folderName}</span>
				{/if}
			</button>
		</Tooltip>

		<div
			slot="content"
			class="flex flex-col gap-3 rounded-2xl border border-gray-100 bg-white p-3.5 text-sm shadow-lg dark:border-gray-800 dark:bg-gray-900"
		>
			<div class="flex items-center justify-between gap-3">
				<div>
					<div class="font-medium">{$i18n.t('Local agent')}</div>
					<div class="text-[0.75rem] text-gray-500 dark:text-gray-400">
						{$i18n.t('Edit files and run commands on this computer, like Claude Code.')}
					</div>
				</div>
				<Switch
					state={$localAgentPrefs.enabled}
					ariaLabel={$i18n.t('Local agent')}
					on:change={(e) => toggle(e.detail)}
				/>
			</div>

			{#if info?.running_in_docker}
				<div
					class="rounded-xl bg-amber-50 px-2.5 py-2 text-[0.6875rem] text-amber-800 dark:bg-amber-500/10 dark:text-amber-200"
				>
					{$i18n.t(
						'The server runs inside Docker: the agent acts inside the container, not on your computer.'
					)}
				</div>
			{/if}

			<div>
				<label class="mb-1 block text-[0.75rem] text-gray-500" for="local-agent-cwd"
					>{$i18n.t('Working folder')}</label
				>
				<input
					id="local-agent-cwd"
					class="w-full rounded-xl border border-gray-100 bg-transparent px-2.5 py-1.5 text-sm outline-hidden dark:border-gray-800"
					bind:value={cwdDraft}
					on:blur={commitCwd}
					on:keydown={(e) => e.key === 'Enter' && commitCwd()}
					placeholder={info?.default_cwd ?? ''}
				/>
				{#if cwdError}
					<p class="mt-1 text-[0.6875rem] text-red-500">{cwdError}</p>
				{:else if info?.roots?.length}
					<p class="mt-1 text-[0.6875rem] text-gray-400">
						{$i18n.t('Allowed')}: {info.roots.join(', ')}
					</p>
				{/if}
			</div>

			<div>
				<label class="mb-1 block text-[0.75rem] text-gray-500" for="local-agent-approval"
					>{$i18n.t('Approvals')}</label
				>
				<select
					id="local-agent-approval"
					class="w-full rounded-xl border border-gray-100 bg-transparent px-2.5 py-1.5 text-sm outline-hidden dark:border-gray-800 dark:bg-gray-900"
					bind:value={$localAgentPrefs.approval}
				>
					{#each APPROVALS as a}
						<option value={a.value}>{$i18n.t(a.label)}</option>
					{/each}
				</select>
				<p class="mt-1 text-[0.6875rem] text-gray-400">{$i18n.t(hint)}</p>
			</div>

			<p class="text-[0.6875rem] text-gray-400">
				{$i18n.t(
					'Reading files never asks. Dangerous commands (rm -rf /, sudo, shutdown…) are always blocked.'
				)}
				{#if info}· {info.system} · {info.shell}{/if}
			</p>
		</div>
	</Dropdown>
{/if}
