/**
 * The API client.
 *
 * Every error the server produces has the same shape -- `{"detail": "..."}` --
 * including validation failures, which FastAPI would otherwise return as a
 * list of per-field objects. So one function unwraps it and throws an Error
 * carrying the server's own message, and callers just show `error.message`.
 * That matters here more than usual: the messages are the desktop validator's
 * Chinese wording, like 「不支持的 Xbox 按键：Z」, and they are the most useful
 * thing the user can be told.
 *
 * Authentication is the session cookie, which the browser sends on its own.
 * There is no token in JavaScript anywhere -- the cookie is HttpOnly, so a
 * script cannot read it even in this page, which is what stops an XSS bug from
 * turning into a stolen account.
 */

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(BASE + path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    credentials: "same-origin",
  });
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new ApiError(response.status, body?.detail ?? `请求失败（${response.status}）`);
  }
  return body as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export interface User {
  id: string;
  display_name: string;
  email: string;
  is_admin: boolean;
  created_at: string;
}

export interface Version {
  id: string;
  revision_no: number;
  schema_version: string;
  canonical_sha256: string;
  size_bytes: number;
  parent_version_id: string | null;
  restored_from_id: string | null;
  note: string;
  created_at: string;
}

export type DocType = "profile_selection" | "motion_mappings" | "voice_mappings";
export type Visibility = "private" | "unlisted" | "public";

export interface Profile {
  id: string;
  doc_type: DocType;
  game_id: string | null;
  game_name: string | null;
  title: string;
  summary: string;
  visibility: Visibility;
  owner_id: string;
  owner_name: string;
  current_version: Version | null;
  created_at: string;
  updated_at: string;
}

export interface Game {
  id: string;
  name: string;
}

/** 服务端从存下来的文档生成的说明，不是上传者写的简介。 */
export interface SummaryItem {
  trigger?: string;
  name?: string;
  phrase?: string;
  synonyms?: string[];
  action: string;
  disabled?: boolean;
  enabled?: boolean;
  runtime_zone?: string;
}

export interface SummaryGroup {
  key: string;
  name: string;
  items: SummaryItem[];
}

export interface Summary {
  kind: DocType;
  revision_no: number;
  headline: string;
  selected_id?: string;
  games?: { game_id: string; total: number; groups: SummaryGroup[] }[];
  items?: SummaryItem[];
  wake_word?: string;
  emergency_stop_phrases?: string[];
}

export const api = {
  me: () => request<User>("/auth/me"),
  login: (email: string, password: string) => post<User>("/auth/login", { email, password }),
  logout: () => post<void>("/auth/logout"),
  register: (body: {
    invite_code: string; email: string; password: string; display_name: string;
  }) => post<User>("/auth/register", body),

  games: (q?: string) =>
    request<Game[]>(`/games${q ? `?q=${encodeURIComponent(q)}` : ""}`),

  myProfiles: () => request<Profile[]>("/profiles"),
  publicProfiles: (docType?: string) =>
    request<Profile[]>(`/public/profiles${docType ? `?doc_type=${docType}` : ""}`),
  profile: (id: string) => request<Profile>(`/profiles/${id}`),
  versions: (id: string) => request<Version[]>(`/profiles/${id}/versions`),
  summary: (id: string, versionId: string) =>
    request<Summary>(`/profiles/${id}/versions/${versionId}/summary`),

  createProfile: (body: {
    doc_type: DocType; title: string; document: unknown;
    game_id?: string | null; visibility?: Visibility; summary?: string;
  }) => post<Profile>("/profiles", body),

  addVersion: (id: string, document: unknown, baseVersionId: string | null, note = "") =>
    post<Version>(`/profiles/${id}/versions`, {
      document, base_version_id: baseVersionId, note,
    }),

  rollback: (id: string, versionId: string, note = "") =>
    post<Version>(`/profiles/${id}/rollback`, { version_id: versionId, note }),

  updateProfile: (id: string, body: Partial<Pick<Profile, "title" | "summary" | "visibility" | "game_id">>) =>
    request<Profile>(`/profiles/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  /** The download URL, used as an href so the browser saves the file itself. */
  downloadUrl: (profileId: string, versionId: string) =>
    `${BASE}/profiles/${profileId}/versions/${versionId}/download`,
};

export const DOC_TYPE_NAMES: Record<DocType, string> = {
  profile_selection: "游戏映射",
  motion_mappings: "动作映射",
  voice_mappings: "语音映射",
};

export function formatTime(iso: string): string {
  const date = new Date(iso);
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatSize(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}
