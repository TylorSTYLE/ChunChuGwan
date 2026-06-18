<script lang="ts">
	import { base } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { ts } from '$lib/format';
	import { api, ApiError } from '$lib/api';
	import type { CredentialsData } from '$lib/types';

	let { data }: { data: { data: CredentialsData } } = $props();
	const d = $derived(data.data);

	let error = $state('');
	let notice = $state('');
	let busy = $state(false);

	// 등록 폼
	let label = $state('');
	let kind = $state('http_basic');
	let username = $state('');
	let password = $state('');
	let storageState = $state('');
	let token = $state('');
	let harFiles = $state<FileList | null>(null);

	function resetForm() {
		label = username = password = storageState = token = '';
		harFiles = null;
		kind = 'http_basic';
	}

	async function create(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		notice = '';
		try {
			const fd = new FormData();
			fd.set('label', label.trim());
			fd.set('kind', kind);
			if (kind === 'http_basic') {
				fd.set('username', username);
				fd.set('password', password);
			} else if (kind === 'session') {
				fd.set('storage_state', storageState);
				if (harFiles && harFiles[0]) fd.set('har_file', harFiles[0]);
			} else if (kind === 'jwt') {
				fd.set('token', token);
			}
			await api(`/sites/${d.site.id}/credentials`, { method: 'POST', body: fd });
			notice = t('자격증명을 등록했습니다.');
			resetForm();
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function remove(id: number, name: string) {
		if (!confirm(`${name}\n\n${t('이 자격증명을 삭제합니다. 되돌릴 수 없습니다.')}`)) return;
		busy = true;
		error = '';
		notice = '';
		try {
			await api(`/sites/${d.site.id}/credentials/${id}/delete`, { method: 'POST' });
			notice = t('자격증명을 삭제했습니다.');
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

<p class="muted back">
	<a href="{base}/archive/sites/{d.site.id}">← <span class="mono">{d.site.site_key}</span></a>
</p>
<h2>{t('로그인 자격증명')} <span class="mono muted">{d.site.site_key}</span></h2>
{#if notice}<div class="notice">{notice}</div>{/if}
{#if error}<div class="error">{error}</div>{/if}

<p class="muted hint">
	{t('이 사이트를 아카이빙할 때 춘추관이 로그인하는 데 쓸 자격증명입니다. 비밀은 WCCG_SECRET_KEY 로 대칭 암호화해 저장하며, 화면에는 다시 표시되지 않습니다.')}
</p>

{#if !d.secret_key_configured}
	<div class="error">
		<strong>{t('WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 저장할 수 없습니다.')}</strong><br />
		{t('환경변수 WCCG_SECRET_KEY 에 임의의 비밀 문자열을 설정하고 대시보드를 다시 시작하면 등록할 수 있습니다.')}
	</div>
{/if}

<div class="table-wrap wide">
	<table>
		<thead>
			<tr>
				<th>{t('이름')}</th><th>{t('종류')}</th><th>{t('만든 사람')}</th>
				<th>{t('등록')}</th><th></th>
			</tr>
		</thead>
		<tbody>
			{#each d.credentials as c}
				<tr>
					<td>{c.label}</td>
					<td>{t(c.kind_label)}</td>
					<td class="mono muted">{c.creator_email ?? '—'}</td>
					<td class="mono muted">{ts(c.created_at, true)}</td>
					<td>
						<button class="danger" onclick={() => remove(c.id, c.label)} disabled={busy}
							>{t('삭제')}</button
						>
					</td>
				</tr>
			{:else}
				<tr><td colspan="5" class="muted">{t('등록된 자격증명이 없습니다.')}</td></tr>
			{/each}
		</tbody>
	</table>
</div>

<h3>{t('새 자격증명 등록')}</h3>
<form class="cred-form" onsubmit={create}>
	<label
		>{t('이름')}
		<input
			type="text"
			bind:value={label}
			maxlength="50"
			required
			placeholder={t('예: 관리자 계정')}
			disabled={!d.secret_key_configured}
		/>
	</label>
	<label
		>{t('종류')}
		<select bind:value={kind} disabled={!d.secret_key_configured}>
			{#each d.kinds as k}<option value={k.value}>{t(k.label)}</option>{/each}
		</select>
	</label>

	{#if kind === 'http_basic'}
		<label
			>{t('사용자명')}
			<input type="text" bind:value={username} autocomplete="off" disabled={!d.secret_key_configured} />
		</label>
		<label
			>{t('비밀번호')}
			<input
				type="password"
				bind:value={password}
				autocomplete="new-password"
				disabled={!d.secret_key_configured}
			/>
		</label>
	{:else if kind === 'session'}
		<label
			>{t('세션 상태 (storage_state JSON)')}
			<textarea
				bind:value={storageState}
				rows="8"
				spellcheck="false"
				placeholder={'{"cookies": [...], "origins": [...]}'}
				disabled={!d.secret_key_configured}
			></textarea>
		</label>
		<p class="muted hint">
			{t('브라우저에서 로그인한 뒤 Playwright 의 storage_state() 등으로 추출한 JSON 을 붙여넣으세요. 쿠키·localStorage 가 포함됩니다.')}
		</p>
		<label
			>{t('또는 HAR 파일 업로드')}
			<input
				type="file"
				accept=".har,application/json"
				bind:files={harFiles}
				disabled={!d.secret_key_configured}
			/>
		</label>
		<p class="muted hint">
			{t('로그인한 상태로 기록한 HAR 파일(브라우저 개발자도구 네트워크 탭 → 내보내기)을 올리면 쿠키를 자동 추출해 세션 상태로 저장합니다. 이 사이트 도메인의 쿠키만 가져오며, HAR 을 올리면 위 JSON 입력은 무시되고 localStorage 는 포함되지 않습니다.')}
		</p>
	{:else if kind === 'jwt'}
		<label
			>{t('Bearer 토큰')}
			<textarea
				bind:value={token}
				rows="4"
				spellcheck="false"
				autocomplete="off"
				placeholder="eyJhbGciOi…"
				disabled={!d.secret_key_configured}
			></textarea>
		</label>
		<p class="muted hint">
			{t("캡처 시 Authorization: Bearer 헤더로 주입됩니다. 'Bearer ' 접두사 없이 토큰 값만 넣으세요.")}
		</p>
	{/if}

	<button type="submit" disabled={busy || !d.secret_key_configured}>{t('등록')}</button>
</form>

<style>
	.back {
		font-size: 12px;
		margin: 0 0 4px;
	}
	.hint {
		font-size: 12px;
		max-width: 720px;
	}
	.cred-form {
		display: flex;
		flex-direction: column;
		gap: 4px;
		max-width: 640px;
	}
	.cred-form label {
		display: block;
		font-size: 13px;
		margin-top: 8px;
	}
	.cred-form input,
	.cred-form select,
	.cred-form textarea {
		display: block;
		width: 100%;
		margin-top: 3px;
		font: inherit;
		font-size: 13px;
		padding: 5px 10px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--fg);
	}
	.cred-form > button {
		align-self: flex-start;
		margin-top: 12px;
	}
	button.danger {
		color: #fff;
		background: var(--red);
		border-color: var(--red);
	}
</style>
