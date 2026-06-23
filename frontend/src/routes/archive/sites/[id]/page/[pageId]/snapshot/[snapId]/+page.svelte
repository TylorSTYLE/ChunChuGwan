<script lang="ts">
	import { pagePath } from '$lib/urls';
	import { base } from '$app/paths';
	import { invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import { api, ApiError } from '$lib/api';
	import { Button } from '$lib/components/ui/button';
	import type { SnapshotMeta, Me } from '$lib/types';

	let { data }: { data: { meta: SnapshotMeta; me: Me | null } } = $props();
	const m = $derived(data.meta);
	// 관리자 전용 — 복구된(또는 로그인 캡처) 스냅샷의 접근 제한(authenticated) 토글.
	const isAdmin = $derived(!!data.me?.flags.can_manage_system);
	const restricted = $derived(!!m.snap.authenticated);
	let authBusy = $state(false);
	let authError = $state('');

	async function toggleAuth() {
		const restrict = !restricted;
		const msg = restrict
			? t('이 스냅샷을 관리자 전용으로 제한할까요?')
			: t('이 스냅샷을 모든 사용자에게 공개할까요? 로그인 캡처였다면 비공개 정보가 노출될 수 있습니다.');
		if (!confirm(msg)) return;
		authBusy = true;
		authError = '';
		try {
			await api(`/system/recovery/snapshot/${m.snap.id}/authenticated`, {
				method: 'POST',
				body: JSON.stringify({ value: restrict })
			});
			await invalidateAll();
		} catch (err) {
			authError = err instanceof ApiError ? err.message : String(err);
		} finally {
			authBusy = false;
		}
	}

	type Tab = 'render' | 'shot' | 'shot-mobile' | 'text';
	let tab = $state<Tab>('render');

	// 텍스트 탭: content.md(정규화 본문)는 아카이빙 HTML 이 아니라 플레인 텍스트라
	// sandbox iframe(원칙 5)이 불필요하다. iframe 에 plain text 를 띄우면 OS 다크모드가
	// 글자색을 반전시키는데 iframe 요소 색은 부모 CSS 로 제어 못 해 흰배경+흰글자로
	// 안 보이는 문제가 있어, 직접 fetch 해서 앱 토큰 색을 입힌 <pre> 로 렌더한다.
	let textContent = $state<string | null>(null);
	let textError = $state(false);

	async function loadText() {
		if (textContent !== null || textError) return;
		try {
			const r = await fetch(m.content_url);
			if (!r.ok) throw new Error(String(r.status));
			textContent = await r.text();
		} catch {
			textError = true;
		}
	}
</script>

<h2>{m.title || t('스냅샷')}</h2>

<table style="max-width:760px">
	<tbody>
		<tr><th>{t('캡처 시각')}</th><td class="mono">{ts(m.snap.taken_at)}</td></tr>
		<tr><th>{t('해시')}</th><td class="mono">{m.snap.content_hash}</td></tr>
		<tr><th>HTTP</th><td class="mono">{m.snap.http_status ?? '-'}</td></tr>
	</tbody>
</table>

{#if isAdmin}
	<div class="auth-control">
		<span class="muted">{t('접근')}:</span>
		<span class="mono">{restricted ? t('관리자 전용 (제한됨)') : t('전체 공개')}</span>
		<Button variant="outline" size="sm" onclick={toggleAuth} disabled={authBusy}>
			{restricted ? t('전체 공개로 전환') : t('관리자 전용으로 제한')}
		</Button>
	</div>
	{#if authError}<p class="auth-err">{authError}</p>{/if}
{/if}

{#if m.documents.length > 0}
	<h3>{t('첨부 문서')} ({m.documents.length})</h3>
	<table style="max-width:760px">
		<thead>
			<tr><th>{t('문서명')}</th><th style="text-align:right">{t('용량')}</th></tr>
		</thead>
		<tbody>
			{#each m.documents as d}
				<tr>
					<td>
						<a href="/snapshot/{m.snap.id}/doc/{d.file}" download title={d.url}>{d.file}</a>
					</td>
					<td class="num mono">{filesize(d.bytes)}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<div class="toolbar">
	<a href={pagePath(m.snap.site_id, m.snap.page_id)}>← {t('타임라인')}</a>
	<button class="tab" class:active={tab === 'render'} onclick={() => (tab = 'render')}
		>{t('렌더링')}</button
	>
	{#if m.has_screenshot}
		<button class="tab" class:active={tab === 'shot'} onclick={() => (tab = 'shot')}
			>{t('데스크탑 스크린샷')}</button
		>
	{/if}
	{#if m.has_mobile_screenshot}
		<button class="tab" class:active={tab === 'shot-mobile'} onclick={() => (tab = 'shot-mobile')}
			>{t('모바일 스크린샷')}</button
		>
	{/if}
	<button class="tab" class:active={tab === 'text'} onclick={() => { tab = 'text'; loadText(); }}>{t('텍스트')}</button>
</div>

<!-- 보안(원칙 5): 허용 sandbox 토큰은 allow-top-navigation-by-user-activation 하나.
     allow-scripts/allow-same-origin 절대 금지. 아카이빙된 JS 는 실행되지 않는다. -->
{#if tab === 'render'}
	<iframe
		sandbox="allow-top-navigation-by-user-activation"
		src={m.page_html_url}
		class="viewer-frame"
		title={t('렌더링')}
	></iframe>
{:else if tab === 'shot' && m.has_screenshot}
	<img src={m.screenshot_url} alt={t('데스크탑 스크린샷')} class="shot" />
{:else if tab === 'shot-mobile' && m.has_mobile_screenshot}
	<img src={m.mobile_screenshot_url} alt={t('모바일 스크린샷')} class="shot shot-mobile" />
{:else if tab === 'text'}
	{#if textError}
		<p class="text-fallback">{t('텍스트를 불러오지 못했습니다.')}</p>
	{:else if textContent === null}
		<p class="text-fallback">{t('불러오는 중…')}</p>
	{:else}
		<pre class="text-view">{textContent}</pre>
	{/if}
{/if}

<style>
	.auth-control {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 8px 0;
		font-size: 13px;
		max-width: 760px;
	}
	.auth-err {
		color: var(--destructive, #b91c1c);
		font-size: 12px;
		margin: 4px 0;
	}
	.viewer-frame {
		width: 100%;
		height: 78vh;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: #fff;
	}
	/* 텍스트 뷰: 앱 토큰 색을 써서 라이트/다크 모두 가독성 보장 (iframe 미사용 — 위 주석). */
	.text-view {
		width: 100%;
		height: 78vh;
		box-sizing: border-box;
		overflow: auto;
		margin: 0;
		padding: 12px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--background);
		color: var(--foreground);
		white-space: pre-wrap;
		word-break: break-word;
		font-family: var(--font-mono);
		font-size: 13px;
		line-height: 1.6;
	}
	.text-fallback {
		padding: 12px;
		color: var(--muted);
	}
	.shot {
		max-width: 100%;
		border: 1px solid var(--border);
	}
	.shot-mobile {
		width: 390px;
		max-width: 100%;
	}
	button.tab.active {
		background: var(--gray-bg);
		border-color: var(--gray);
	}
</style>
