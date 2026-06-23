/** `/api/web` 응답 타입 — 백엔드 web_api_routes.py 와 형태를 맞춘다. */

export interface MeFlags {
	system_allowed: boolean;
	can_manage_system: boolean;
	can_manage_users: boolean;
	can_manage_credentials: boolean;
	can_archive: boolean;
	can_delete: boolean;
	can_view_archive_logs: boolean;
	can_view_system_logs: boolean;
	can_view_audit_logs: boolean;
	can_view_any_logs: boolean;
	can_search: boolean;
	can_use_api_keys: boolean;
	can_manage_trash: boolean;
}

export interface MeUser {
	email: string;
	display_name: string | null;
	role: string;
	locale: string;
	timezone: string | null;
}

export interface NeedsHumanJob {
	id: number;
	url: string;
}

/** 업데이트 노트 항목 — GitHub Release 의 변경 한 줄. PR 번호가 있으면 링크가 걸린다. */
export interface ReleaseNoteItem {
	text: string;
	pr: number | null;
	url: string | null;
}

/** 로그인 후 1회 표시하는 현재 버전 업데이트 노트 (web_api_routes.release_notes). */
export interface ReleaseNote {
	version: string;
	title: string;
	items: ReleaseNoteItem[];
}

export interface Me {
	auth_enabled: boolean;
	authenticated: boolean;
	user: MeUser | null;
	flags: MeFlags;
	locale: string;
	timezone: string;
	needs_human: NeedsHumanJob[];
	needs_human_count: number;
	version: string;
	release_note: ReleaseNote | null;
}

// ── 미인증 인증 흐름 (/api/web/auth) ──

export interface AuthConfig {
	oidc_enabled: boolean;
	signup_enabled: boolean;
	mail_enabled: boolean;
}

/** 로그인·2단계·가입 응답의 다음 단계. */
export type AuthStatus = 'active' | 'totp' | 'email_verify';

export interface MigrationStatus {
	status: string;
	done?: number;
	total?: number;
	insecure?: boolean;
	error?: string;
	failed?: { path: string; error: string }[];
}

export interface LoginResult {
	status: AuthStatus;
	has_totp?: boolean;
	has_passkey?: boolean;
}

export interface TotpStatus {
	has_totp: boolean;
	has_passkey: boolean;
}

export interface VerifyEmailStatus {
	email: string;
	verified: boolean;
	pending: boolean;
	mail_enabled: boolean;
	ttl_minutes: number;
}

// ── 라이브 챌린지 (/api/web/live) ──

export interface LiveJob {
	id: number;
	url: string;
	needs_human_at: string;
	held_by_other: boolean;
}

export interface LiveMeta {
	id: number;
	url: string;
	owned: boolean;
	viewport_w: number;
	viewport_h: number;
	shot_interval_ms: number;
}

export interface LiveState {
	status: 'needs_human' | 'done';
	owned?: boolean;
}

export interface TrendRow {
	label: string;
	count: number;
	bytes: number;
	pct: number;
}

export interface RecentSnap {
	id: number;
	page_id: number;
	site_id: number | null;
	page_url: string;
	taken_at: string;
	is_first: number;
	changed: number;
	bytes: number;
	network_tag_name: string | null;
	network_tag_description: string | null;
	network_tag_id: number | null;
}

export interface RecentLog {
	started_at: string;
	status: string;
	url: string;
	page_id: number | null;
	page_site_id: number | null;
	snapshot_id: number | null;
	duration_ms: number;
	source: string;
	network_tag_name: string | null;
}

export interface SiteItem {
	site_id: number | null;
	site_key: string;
	page_count: number;
	snapshot_count: number;
	crawl_count: number;
	schedule_count: number;
	bytes: number;
	title: string | null;
	network_tags: { id: number; name: string; description: string | null }[];
	activity_at: string | null;
	crawling: boolean;
	active: boolean;
}

/** 목록 페이저 메타 — 모든 페이징 응답이 공유하는 형태. */
export interface Pager {
	page: number;
	total_pages: number;
	per_page: number;
	total: number;
}

