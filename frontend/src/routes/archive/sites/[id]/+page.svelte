<script lang="ts">
	import { pagePath } from '$lib/urls';
	import { resolve } from '$app/paths';
	import type { ResolvedPathname } from '$app/types';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { filesize, ts } from '$lib/format';
	import { api, ApiError, download } from '$lib/api';
	import { filterUrl } from '$lib/filters';
	import { createList } from '$lib/list.svelte';
	import type { SiteDetail, FailedItem, SiteLists } from '$lib/types';
	import AlertBox from '$lib/components/AlertBox.svelte';
	import Notes from '$lib/components/Notes.svelte';
	import StatGrid from '$lib/components/StatGrid.svelte';
	import StatCard from '$lib/components/StatCard.svelte';
	import Pager from '$lib/components/Pager.svelte';
	import PageSize from '$lib/components/PageSize.svelte';
	import Spinner from '$lib/components/Spinner.svelte';
	import { createAction } from '$lib/action.svelte';
	import { Badge, type BadgeVariant } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';

	let { data }: { data: { site: SiteDetail } } = $props();
	const s = $derived(data.site);
	const action = createAction();
	let exporting = $state(false);

	// 크롤 회차 상태 → 색 뱃지·라벨 (crawls/[id] 상세와 동일).
	const STATUS_BADGE: Record<string, BadgeVariant> = {
		running: 'running',
		done: 'new',
		cancelled: 'same'
	};
	const STATUS_LABEL: Record<string, string> = {
		running: '진행 중',
		done: '완료됨',
		cancelled: '취소됨'
	};

	// 페이지·회차·실패 목록만 린 엔드포인트(/lists)로 in-place 페이징한다 — 통계·인증서·스케줄
	// 등 나머지 화면은 data.site 그대로 두고, 목록 테이블·페이저만 교체된다(라우트 재로드 없음).
	// 단일 createList 라 params 가 누적돼 한 목록을 넘겨도 다른 목록 위치가 URL 에 보존된다.
	const LIST_DEF = {
		page: 1,
		per_page: 10,
		crawls_page: 1,
		crawls_per_page: 10,
		failed_page: 1,
		failed_per_page: 10
	};
	const list = createList<SiteLists>({
		source: () => ({
			pages: data.site.pages,
			pager: data.site.pager,
			crawls: data.site.crawls,
			crawls_pager: data.site.crawls_pager,
			failed_items: data.site.failed_items,
			failed_pager: data.site.failed_pager
		}),
		api: () => `/sites/${data.site.site.id}/lists`,
		route: () => `/archive/sites/${data.site.site.id}`,
		params: (b) => ({
			page: b.pager.page,
			per_page: b.pager.per_page,
			crawls_page: b.crawls_pager.page,
			crawls_per_page: b.crawls_pager.per_page,
			failed_page: b.failed_pager.page,
			failed_per_page: b.failed_pager.per_page
		}),
		defaults: LIST_DEF,
		onError: (m) => (action.error = m)
	});
	const ld = $derived(list.data);

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
		action.run(async () => {
			if (f.kind === 'crawl') {
				await api(`/crawls/${f.crawl_id}/pages/${f.id}/retry`, { method: 'POST' });
			} else {
				await api(`/sites/${s.site.id}/failed/${f.id}/retry`, { method: 'POST' });
			}
		}, t('재시도가 등록되었습니다 — 백그라운드에서 진행됩니다.'));

	const retryAllFailed = () =>
		action.run(
			() => api(`/sites/${s.site.id}/failed/retry-all`, { method: 'POST' }),
			t('실패한 작업을 모두 재시도합니다 — 백그라운드에서 진행됩니다.')
		);

	async function exportSite() {
		// 큰 사이트는 서버가 .ccg.export 를 만드는 동안 시간이 걸린다 — 준비중 표시로
		// 중복 클릭(=중복 다운로드)을 막고, 다운로드가 시작되면 알림으로 끝을 알린다.
		action.busy = true;
		exporting = true;
		action.error = '';
		action.notice = '';
		try {
			await download(`/sites/${s.site.id}/export`);
			action.notice = t('내보내기 파일을 다운로드했습니다.');
		} catch (err) {
			action.error = err instanceof ApiError ? err.message : String(err);
		} finally {
			action.busy = false;
			exporting = false;
		}
	}

	async function deleteSite() {
		const msg = s.trash_enabled
			? t('이 사이트의 모든 페이지·스냅샷·크롤·스케줄을 휴지통으로 옮길까요? 휴지통에서 복원할 수 있습니다.')
			: t('이 사이트의 모든 페이지·스냅샷·크롤·스케줄을 삭제할까요? 되돌릴 수 없습니다.');
		if (!confirm(msg)) return;
		action.busy = true;
		action.error = '';
		try {
			await api(`/sites/${s.site.id}/delete`, { method: 'POST' });
			goto(resolve('/archive/list'));
		} catch (err) {
			action.error = err instanceof ApiError ? err.message : String(err);
			action.busy = false;
		}
	}

	// 페이저 href(중간클릭·새 탭용)는 세 목록의 현재 위치를 모두 보존해야 한다.
	const curParams = () => ({
		page: ld.pager.page,
		per_page: ld.pager.per_page,
		crawls_page: ld.crawls_pager.page,
		crawls_per_page: ld.crawls_pager.per_page,
		failed_page: ld.failed_pager.page,
		failed_per_page: ld.failed_pager.per_page
	});
	const listUrl = (patch: Record<string, number>) =>
		filterUrl(`/archive/sites/${s.site.id}`, { ...curParams(), ...patch }, LIST_DEF);
