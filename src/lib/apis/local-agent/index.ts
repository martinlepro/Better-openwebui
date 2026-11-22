import { WEBUI_API_BASE_URL } from '$lib/constants';

export type LocalAgentInfo = {
	enabled: boolean;
	available: boolean;
	admin_only: boolean;
	roots: string[];
	default_cwd: string | null;
	timeout: number;
	system: string;
	shell: string;
	running_in_docker: boolean;
	approval_policies: string[];
};

export type LocalAgentConfig = {
	enable: boolean;
	admin_only: boolean;
	allowed_roots: string[];
	command_timeout: number;
	max_output: number;
	max_processes: number;
	running_in_docker: boolean;
};

export type LocalAgentProcess = {
	id: string;
	name: string;
	command: string;
	cwd: string;
	status: string;
	pid: number;
	uptime_seconds: number;
};

const request = async <T>(token: string, path: string, init: RequestInit = {}): Promise<T> => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/local-agent${path}`, {
		...init,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	});
	if (!res.ok) {
		let detail: any = null;
		try {
			detail = await res.json();
		} catch {
			// not JSON
		}
		throw detail?.detail ?? detail ?? res.statusText;
	}
	return res.json();
};

export const getLocalAgentInfo = (token: string) => request<LocalAgentInfo>(token, '/info');

export const validateLocalAgentCwd = (token: string, path: string) =>
	request<{ path: string }>(token, '/validate-cwd', {
		method: 'POST',
		body: JSON.stringify({ path })
	});

export const getLocalAgentProcesses = (token: string) =>
	request<{ processes: LocalAgentProcess[] }>(token, '/processes');

export const stopLocalAgentProcess = (token: string, id: string) =>
	request<{ status: boolean }>(token, `/processes/${id}`, { method: 'DELETE' });

export const getLocalAgentConfig = (token: string) => request<LocalAgentConfig>(token, '/config');

export const setLocalAgentConfig = (token: string, form: Partial<LocalAgentConfig>) =>
	request<LocalAgentConfig>(token, '/config', {
		method: 'POST',
		body: JSON.stringify({
			enable: form.enable,
			admin_only: form.admin_only,
			allowed_roots: form.allowed_roots,
			command_timeout: form.command_timeout,
			max_output: form.max_output,
			max_processes: form.max_processes
		})
	});
