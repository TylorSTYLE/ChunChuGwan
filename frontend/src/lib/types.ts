/** `/api/web` 응답 타입 — 백엔드 web_api_routes.py 와 형태를 맞춘다. */

export interface MeFlags {
	system_allowed: boolean;
	can_manage_system: boolean;
	can_manage_users: boolean;
	can_manage_credentials: boolean;
	can_archive: boolean;
	can_delete: boolean;
	can_view_logs: boolean;
	can_search: boolean;
	can_use_api_keys: boolean;
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
}

// ── 미인증 인증 흐름 (/api/web/auth) ──

export interface AuthConfig {
	oidc_enabled: boolean;
	signup_enabled: boolean;
	mail_enabled: boolean;
}

/** 로그인·2단계·가입 응답의 다음 단계. */
export type AuthStatus = 'active' | 'totp' | 'email_verify';

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

export interface SnapshotRow {
	id: number;
	page_id: number;
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

export interface SiteDetail {
	site: Record<string, unknown> & { id: number; site_key: string };
	site_title: string | null;
	pages: (Record<string, unknown> & { id: number; url: string; bytes: number })[];
	page_count: number;
	snapshot_total: number;
	site_bytes: number;
	pager: { page: number; total_pages: number; per_page: number; total: number };
	crawls: Record<string, unknown>[];
	schedules: (Record<string, unknown> & { page_id: number; label: string })[];
	crawl_schedules: { start_url: string; label: string; next_run_at: string }[];
	network_tags: Record<string, unknown>[];
	documents: Record<string, unknown>[];
	doc_total: number;
	failed_items: Record<string, unknown>[];
	can_archive: boolean;
	can_delete: boolean;
}

export type DiffRow = [tag: string, left: string, right: string];

export interface DiffData {
	page: Record<string, unknown> & { id: number; url: string };
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
}

export interface SchedulesData {
	items: ScheduleItem[];
	crawl_items: ScheduleItem[];
	can_archive: boolean;
}

export interface LogItem {
	log: Record<string, unknown> & {
		started_at: string;
		status: string;
		url: string;
		page_id: number | null;
		snapshot_id: number | null;
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
	invites: (Record<string, unknown> & { id: number; email: string; role: string })[];
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
	signup_enabled: boolean;
	signup_default_role: string;
	signup_roles: string[];
	role_labels: Record<string, string>;
	email_verification_enabled: boolean;
	email_verification_ttl_minutes: number;
	email_verification_ttl_limits: { min: number; max: number };
	crawl_defaults: { max_pages: number; max_depth: number; delay: number };
	crawl_retry_backoff: string;
	crawl_limits: Record<string, number>;
	ext_credential_ttl_hours: number;
	ext_credential_ttl_limits: { min: number; max: number };
	mobile_screenshot_enabled: boolean;
	document_limits: { max_count: number; max_mb: number; timeout_seconds: number };
	document_limit_ranges: Record<string, number>;
	network_tags: (Record<string, unknown> & { id: string; name: string })[];
	credential_key_set: boolean;
	smtp_config: Record<string, unknown> & { enabled: boolean; host: string };
	smtp_tls_modes: string[];
	archive_root: string;
	usage: Record<string, number>;
	optimize_pending: number;
	search: Record<string, unknown>;
	migration_mode: boolean;
	migration_token_created_at: string | null;
	public_url: string | null;
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
