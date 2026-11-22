<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getLocalAgentConfig,
		setLocalAgentConfig,
		type LocalAgentConfig
	} from '$lib/apis/local-agent';

	import Switch from '$lib/components/common/Switch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n: any = getContext('i18n');

	let cfg: LocalAgentConfig | null = null;
	let rootsText = '';
	let saving = false;

	const load = async () => {
		try {
			cfg = await getLocalAgentConfig(localStorage.token);
			rootsText = cfg.allowed_roots.join('\n');
		} catch (error) {
			toast.error(`${error}`);
		}
	};

	const save = async (patch: Partial<LocalAgentConfig> = {}) => {
		if (!cfg) return;
		saving = true;
		try {
			cfg = await setLocalAgentConfig(localStorage.token, {
				...cfg,
				allowed_roots: rootsText
					.split('\n')
					.map((s) => s.trim())
					.filter(Boolean),
				...patch
			});
			rootsText = cfg.allowed_roots.join('\n');
			toast.success($i18n.t('Saved'));
		} catch (error) {
			toast.error(`${error}`);
			await load();
		} finally {
			saving = false;
		}
	};

	onMount(load);

	const field =
		'w-full rounded-xl border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden dark:border-gray-800';
</script>

{#if cfg}
	<div class="flex flex-col gap-4 rounded-2xl border border-gray-100 bg-white p-4 dark:border-gray-850 dark:bg-gray-900">
		<div>
			<div class="text-sm font-medium">{$i18n.t('Local agent')} — {$i18n.t('administration')}</div>
			<p class="mt-0.5 text-[0.75rem] text-gray-500 dark:text-gray-400">
				{$i18n.t(
					'Lets the model read and edit files and run commands on the machine that runs the Open WebUI server, like Claude Code. This executes code: enable it only if you trust the people who can use it.'
				)}
			</p>
		</div>

		{#if cfg.running_in_docker}
			<div class="rounded-xl bg-amber-50 px-3 py-2 text-[0.75rem] text-amber-800 dark:bg-amber-500/10 dark:text-amber-200">
				{$i18n.t(
					'The server runs inside Docker: the agent will act inside the container, not on your computer. Run the backend directly on your machine, or mount your project folder into the container.'
				)}
			</div>
		{/if}

		<div class="flex items-center justify-between gap-4">
			<div class="text-[0.8125rem]">{$i18n.t('Enable the local agent')}</div>
			<Switch state={cfg.enable} on:change={(e) => save({ enable: e.detail })} />
		</div>
		<div class="flex items-center justify-between gap-4">
			<div>
				<div class="text-[0.8125rem]">{$i18n.t('Administrators only')}</div>
				<div class="text-[0.6875rem] text-gray-400">{$i18n.t('Strongly recommended on a shared server')}</div>
			</div>
			<Switch state={cfg.admin_only} on:change={(e) => save({ admin_only: e.detail })} />
		</div>

		<div>
			<label class="mb-1 block text-[0.75rem] font-medium text-gray-500" for="agent-roots">
				{$i18n.t('Allowed folders (one per line)')}
			</label>
			<textarea id="agent-roots" class="{field} min-h-20 resize-y font-mono text-xs" bind:value={rootsText}></textarea>
			<p class="mt-1 text-[0.6875rem] text-gray-400">
				{$i18n.t('File tools cannot leave these folders. Commands run inside them but a shell can reach further: use approvals.')}
			</p>
		</div>

		<div class="grid gap-3 sm:grid-cols-3">
			<div>
				<label class="mb-1 block text-[0.75rem] text-gray-500" for="agent-timeout">{$i18n.t('Command timeout (s)')}</label>
				<input id="agent-timeout" type="number" min="5" max="1800" class={field} bind:value={cfg.command_timeout} />
			</div>
			<div>
				<label class="mb-1 block text-[0.75rem] text-gray-500" for="agent-output">{$i18n.t('Max output (chars)')}</label>
				<input id="agent-output" type="number" min="1000" max="500000" class={field} bind:value={cfg.max_output} />
			</div>
			<div>
				<label class="mb-1 block text-[0.75rem] text-gray-500" for="agent-procs">{$i18n.t('Max background processes')}</label>
				<input id="agent-procs" type="number" min="1" max="20" class={field} bind:value={cfg.max_processes} />
			</div>
		</div>

		<div class="flex justify-end">
			<button
				type="button"
				class="inline-flex items-center gap-1.5 rounded-full bg-black px-4 py-1.5 text-sm text-white hover:bg-gray-800 disabled:opacity-40 dark:bg-white dark:text-black"
				disabled={saving}
				on:click={() => save()}
			>
				{#if saving}<Spinner className="size-3.5" />{/if}
				{$i18n.t('Save')}
			</button>
		</div>
	</div>
{/if}