/** 아카이브 사이트 목록 (/api/web/sites) — q(site_key 부분 일치) 필터 + 페이징. */
export interface SitesData {
	items: SiteItem[];
	q: string;
	total: number;
	total_pages: number;
	page_num: number;
	limit: number;
	limits: number[];
}

export interface SnapshotRow {
	id: number;
	page_id: number;
	site_id: number | null;
	taken_at: string;
	changed: number;
	content_hash: string;
	http_status: number | null;
	authenticated: number;
	title: string | null;
	[key: string]: unknown;
}

export interface TimelineSnap {
	idx: number;
	snap: SnapshotRow;
	badge: string;
	files: { name: string; bytes: number }[];
	total_bytes: number | null;
	steps: unknown[];
	log: Record<string, unknown> | null;
}

export interface PageTimeline {
	page: Record<string, unknown> & { id: number; url: string; domain: string; title: string | null };
	site: { id: number; site_key: string } | null;
	network_tag: Record<string, unknown> | null;
	schedule: (Record<string, unknown> & { label: string }) | null;
	snapshots: TimelineSnap[];
	checks: Record<string, unknown>[];
	can_archive: boolean;
	can_delete: boolean;
	trash_enabled: boolean;
}

export interface SnapshotDoc {
	file: string;
	url: string;
	bytes: number;
}

export interface SnapshotMeta {
	snap: SnapshotRow & { domain?: string };
	network_tag: Record<string, unknown> | null;
	title: string | null;
	documents: SnapshotDoc[];
	page_html_url: string;
	screenshot_url: string;
	mobile_screenshot_url: string;
	content_url: string;
	has_screenshot: boolean;
	has_mobile_screenshot: boolean;
}

/** 사이트 상세의 크롤 회차 한 줄 (db.list_site_crawls — 회차 + 상태별 페이지 수 집계). */
export interface SiteCrawl {
	id: number;
	status: string;
	created_at: string;
	finished_at: string | null;
	total_count: number;
	done_count: number;
	failed_count: number;
	pending_count: number;
}

export interface SiteDetail {
	site: Record<string, unknown> & { id: number; site_key: string };
	site_title: string | null;
	pages: (Record<string, unknown> & { id: number; url: string; bytes: number })[];
	page_count: number;
	snapshot_total: number;
	site_bytes: number;
	pager: Pager;
	crawls: SiteCrawl[];
	crawls_pager: Pager;
	schedules: (Record<string, unknown> & { page_id: number; label: string })[];
	crawl_schedules: { start_url: string; label: string; next_run_at: string }[];
	network_tags: Record<string, unknown>[];
	certificates: {
		cert: Record<string, unknown> & {
			id: number;
			host: string;
			subject: string;
			issuer: string;
			serial: string;
			fingerprint: string;
			not_before: string;
			not_after: string;
			signature_algorithm: string | null;
			verified: number;
			first_seen_at: string;
			last_seen_at: string;
		};
		san: string[];
		is_current: boolean;
		pem_url: string;
	}[];
	documents: Record<string, unknown>[];
	doc_total: number;
	failed_items: FailedItem[];
	failed_pager: Pager;
	can_archive: boolean;
	can_delete: boolean;
	can_manage_credentials: boolean;
	trash_enabled: boolean;
}

/** 사이트 상세의 3개 목록만 (/api/web/sites/{id}/lists) — 페이저 in-place 갱신용 린 응답. */
export type SiteLists = Pick<
	SiteDetail,
	'pages' | 'pager' | 'crawls' | 'crawls_pager' | 'failed_items' | 'failed_pager'
>;

export interface FailedItem {
	kind: 'log' | 'crawl';
	id: number;
	url: string;
	at: string | null;
	error: string;
	page_id?: number | null;
	page_url?: string;
	source?: string;
	crawl_id?: number;
}

// ── 사이트 로그인 자격증명 (/api/web/sites/{id}/credentials) ──

export interface SiteCredential {
	id: number;
	label: string;
	kind: string;
	kind_label: string;
	creator_email: string | null;
	created_at: string;
}

export interface CredentialsData {
	site: { id: number; site_key: string };
	credentials: SiteCredential[];
	kinds: { value: string; label: string }[];
	secret_key_configured: boolean;
}

