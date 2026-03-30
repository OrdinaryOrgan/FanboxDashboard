import type {
  ArchiveActionResult,
  AuthStatus,
  Envelope,
  OpenPathResult,
  Post,
  RefreshMode,
  Settings,
  Task,
  TaskClearResult,
  TitleAnnotationCacheClearResult,
  TitleAliasUpsertRequest,
  TitleAliasUpsertResult,
  TitleAliasesClearResult,
  TaskResponse,
} from './types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: {
      'Content-Type': 'application/json',
    },
    ...options,
  })

  if (!response.ok) {
    const fallback = `Request failed: ${response.status}`
    const payload = await response.text()

    if (payload) {
      try {
        const parsed = JSON.parse(payload) as { detail?: string; message?: string }
        throw new Error(parsed.detail ?? parsed.message ?? fallback)
      } catch {
        throw new Error(payload || fallback)
      }
    }

    throw new Error(fallback)
  }

  const envelope = (await response.json()) as Envelope<T>
  return envelope.data
}

export const api = {
  health: () => request<{ status: string }>('/health'),
  getPosts: () => request<Post[]>('/api/posts'),
  refreshPosts: (mode: RefreshMode = 'incremental') =>
    request<TaskResponse>('/api/posts/refresh', {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),
  getTasks: () => request<Task[]>('/api/tasks'),
  clearTasks: () => request<TaskClearResult>('/api/tasks/clear', { method: 'POST' }),
  getSettings: () => request<Settings>('/api/settings'),
  saveSettings: (payload: Settings) =>
    request<Settings>('/api/settings', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getAuthStatus: () => request<AuthStatus>('/api/auth/status'),
  openLogin: () => request<TaskResponse>('/api/auth/open-login', { method: 'POST' }),
  rescanLibrary: () => request<TaskResponse>('/api/library/rescan', { method: 'POST' }),
  downloadPosts: (postIds: string[]) =>
    request<TaskResponse>('/api/tasks/download', {
      method: 'POST',
      body: JSON.stringify({ post_ids: postIds }),
    }),
  retryTask: (taskId: string) =>
    request<TaskResponse>('/api/tasks/retry', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId }),
    }),
  deleteArchive: (postId: string) =>
    request<ArchiveActionResult>('/api/archives/delete', {
      method: 'POST',
      body: JSON.stringify({ post_id: postId }),
    }),
  openLocalPath: (postId: string) =>
    request<OpenPathResult>('/api/posts/open-local-path', {
      method: 'POST',
      body: JSON.stringify({ post_id: postId }),
    }),
  openYearFolder: (year: string) =>
    request<OpenPathResult>('/api/library/open-year', {
      method: 'POST',
      body: JSON.stringify({ year }),
    }),
  openTitleAliasesPath: () => request<OpenPathResult>('/api/title-aliases/open', { method: 'POST' }),
  clearTitleAliases: () => request<TitleAliasesClearResult>('/api/title-aliases/clear', { method: 'POST' }),
  upsertTitleAliases: (payload: TitleAliasUpsertRequest) =>
    request<TitleAliasUpsertResult>('/api/title-aliases/upsert', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  clearTitleAnnotationCache: () =>
    request<TitleAnnotationCacheClearResult>('/api/title-annotations/clear-cache', { method: 'POST' }),
  purgeArchives: (year?: string | null) =>
    request<ArchiveActionResult>('/api/archives/purge', {
      method: 'POST',
      body: JSON.stringify({ year: year ?? null }),
    }),
}