</script>

<h2 class="mono page-key">{s.site.site_key}</h2>
{#if s.site_title}<p class="muted">{s.site_title}</p>{/if}
<AlertBox error={action.error} notice={action.notice} />

<Notes
	kind="site"
	targetId={s.site.id}
	notes={s.notes}
	canView={s.can_memo_view}
	canCreate={s.can_memo_create}
	canDelete={s.can_memo_delete}
/>

<StatGrid>
	<StatCard label={t('페이지')} value={s.page_count} />
	<StatCard label={t('스냅샷')} value={s.snapshot_total} />
	<StatCard label={t('문서')} value={s.doc_total} />
	<StatCard label={t('용량')} value={filesize(s.site_bytes)} />
</StatGrid>

<div class="section-head">
	<h3>{t('페이지')} ({s.page_count})</h3>
	<PageSize value={ld.pager.per_page} onchange={(n) => list.go({ per_page: n, page: 1 })} />
</div>
<div class="table-wrap cards">
	<table>
		<thead>
			<tr><th>URL</th><th>{t('스냅샷')}</th><th>{t('용량')}</th><th>{t('마지막')}</th></tr>
		</thead>
		<tbody>
			{#each ld.pages as p (p.id)}
				<tr>
					<td class="url-cell" data-label="URL"><a href={pagePath(s.site.id, p.id)} title={p.url}>{p.url}</a></td>
					<td class="num" data-label={t('스냅샷')}>{p.snapshot_count ?? '-'}</td>
					<td class="num mono" data-label={t('용량')}>{filesize(p.bytes)}</td>
					<td class="mono" data-label={t('마지막')}>{p.last_snapshot_at ? ts(String(p.last_snapshot_at)) : '-'}</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>
<Pager
	page={ld.pager.page}
	totalPages={ld.pager.total_pages}
	href={(n) => listUrl({ page: n })}
	onpage={(n) => list.go({ page: n })}
	busy={list.busy}
/>

{#if ld.crawls_pager.total > 0}
	<div class="section-head">
		<h3>{t('사이트 아카이브 회차')} ({ld.crawls_pager.total})</h3>
		<PageSize
			value={ld.crawls_pager.per_page}
			onchange={(n) => list.go({ crawls_per_page: n, crawls_page: 1 })}
		/>
	</div>
	<div class="table-wrap cards">
		<table>
			<thead>
				<tr>
					<th>{t('시작')}</th>
					<th>{t('상태')}</th>
					<th class="num">{t('완료')}</th>
					<th class="num">{t('실패')}</th>
					<th class="num">{t('대기')}</th>
				</tr>
			</thead>
			<tbody>
				{#each ld.crawls as c (c.id)}
					<tr>
						<td class="mono" data-label={t('시작')}
							><a href={resolve('/crawls/[id]', { id: String(c.id) })}>{ts(c.created_at)}</a></td
						>
						<td data-label={t('상태')}>
							<Badge variant={STATUS_BADGE[c.status] ?? 'same'}>
								{t(STATUS_LABEL[c.status] ?? c.status)}
							</Badge>
						</td>
						<td class="num mono cnt done" data-label={t('완료')} class:zero={c.done_count === 0}>{c.done_count}</td>
						<td class="num mono cnt fail" data-label={t('실패')} class:zero={c.failed_count === 0}>{c.failed_count}</td>
						<td class="num mono cnt pend" data-label={t('대기')} class:zero={c.pending_count === 0}>{c.pending_count}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	<Pager
		page={ld.crawls_pager.page}
		totalPages={ld.crawls_pager.total_pages}
		href={(n) => listUrl({ crawls_page: n })}
		onpage={(n) => list.go({ crawls_page: n })}
		busy={list.busy}
	/>
{/if}

{#if s.schedules.length > 0 || s.crawl_schedules.length > 0}
	<h3>{t('스케줄')}</h3>
	<ul class="muted">
		{#each s.schedules as sc (sc.page_id)}
			<li><a href={pagePath(s.site.id, sc.page_id)}>{t('페이지')} #{sc.page_id}</a> — {sc.label}</li>
		{/each}
		{#each s.crawl_schedules as cs (cs.start_url)}
			<li class="mono">{cs.start_url} — {cs.label}</li>
		{/each}
	</ul>
{/if}

{#if ld.failed_pager.total > 0}
	<div class="section-head">
		<h3>{t('실패한 작업')} ({ld.failed_pager.total})</h3>
		<div class="head-actions">
			<PageSize
				value={ld.failed_pager.per_page}
				onchange={(n) => list.go({ failed_per_page: n, failed_page: 1 })}
			/>
			{#if s.can_archive}
				<Button variant="outline" size="sm" onclick={retryAllFailed} disabled={action.busy}>{t('모두 재시도')}</Button>
			{/if}
		</div>
	</div>
	<div class="table-wrap cards">
		<table>
			<thead>
				<tr>
					<th>{t('시간')}</th><th>URL</th><th>{t('오류')}</th>
					{#if s.can_archive}<th></th>{/if}
				</tr>
			</thead>
			<tbody>
				{#each ld.failed_items as f (`${f.kind}:${f.id}`)}
					<tr>
						<td class="mono" data-label={t('시간')}>{f.at ? ts(String(f.at)) : '-'}</td>
						<td class="url-cell" data-label="URL">{f.url}</td>
						<td class="muted" data-label={t('오류')}>{f.error}</td>
						{#if s.can_archive}
							<td><Button variant="outline" size="sm" onclick={() => retryFailed(f)} disabled={action.busy}>{t('재시도')}</Button></td>
						{/if}
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	<Pager
		page={ld.failed_pager.page}
		totalPages={ld.failed_pager.total_pages}
		href={(n) => listUrl({ failed_page: n })}
		onpage={(n) => list.go({ failed_page: n })}
		busy={list.busy}
	/>
{/if}

{#if s.certificates.length > 0}
	<h3>{t('TLS 인증서')}</h3>
	<p class="muted cert-note">
		{t(
			'https 아카이빙 때 받은 서버 인증서의 버전 이력입니다. 인증서가 갱신되면 새 버전으로 기록되고 이전 버전은 남습니다.'
		)}
	</p>
	<ul class="certs">
		{#each s.certificates as c (c.cert.id)}
			{@const exp = expiry(String(c.cert.not_after))}
			<li class="cert" class:is-current={c.is_current}>
				<div class="cert-head">
					<span class="mono host">{c.cert.host}</span>
					{#if c.is_current}
						<Badge variant="new">{t('현재')}</Badge>
					{:else}
						<Badge variant="same">{t('이전 버전')}</Badge>
					{/if}
					{#if exp === 'expired'}
						<Badge variant="error">{t('만료됨')}</Badge>
					{:else if exp === 'soon'}
						<Badge variant="changed">{t('곧 만료')}</Badge>
					{/if}
					{#if !c.cert.verified}
						<Badge variant="error" title={t('캡처가 인증서 검증을 통과하지 못했습니다 (자체 서명 등)')}>
							{t('검증 안 됨')}
						</Badge>
					{/if}
					<a href={c.pem_url as ResolvedPathname} class="pem" download>PEM</a>
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
		<a href={resolve('/archive/sites/[id]/credentials', { id: String(s.site.id) })}
			>{t('로그인 자격증명 관리')}</a
		>
		<span class="muted">{t('— 이 사이트 캡처 시 사용할 로그인 정보')}</span>
	</p>
{/if}

{#if s.can_archive}
	<p class="export-link">
		<Button variant="outline" size="sm" class="gap-1.5" onclick={exportSite} disabled={action.busy} aria-busy={exporting}>
			{#if exporting}<Spinner />{t('파일 준비중…')}{:else}{t('이 사이트 내보내기')}{/if}
		</Button>
		<span class="muted">{t('— 이 사이트의 페이지·스냅샷만 담은 .ccg.export 파일')}</span>
	</p>
{/if}

{#if s.can_delete}
	<fieldset class="danger-zone">
		<legend>{t('위험 구역')}</legend>
		<Button variant="destructive" onclick={deleteSite} disabled={action.busy}>{t('이 사이트 삭제')}</Button>
	</fieldset>
{/if}

<style>
	.page-key {
		overflow-wrap: anywhere;
	}
	/* 회차별 완료/실패/대기 수 — 색으로 한눈에 구분, 0 은 흐리게. */
	.cnt.done {
		color: var(--green);
	}
	.cnt.fail {
		color: var(--red-text);
	}
	.cnt.pend {
		color: var(--amber);
	}
	.cnt.zero {
		color: var(--muted);
	}
	.cred-link,
	.export-link {
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
	.section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 12px;
	}
	.head-actions {
		display: flex;
		align-items: center;
		gap: 8px;
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
</style>