// ── 크롤 회차 (/api/web/crawls/{id}) ──

export interface CrawlCounts {
	total: number;
	pending: number;
	in_progress: number;
	done: number;
	failed: number;
}

export interface CrawlPage {
	id: number;
	url: string;
	depth: number;
	status: string;
	attempts: number;
	next_attempt_at: string | null;
	snapshot_id: number | null;
	snapshot_page_id: number | null;
	snapshot_site_id: number | null;
	error: string | null;
}

export interface CrawlDetail {
	crawl: Record<string, unknown> & {
		id: number;
		start_url: string;
		status: string;
		site_id: number;
		scope_host: string;
		scope_path: string;
		max_pages: number;
		max_depth: number;
		delay_seconds: number;
		created_at: string;
		finished_at: string | null;
	};
	counts: CrawlCounts;
	pages: CrawlPage[];
	network_tag: { id: number; name: string; description: string | null } | null;
	status_filter: string;
	retry_backoff_labels: string[];
	max_attempts: number;
	can_archive: boolean;
}

export type DiffRow = [tag: string, left: string, right: string];

export interface DiffData {
	page: Record<string, unknown> & { id: number; url: string; site_id: number | null };
	added: number;
	removed: number;
	rows: DiffRow[];
	from_idx: number;
	to_idx: number;
	total: number;
	old_snap: SnapshotRow;
	new_snap: SnapshotRow;
	local_capture: boolean;
	old_shot: string;
	new_shot: string;
	shot_ratio: number | null;
	shotdiff_url: string;
}

export interface SearchHit {
	snapshot_id: number;
	page_id: number;
	site_id: number | null;
	page_url: string;
	title: string | null;
	snippet: string;
	taken_at: string;
	changed: number;
	terms: string[];
}

export interface SearchData {
	q: string;
	domain: string;
	latest: boolean;
	available: boolean;
	results: { total: number; mode: string; hits: SearchHit[] } | null;
	page: number;
	total_pages: number;
	per_page: number;
}

export interface DocumentsData {
	groups: (Record<string, unknown> & {
		file: string;
		bytes: number;
		page_id: number;
		site_id: number | null;
		page_url: string;
		snapshot_id: number;
		page_count: number;
		snapshot_count: number;
	})[];
	totals: Record<string, number>;
	page: number;
	has_next: boolean;
	legacy_pending: boolean;
}

export interface ScheduleItem extends Record<string, unknown> {
	label: string;
	page_id: number;
	site_id: number | null;
}

export interface SchedulesData {
	items: ScheduleItem[];
	crawl_items: ScheduleItem[];
	can_archive: boolean;
}

export interface LogItem {
	log: Record<string, unknown> & {
		id: number;
		started_at: string;
		status: string;
		url: string;
		page_id: number | null;
		snapshot_id: number | null;
		page_site_id: number | null;
		duration_ms: number;
		source: string;
	};
	steps: unknown[];
	files: unknown[];
	total_bytes: number | null;
}

export interface LogsData {
	items: LogItem[];
	domains: string[];
	domain: string;
	status: string;
	date_from: string;
	date_to: string;
	snapshot_id: number | null;
	limit: number;
	limits: number[];
	statuses: string[];
	total: number;
	total_pages: number;
	page_num: number;
	can_archive: boolean;
}

export interface SystemLogsData {
	logs: (Record<string, unknown> & {
		id: number;
		created_at: string;
		level: string;
		source: string;
		logger: string;
		message: string;
		traceback: string | null;
	})[];
	level: string;
	source: string;
	date_from: string;
	date_to: string;
	levels: string[];
	sources: string[];
	limit: number;
	limits: number[];
	total: number;
	total_pages: number;
	page_num: number;
}

export interface AuditLogsData {
	logs: {
		id: number;
		created_at: string;
		actor: string;
		actor_user_id: number | null;
		action: string;
		target: string | null;
		message: string;
	}[];
	action: string;
	actor: string;
	date_from: string;
	date_to: string;
	actions: string[];
	action_labels: Record<string, string>;
	actors: string[];
	limit: number;
	limits: number[];
	total: number;
	total_pages: number;
	page_num: number;
}

