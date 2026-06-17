<script lang="ts">
	import { base } from '$app/paths';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import type { SnapshotMeta } from '$lib/types';

	let { data }: { data: { meta: SnapshotMeta } } = $props();
	const m = $derived(data.meta);

	type Tab = 'render' | 'shot' | 'shot-mobile' | 'text';
	let tab = $state<Tab>('render');
</script>

<h2>{m.title || t('스냅샷')}</h2>

<table style="max-width:760px">
	<tbody>
		<tr><th>{t('캡처 시각')}</th><td class="mono">{ts(m.snap.taken_at)}</td></tr>
		<tr><th>{t('해시')}</th><td class="mono">{m.snap.content_hash}</td></tr>
		<tr><th>HTTP</th><td class="mono">{m.snap.http_status ?? '-'}</td></tr>
	</tbody>
</table>

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
	<a href="{base}/page/{m.snap.page_id}">← {t('타임라인')}</a>
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
	<button class="tab" class:active={tab === 'text'} onclick={() => (tab = 'text')}>{t('텍스트')}</button>
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
	<iframe sandbox="" src={m.content_url} class="viewer-frame" title={t('텍스트')}></iframe>
{/if}

<style>
	.viewer-frame {
		width: 100%;
		height: 78vh;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: #fff;
	}
	.shot {
		max-width: 100%;
		border: 1px solid var(--border);
	}
	.shot-mobile {
		width: 390px;
	}
	button.tab.active {
		background: var(--gray-bg);
		border-color: var(--gray);
	}
</style>
