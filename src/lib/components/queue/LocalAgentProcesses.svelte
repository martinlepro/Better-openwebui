<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getLocalAgentProcesses,
		stopLocalAgentProcess,
		type LocalAgentProcess
	} from '$lib/apis/local-agent';
	import { formatDuration } from '$lib/utils/queueEvents';

	const i18n: any = getContext('i18n');

	let processes: LocalAgentProcess[] = [];
	let timer: ReturnType<typeof setInterval>;

	const load = async () => {
		processes = (await getLocalAgentProcesses(localStorage.token).catch(() => ({ processes: [] })))
			.processes;
	};

	const stop = async (id: string) => {
		try {
			await stopLocalAgentProcess(localStorage.token, id);
			toast.success($i18n.t('Process stopped'));
		} catch (error) {
			toast.error(`${error}`);
		}
		await load();
	};

	onMount(() => {
		load();
		timer = setInterval(load, 8000);
	});
	onDestroy(() => clearInterval(timer));
</script>

{#if processes.length > 0}
	<div class="rounded-2xl border border-gray-100 bg-white p-4 dark:border-gray-850 dark:bg-gray-900">
		<div class="mb-2 text-sm font-medium">{$i18n.t('Background processes started by the agent')}</div>
		<div class="flex flex-col gap-1.5">
			{#each processes as p (p.id)}
				<div class="flex items-center gap-3 rounded-xl bg-gray-50 px-3 py-1.5 text-[0.8125rem] dark:bg-gray-850">
					<span
						class="size-2 shrink-0 rounded-full {p.status === 'running' ? 'bg-emerald-500' : 'bg-gray-400'}"
					></span>
					<div class="min-w-0 flex-1">
						<div class="truncate font-medium">{p.name}</div>
						<div class="truncate font-mono text-[0.6875rem] text-gray-500">{p.command} · {p.cwd}</div>
					</div>
					<span class="shrink-0 text-[0.6875rem] text-gray-400">
						{p.status}{p.status === 'running' ? ` · ${formatDuration(p.uptime_seconds)}` : ''}
					</span>
					{#if p.status === 'running'}
						<button
							type="button"
							class="shrink-0 rounded-full border border-gray-200 px-2.5 py-0.5 text-xs hover:bg-white dark:border-gray-700 dark:hover:bg-gray-800"
							on:click={() => stop(p.id)}>{$i18n.t('Stop')}</button
						>
					{/if}
				</div>
			{/each}
		</div>
	</div>
{/if}