export interface SystemUser {
	id: number;
	email: string;
	display_name: string | null;
	role: string;
	is_founder: number;
	[key: string]: unknown;
}

export interface SystemUsersData {
	users: SystemUser[];
	invites: (Record<string, unknown> & {
		id: number;
		email: string;
		role: string;
		expires_at: string;
		expired: boolean;
	})[];
	me_id: number | null;
	roles: string[];
	invitable_roles: string[];
	role_labels: Record<string, string>;
	permission_roles: string[];
	permissions_catalog: string[];
	permission_labels: Record<string, string>;
	user_perms: Record<string, { effective: string[]; overridden: string[] }>;
	mail_enabled: boolean;
	invite_ttl_days: number;
	total: number;
	total_pages: number;
	page_num: number;
	limit: number;
	limits: number[];
}

export interface SystemGroup {
	name: string;
	label: string;
	is_builtin: boolean;
	permissions: string[];
	member_count: number;
}

export interface SystemGroupsData {
	groups: SystemGroup[];
	permissions_catalog: string[];
	permission_labels: Record<string, string>;
}

export interface SystemApiKeysData {
	keys: (Record<string, unknown> & {
		id: number;
		name: string;
		can_view: number;
		can_archive: number;
		expires_at: string | null;
	})[];
}

export interface SystemOverview {
	version: string;
	counts: Record<string, number>;
	storage_backend: string; // local | s3 — 사용량 UI 분기
	signup_enabled: boolean;
	signup_default_role: string;
	signup_roles: string[];
	role_labels: Record<string, string>;
	email_verification_enabled: boolean;
	email_verification_ttl_minutes: number;
	email_verification_ttl_limits: { min: number; max: number };
	auth_throttle_enabled: boolean;
	auth_throttle: {
		login_limit: number;
		login_ip_limit: number;
		login_window_minutes: number;
		totp_limit: number;
		email_verify_limit: number;
		email_resend_limit: number;
	};
	auth_throttle_limits: {
		limit_min: number;
		limit_max: number;
		window_min: number;
		window_max: number;
	};
	crawl_defaults: { max_pages: number; max_depth: number; delay: number };
	crawl_retry_backoff: string;
	crawl_limits: { max_pages: number; max_depth: number; min_delay: number; max_delay: number };
	ext_credential_ttl_hours: number;
	ext_credential_ttl_limits: { min: number; max: number };
	mobile_screenshot_enabled: boolean;
	trash_enabled: boolean;
	trash_retention_days: number;
	trash_retention_limits: { min: number; max: number };
	document_limits: { max_count: number; max_mb: number; timeout_seconds: number };
	document_limit_ranges: Record<string, number>;
	network_tags: (Record<string, unknown> & { id: string; name: string })[];
	credential_key_set: boolean;
	smtp_config: Record<string, unknown> & {
		enabled: boolean;
		host: string;
		port: number;
		user: string;
		sender: string;
		tls: string;
		has_password: boolean;
	};
	smtp_tls_modes: string[];
	archive_root: string;
	usage: Record<string, number>;
	optimize_pending: number;
	search: Record<string, unknown>;
	migration_mode: boolean;
	migration_token_created_at: string | null;
	public_url: string | null;
}

export interface TrashEntry {
	id: number;
	kind: 'page' | 'site';
	label: string;
	site_id: number | null;
	page_id: number | null;
	page_count: number;
	snapshot_count: number;
	bytes: number;
	deleted_at: string;
	expires_at: string | null;
	deleted_by_email: string | null;
	deleted_by_name: string | null;
}

export interface TrashData {
	entries: TrashEntry[];
	trash_enabled: boolean;
	retention_days: number;
	total: number;
	total_pages: number;
	page_num: number;
	limit: number;
	limits: number[];
}

export interface Dashboard {
	total_pages: number;
	total_sites: number;
	total_snapshots: number;
	total_bytes: number;
	week_count: number;
	recent_count: number;
	trend: TrendRow[];
	recent_snaps: RecentSnap[];
	recent_logs: RecentLog[];
	version: string;
}

