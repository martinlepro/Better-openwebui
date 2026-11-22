<script lang="ts">
	import { config, user } from '$lib/stores';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import QueuePage from '$lib/components/queue/QueuePage.svelte';

	onMount(() => {
		if (!$user || ($config?.features as any)?.enable_chat_queue === false) {
			goto('/');
		}
	});
</script>

<QueuePage />
