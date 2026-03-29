export type Envelope<T> = {
  success: boolean
  message: string
  data: T
}

export type Post = {
  id: number
  post_id: string
  title: string
  published_at: string | null
  detail_url: string
  cover_url: string | null
  mega_url: string | null
  status: string
  last_error: string | null
  archive_path: string | null
  extract_dir: string | null
  can_open_local_path: boolean
  updated_at: string
}

export type Task = {
  id: string
  kind: string
  status: string
  refresh_mode: 'incremental' | 'full' | null
  post_id: number | null
  message: string | null
  error: string | null
  log: string | null
  progress_current: number | null
  progress_total: number | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export type Settings = {
  creator_url: string
  profile_dir: string
  download_dir: string
  library_dir: string
  temp_dir: string
  mega_command: string
  playwright_channel: string
  refresh_interval_minutes: number
  download_concurrency: number
  posts_per_row: number
  auto_delete_archive: boolean
}

export type AuthStatus = {
  authenticated: boolean
  profile_exists: boolean
  reason: string | null
}

export type TaskResponse = {
  task_id?: string | null
  task_ids?: string[] | null
  task_status?: string | null
  last_error?: string | null
}

export type TaskClearResult = {
  deleted_count: number
}

export type RefreshMode = 'incremental' | 'full'

export type ArchiveActionResult = {
  deleted_count: number
  deleted_paths: string[]
}

export type OpenPathResult = {
  opened_path: string
}