export interface AccountData {
	display_name: string;
	email: string;
	role: string;
	role_label: string;
	is_admin: boolean;
	has_password: boolean;
	totp_enabled: boolean;
	passkey_count: number;
	passkeys: { id: number; name: string; created_at: string; last_used_at: string | null }[];
	email_verified: boolean;
	email_verification_on: boolean;
	timezone: string;
	timezones: string[];
	locale: string;
	locales: string[];
	locale_names: Record<string, string>;
}

export interface PersonalApiKeysData {
	keys: (Record<string, unknown> & {
		id: number;
		name: string;
		can_view: number;
		can_archive: number;
		expires_at: string | null;
	})[];
	can_view: boolean;
	can_archive: boolean;
}

export interface MyArchiveItem {
	log: Record<string, unknown> & {
		started_at: string;
		status: string;
		url: string;
		page_id: number | null;
		page_site_id: number | null;
		snapshot_id: number | null;
		duration_ms: number;
		source: string;
	};
}

export interface MyArchivesData {
	items: MyArchiveItem[];
	status: string;
	limit: number;
	limits: number[];
	statuses: string[];
	total: number;
	total_pages: number;
	page_num: number;
}

// 스토리지(blob 백엔드) 마이그레이션 — /api/web/system/storage/status 응답.
// DB 요약(세션 간 유지)은 정리 대기 배너의 영속 소스다.
export interface StorageMigrationSummary {
	status?: string;
	direction?: string;
	source_backend?: string;
	target_backend?: string;
	source_location?: string;
	cleanup_pending?: boolean;
	total?: number;
	finished_at?: string;
}

export interface StorageStatus {
	status: string; // idle | manifest | copying | partial | done | error
	active_backend: string; // local | s3
	paused: boolean;
	done?: number;
	total?: number;
	failed_count?: number; // 진행 중 누적 실패 수 (라이브)
	workers?: number; // 동시 전송 워커 수
	failed?: { path: string; error: string }[];
	direction?: string;
	source_backend?: string;
	target_backend?: string;
	error?: string | null;
	source_location?: string;
	cleanup_pending?: boolean;
	summary?: StorageMigrationSummary | null;
}

// 복구모드 — /auth/setup/recover/status·/system/recovery/status 응답.
export interface RecoveryMeta {
	baseline_max_id?: number;
	last_id?: number;
	status?: string;
	recovered?: number;
	source_backend?: string;
	finished_at?: string;
}

export interface RecoveryStatus {
	status: string; // idle | scanning | rebuilding | done | error
	done?: number;
	total?: number;
	error?: string | null;
	source_backend?: string;
	restricted_count?: number; // 복구분 중 아직 제한(authenticated=1)인 개수 (배너 기준)
	recovery_meta?: RecoveryMeta | null;
}

// 첫 구동 분류 — /auth/setup 응답 (P5a recovery.classify + 진행 상태).
export interface SetupStatus {
	needed: boolean;
	migration: MigrationStatus;
	token_required: boolean;
	case: string; // operating | data_preserved | restore_s3 | recover_local | recover_s3 | fresh
	has_archive_data?: boolean;
	local_blob?: boolean;
	s3_configured?: boolean;
	s3_blob?: boolean;
	s3_db_backup?: boolean;
	recovery?: RecoveryStatus | null;
}

// 저장 사용량 — /api/web/system/storage/usage 응답 (캐시·로컬, S3 미호출).
export interface S3Usage {
	categories: Record<string, number>; // sites·resources·documents·db-backups·other
	total: number;
	scanned_at: string;
}

export interface StorageUsage {
	backend: string; // local | s3
	local: { db: number; cache: number; blobcache: number } | null; // S3 모드
	s3: S3Usage | null; // 캐시 (미스캔이면 null)
	archive: Record<string, number> | null; // 로컬 모드 (db/sites/resources/documents)
}

// S3 DB 백업 — /api/web/system/db-backup/status 응답.
export interface DbBackupStatus {
	s3_mode: boolean;
	running: boolean;
	interval_hours: number;
	keep: number;
	last_at?: string | null;
	last_status?: string | null;
	last_error?: string | null;
	last_bytes?: number | null;
	count: number;
	backups: { key: string; bytes: number; at: string }[];
	list_error?: string;
}
