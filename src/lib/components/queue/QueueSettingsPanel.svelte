<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { settings } from '$lib/stores';
	import { updateUserSettings } from '$lib/apis/users';
	import { updateQueueSettings, type QueueSettings } from '$lib/apis/chat-queue';

	import Switch from '$lib/components/common/Switch.svelte';

	const i18n: any = getContext('i18n');
	const dispatch = createEventDispatcher();

	export let value: QueueSettings;

	let resumeMessage = value.resume_message;
	let permission: NotificationPermission | 'unsupported' =
		typeof window !== 'undefined' && 'Notification' in window ? Notification.permission : 'unsupported';

	const save = async (patch: Partial<QueueSettings>) => {
		try {
			const state = await updateQueueSettings(localStorage.token, patch);
			value = state.settings;
			dispatch('changed', state);
		} catch (error) {
			toast.error(`${error}`);
		}
	};

	const enableBrowserNotifications = async () => {
		if (permission === 'unsupported') {
			toast.error($i18n.t('This browser does not support notifications.'));
			return;
		}
		permission = await Notification.requestPermission();
		if (permission !== 'granted') {
			toast.error(
				$i18n.t(
					'Notifications are blocked. Allow them for this site in your browser settings, then try again.'
				)
			);
			return;
		}
		settings.set({ ...$settings, notificationEnabled: true });
		await updateUserSettings(localStorage.token, { ui: $settings }).catch(() => null);
		toast.success($i18n.t('Browser notifications are on.'));
	};

	const toggles: { key: keyof QueueSettings; label: string; hint: string }[] = [
		{
			key: 'notify_limit_reached',
			label: 'Limit reached',
			hint: 'When the provider limit stops the queue'
		},
		{
			key: 'notify_item_done',
			label: 'A conversation is finished',
			hint: 'With a preview of the answer and the files changed'
		},
		{ key: 'notify_queue_done', label: 'Everything is finished', hint: 'When the queue is empty' },
		{
			key: 'notify_needs_approval',
			label: 'Approval required',
			hint: 'The assistant wants to run an action you must allow'
		},
		{ key: 'notify_item_failed', label: 'A conversation failed', hint: 'Errors that are not a limit' }
	];
</script>

<div class="flex flex-col gap-5 rounded-2xl border border-gray-100 bg-white p-4 dark:border-gray-850 dark:bg-gray-900">
	<div class="flex items-start justify-between gap-4">
		<div>
			<div class="text-sm font-medium">{$i18n.t('Resume automatically after a limit')}</div>
			<div class="text-[0.75rem] text-gray-500 dark:text-gray-400">
				{$i18n.t(
					'When a conversation stops because a usage limit is reached, send a message to continue it as soon as the limit is over.'
				)}
			</div>
		</div>
		<Switch
			state={value.auto_resume}
			ariaLabel={$i18n.t('Resume automatically after a limit')}
			on:change={(e) => save({ auto_resume: e.detail })}
		/>
	</div>

	<div>
		<label class="mb-1 block text-[0.75rem] font-medium text-gray-500 dark:text-gray-400" for="resume-message">
			{$i18n.t('Message sent to continue')}
		</label>
		<textarea
			id="resume-message"
			class="min-h-16 w-full resize-y rounded-xl border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden dark:border-gray-800"
			bind:value={resumeMessage}
			on:blur={() => resumeMessage.trim() && resumeMessage !== value.resume_message && save({ resume_message: resumeMessage })}
		></textarea>
		<p class="mt-1 text-[0.6875rem] text-gray-400">
			{$i18n.t('If the reply failed before writing anything, the same question is simply asked again instead.')}
		</p>
	</div>

	<div class="border-t border-gray-100 pt-4 dark:border-gray-850">
		<div class="mb-2 flex items-center justify-between">
			<div class="text-sm font-medium">{$i18n.t('Notify me')}</div>
			{#if permission !== 'granted' || !$settings?.notificationEnabled}
				<button
					type="button"
					class="rounded-full border border-gray-200 px-3 py-1 text-xs hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800"
					on:click={enableBrowserNotifications}
				>
					{$i18n.t('Enable browser notifications')}
				</button>
			{:else}
				<span class="text-xs text-emerald-600 dark:text-emerald-400">{$i18n.t('Browser notifications are on.')}</span>
			{/if}
		</div>

		<div class="flex flex-col gap-3">
			{#each toggles as t (t.key)}
				<div class="flex items-center justify-between gap-4">
					<div>
						<div class="text-[0.8125rem]">{$i18n.t(t.label)}</div>
						<div class="text-[0.6875rem] text-gray-400">{$i18n.t(t.hint)}</div>
					</div>
					<Switch
						state={Boolean(value[t.key])}
						ariaLabel={$i18n.t(t.label)}
						on:change={(e) => save({ [t.key]: e.detail })}
					/>
				</div>
			{/each}
		</div>
		<p class="mt-3 text-[0.6875rem] text-gray-400">
			{$i18n.t(
				'Notifications also reach your phone if you set a notification target (webhook) in Settings → Notifications.'
			)}
		</p>
	</div>
</div>
