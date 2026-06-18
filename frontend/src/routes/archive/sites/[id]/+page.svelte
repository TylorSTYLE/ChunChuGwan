<script lang="ts">
	import { pagePath } from '$lib/urls';
	import { base } from '$app/paths';
	import { goto, invalidateAll } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import { api, ApiError, download } from '$lib/api';
	import type { SiteDetail, FailedItem } from '$lib/types';

	let { data }: { data: { site: SiteDetail } } = $props();
	const s = $derived(data.site);

	let busy = $state(false);
	let exporting = $state(false);
	let error = $state('');
	let notice = $state('');

	async function act(fn: () => Promise<void>, ok: string) {
		busy = true;
		error = '';
		notice = '';
		try {
			await fn();
			notice = t(ok);
			await invalidateAll();
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	/** 인증서 만료 상태 — 현재 시각 기준 (만료 30일 전부터 '곧 만료'). */
	function expiry(notAfter: string): 'expired' | 'soon' | 'ok' {
		const end = new Date(notAfter).getTime();
		if (isNaN(end)) return 'ok';
		const now = Date.now();
		if (end < now) return 'expired';
		if (end < now + 30 * 24 * 3600 * 1000) return 'soon';
		return 'ok';
	}

	const retryFailed = (f: FailedItem) =>
		act(async () => {
			if (f.kind === 'crawl') {
				await api(`/crawls/${f.crawl_id}/pages/${f.id}/retry`, { method: 'POST' });
			} else {
				await api(`/sites/${s.site.id}/failed/${f.id}/retry`, { method: 'POST' });
			}
		}, '재시도가 등록되었습니다 — 백그라운드에서 진행됩니다.');

	const retryAllFailed = () =>
		act(async () => {
			await api(`/sites/${s.site.id}/failed/retry-all`, { method: 'POST' });
		}, '실패한 작업을 모두 재시도합니다 — 백그라운드에서 진행됩니다.');

	async function exportSite() {
		// 큰 사이트는 서버가 .ccg.export 를 만드는 동안 시간이 걸린다 — 준비중 표시로
		// 중복 클릭(=중복 다운로드)을 막고, 다운로드가 시작되면 알림으로 끝을 알린다.
		busy = true;
		exporting = true;
		error = '';
		notice = '';
		try {
			await download(`/sites/${s.site.id}/export`);
			notice = t('내보내기 파일을 다운로드했습니다.');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
		} finally {
			busy = false;
			exporting = false;
		}
	}

	async function deleteSite() {
		if (!confirm(t('이 사이트의 모든 페이지·스냅샷·크롤·스케줄을 삭제할까요? 되돌릴 수 없습니다.')))
			return;
		busy = true;
		error = '';
		try {
			await api(`/sites/${s.site.id}/delete`, { method: 'POST' });
			goto(`${base}/archive/list`);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err);
			busy = false;
		}
	}
</script>

<h2 class="mono">{s.site.site_key}</h2>
{#if s.site_title}<p class="muted">{s.site_title}</p>{/if}
{#if error}<div class="error">{error}</div>{/if}
{#if notice}<div class="notice">{notice}</div>{/if}

<div class="stat-grid">
	<div class="stat-card">
		<div class="label">{t('페이지')}</div>
		<div class="value">{s.page_count}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('스냅샷')}</div>
		<div class="value">{s.snapshot_total}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('문서')}</div>
		<div class="value">{s.doc_total}</div>
	</div>
	<div class="stat-card">
		<div class="label">{t('용량')}</div>
		<div class="value">{filesize(s.site_bytes)}</div>
	</div>
</div>

<h3>{t('페이지')} ({s.page_count})</h3>
<div class="table-wrap">
	<table>
		<thead>
			<tr><th>URL</th><th>{t('스냅샷')}</th><th>{t('용량')}</th><th>{t('마지막')}</th></tr>
		</thead>
		<tbody>
			{#each s.pages as p}
				<tr>
					<td class="url-cell"><a href={pagePath(s.site.id, p.id)} title={p.url}>{p.url}</a></td>
					<td class="num">{p.snapshot_count ?? '-'}</td>
					<td class="num mono">{filesize(p.bytes)}</td>
					<td class="mono">{p.last_snapshot_at ? ts(String(p.last_snapshot_at)) : '-'}</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>
{#if s.pager.total_pages > 1}
	<div class="pager">
		{#if s.pager.page > 1}
			<a href="{base}/archive/sites/{s.site.id}?page={s.pager.page - 1}">← {t('이전')}</a>
		{/if}
		<span class="muted">{s.pager.page} / {s.pager.total_pages}</span>
		{#if s.pager.page < s.pager.total_pages}
			<a href="{base}/archive/sites/{s.site.id}?page={s.pager.page + 1}">{t('다음')} →</a>
		{/if}
	</div>
{/if}

{#if s.crawls.length > 0}
	<h3>{t('사이트 아카이브 회차')} ({s.crawls.length})</h3>
	<div class="table-wrap">
		<table>
			<thead><tr><th>{t('시작')}</th><th>{t('상태')}</th><th>{t('완료/실패/대기')}</th></tr></thead>
			<tbody>
				{#each s.crawls as c}
					<tr>
						<td class="mono"
							><a href="{base}/crawls/{c.id}">{ts(String(c.started_at))}</a></td
						>
						<td>{String(c.status)}</td>
						<td class="num mono">{c.done_count}/{c.failed_count}/{c.pending_count}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

{#if s.schedules.length > 0 || s.crawl_schedules.length > 0}
	<h3>{t('스케줄')}</h3>
	<ul class="muted">
		{#each s.schedules as sc}
			<li><a href={pagePath(s.site.id, sc.page_id)}>{t('페이지')} #{sc.page_id}</a> — {sc.label}</li>
		{/each}
		{#each s.crawl_schedules as cs}
			<li class="mono">{cs.start_url} — {cs.label}</li>
		{/each}
	</ul>
{/if}

{#if s.failed_items.length > 0}
	<div class="section-head">
		<h3>{t('실패한 작업')} ({s.failed_items.length})</h3>
		{#if s.can_archive}
			<button onclick={retryAllFailed} disabled={busy}>{t('모두 재시도')}</button>
		{/if}
	</div>
	<div class="table-wrap">
		<table>
			<thead>
				<tr>
					<th>{t('시간')}</th><th>URL</th><th>{t('오류')}</th>
					{#if s.can_archive}<th></th>{/if}
				</tr>
			</thead>
			<tbody>
				{#each s.failed_items as f}
					<tr>
						<td class="mono">{f.at ? ts(String(f.at)) : '-'}</td>
						<td class="url-cell">{f.url}</td>
						<td class="muted">{f.error}</td>
						{#if s.can_archive}
							<td>
								<button onclick={() => retryFailed(f)} disabled={busy}>{t('재시도')}</button>
							</td>
						{/if}
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

{#if s.certificates.length > 0}
	<h3>{t('TLS 인증서')}</h3>
	<p class="muted cert-note">
		{t(
			'https 아카이빙 때 받은 서버 인증서의 버전 이력입니다. 인증서가 갱신되면 새 버전으로 기록되고 이전 버전은 남습니다.'
		)}
	</p>
	<ul class="certs">
		{#each s.certificates as c}
			{@const exp = expiry(String(c.cert.not_after))}
			<li class="cert" class:is-current={c.is_current}>
				<div class="cert-head">
					<span class="mono host">{c.cert.host}</span>
					{#if c.is_current}
						<span class="badge new">{t('현재')}</span>
					{:else}
						<span class="badge same">{t('이전 버전')}</span>
					{/if}
					{#if exp === 'expired'}
						<span class="badge error">{t('만료됨')}</span>
					{:else if exp === 'soon'}
						<span class="badge changed">{t('곧 만료')}</span>
					{/if}
					{#if !c.cert.verified}
						<span
							class="badge error"
							title={t('캡처가 인증서 검증을 통과하지 못했습니다 (자체 서명 등)')}
						>{t('검증 안 됨')}</span>
					{/if}
					<a href={c.pem_url} class="pem" download>PEM</a>
				</div>
				<dl class="cert-fields">
					<dt>{t('주체')}</dt>
					<dd class="mono">{c.cert.subject}</dd>
					<dt>{t('발급자')}</dt>
					<dd class="mono">{c.cert.issuer}</dd>
					{#if c.san.length > 0}
						<dt>{t('대체 이름')}</dt>
						<dd class="mono wrap">{c.san.join(', ')}</dd>
					{/if}
					<dt>{t('유효 기간')}</dt>
					<dd class="mono" class:expired={exp === 'expired'}>
						{ts(String(c.cert.not_before))} ~ {ts(String(c.cert.not_after))}
					</dd>
					<dt>{t('확인 기간')}</dt>
					<dd class="mono">
						{ts(String(c.cert.first_seen_at))}{#if c.cert.last_seen_at !== c.cert.first_seen_at}
							~ {ts(String(c.cert.last_seen_at))}{/if}
					</dd>
					<dt>{t('일련번호')}</dt>
					<dd class="mono wrap">{c.cert.serial}</dd>
					{#if c.cert.signature_algorithm}
						<dt>{t('서명 알고리즘')}</dt>
						<dd class="mono">{c.cert.signature_algorithm}</dd>
					{/if}
					<dt>{t('지문')}</dt>
					<dd class="mono wrap">{c.cert.fingerprint}</dd>
				</dl>
			</li>
		{/each}
	</ul>
{/if}

{#if s.can_manage_credentials}
	<p class="cred-link">
		<a href="{base}/archive/sites/{s.site.id}/credentials">{t('로그인 자격증명 관리')}</a>
		<span class="muted">{t('— 이 사이트 캡처 시 사용할 로그인 정보')}</span>
	</p>
{/if}

{#if s.can_archive}
	<p class="export-link">
		<button onclick={exportSite} disabled={busy} aria-busy={exporting}>
			{#if exporting}<span class="spinner" aria-hidden="true"></span>{t('파일 준비중…')}{:else}{t('이 사이트 내보내기')}{/if}
		</button>
		<span class="muted">{t('— 이 사이트의 페이지·스냅샷만 담은 .ccg.export 파일')}</span>
	</p>
{/if}

{#if s.can_delete}
	<fieldset class="danger-zone">
		<legend>{t('위험 구역')}</legend>
		<button class="danger" onclick={deleteSite} disabled={busy}>{t('이 사이트 삭제')}</button>
	</fieldset>
{/if}

<style>
	.cred-link {
		font-size: 13px;
		margin: 16px 0 0;
	}
	.cert-note {
		font-size: 13px;
		margin: 0 0 8px;
	}
	.certs {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.cert {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 10px 12px;
	}
	.cert.is-current {
		border-color: var(--green);
	}
	.cert-head {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
		margin-bottom: 8px;
	}
	.cert-head .host {
		font-weight: 600;
		font-size: 13px;
	}
	.cert-head .pem {
		margin-left: auto;
		font-size: 13px;
	}
	.cert-fields {
		display: grid;
		grid-template-columns: max-content minmax(0, 1fr);
		gap: 3px 14px;
		margin: 0;
		font-size: 13px;
	}
	.cert-fields dt {
		color: var(--muted);
		white-space: nowrap;
	}
	.cert-fields dd {
		margin: 0;
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.cert-fields dd.wrap {
		word-break: break-all;
	}
	.cert-fields dd.expired {
		color: var(--red-text);
	}
	td.url-cell {
		max-width: 420px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pager {
		display: flex;
		gap: 12px;
		align-items: center;
		margin-top: 10px;
		font-size: 13px;
	}
	.error {
		background: var(--red-bg);
		color: var(--red-text);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
	.notice {
		background: var(--green-bg);
		color: var(--green);
		border-radius: 4px;
		padding: 8px 12px;
		margin-bottom: 12px;
		font-size: 13px;
	}
	.section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}
	.export-link {
		font-size: 13px;
		margin: 16px 0 0;
	}
	.export-link button {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.spinner {
		width: 11px;
		height: 11px;
		border: 2px solid var(--border);
		border-top-color: var(--fg);
		border-radius: 50%;
		animation: export-spin 0.7s linear infinite;
	}
	@keyframes export-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation-duration: 2.5s;
		}
	}
	.danger-zone {
		border: 1px solid var(--red);
		border-radius: 6px;
		margin-top: 28px;
		padding: 10px 14px 14px;
	}
	.danger-zone legend {
		color: var(--red);
		font-size: 12px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0 4px;
	}
	button.danger {
		color: #fff;
		background: var(--red);
		border-color: var(--red);
	}
	button.danger:hover {
		background: var(--red-hover);
	}
</style>
