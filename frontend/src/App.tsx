import { startTransition, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, MouseEvent as ReactMouseEvent } from 'react'
import {
  Alert,
  App as AntdApp,
  Button,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  InputNumber,
  Layout,
  Modal,
  Popconfirm,
  Select,
  Slider,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { MenuProps } from 'antd'
import {
  AppstoreOutlined,
  CheckOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  DownloadOutlined,
  EllipsisOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  FolderOpenOutlined,
  LeftOutlined,
  LoginOutlined,
  MoonOutlined,
  ReloadOutlined,
  RightOutlined,
  SettingOutlined,
  SunOutlined,
  SyncOutlined,
  UnorderedListOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'

import { api } from './api'
import type { Post, RefreshMode, Settings, Task } from './types'

dayjs.extend(utc)

const { Header, Content } = Layout
const { Title, Paragraph, Text } = Typography

const STATUS_COLORS: Record<string, string> = {
  completed: 'green',
  missing_local: 'gold',
  queued: 'blue',
  running_download: 'processing',
  running_extract: 'processing',
  running_rename: 'processing',
  running_refresh: 'processing',
  failed_parse: 'red',
  failed_download: 'red',
  failed_extract: 'red',
  failed_rename: 'red',
  failed_auth: 'volcano',
  new: 'default',
}

const POST_STATUS_META: Record<string, { label: string; tone: string }> = {
  completed: { label: '已入库', tone: 'success' },
  missing_local: { label: '待补档', tone: 'warning' },
  queued: { label: '排队中', tone: 'info' },
  running_download: { label: '下载中', tone: 'info' },
  running_extract: { label: '解压中', tone: 'info' },
  running_rename: { label: '整理中', tone: 'info' },
  running_refresh: { label: '同步中', tone: 'info' },
  failed_parse: { label: '解析失败', tone: 'danger' },
  failed_download: { label: '下载失败', tone: 'danger' },
  failed_extract: { label: '解压失败', tone: 'danger' },
  failed_rename: { label: '重命名失败', tone: 'danger' },
  failed_auth: { label: '登录失效', tone: 'danger' },
  new: { label: '新发现', tone: 'neutral' },
}

type Panel = 'settings' | 'archive' | null
type ThemeMode = 'light' | 'dark'
type SettingsFormValues = Settings & {
  follow_system_theme: boolean
}
type AppProps = {
  resolvedThemeMode: ThemeMode
  followSystemTheme: boolean
  onToggleTheme: () => void
  onFollowSystemThemeChange: (value: boolean) => void
}

const FLOATING_PANEL_EXIT_DELAY_MS = 190
const COVER_READY_TIMEOUT_MS = 900
const HAS_TIMEZONE_SUFFIX = /([zZ]|[+-]\d{2}:\d{2})$/
const MIN_TASK_DRAWER_WIDTH = 560
const MAX_TASK_DRAWER_WIDTH = 1080
const MIN_TASK_TABLE_SCROLL_WIDTH = 780
const ACTIVE_TASK_STATUSES = new Set([
  'queued',
  'running_download',
  'running_extract',
  'running_rename',
  'running_refresh',
  'running_login',
])
const toLocalTime = (value: string | null) => {
  if (!value) return null
  return HAS_TIMEZONE_SUFFIX.test(value) ? dayjs(value).local() : dayjs.utc(value).local()
}
const yearOf = (post: Post) => (post.published_at ? toLocalTime(post.published_at)?.format('YYYY') ?? null : null)
const formatPostDate = (value: string | null) => (value ? (toLocalTime(value)?.format('YY.MM.DD') ?? '--.--.--') : '--.--.--')
const formatTaskTimestamp = (value: string | null) => (value ? (toLocalTime(value)?.format('MM.DD HH:mm') ?? '--') : '--')
const getOptimalTaskDrawerWidth = () => {
  if (typeof window === 'undefined') return 860
  return clampTaskDrawerWidth(Math.max(820, Math.floor(window.innerWidth * 0.72)))
}
const clampTaskDrawerWidth = (width: number) => {
  if (typeof window === 'undefined') return Math.max(MIN_TASK_DRAWER_WIDTH, Math.min(MAX_TASK_DRAWER_WIDTH, width))
  const max = Math.max(460, Math.min(MAX_TASK_DRAWER_WIDTH, window.innerWidth - 36))
  const min = Math.min(MIN_TASK_DRAWER_WIDTH, max)
  return Math.min(max, Math.max(min, width))
}
const hasActiveTask = (task: Task) => ACTIVE_TASK_STATUSES.has(task.status)
const getTasksRefetchInterval = (isDrawerOpen: boolean, currentTasks: Task[] | undefined) => {
  const hasActiveTasks = currentTasks?.some(hasActiveTask) ?? false

  if (hasActiveTasks) return isDrawerOpen ? 3_000 : 8_000
  return isDrawerOpen ? 15_000 : 30_000
}
const getOptimalTaskDrawerPageSize = () => {
  if (typeof window === 'undefined') return 10
  return Math.max(8, Math.min(14, Math.floor((window.innerHeight - 250) / 64)))
}

function getTaskKindLabel(task: Task) {
  if (task.kind === 'refresh_posts') {
    return task.refresh_mode === 'full' ? '全量校准' : '增量刷新'
  }
  if (task.kind === 'rescan_library') return '本地扫描'
  if (task.kind === 'download_post') return '补档帖子'
  if (task.kind === 'open_login') return '打开登录窗口'
  if (task.kind === 'extract_archive') return '解压压缩包'
  if (task.kind === 'rename_files') return '整理文件'
  return task.kind
}

function getTaskStatusLabel(task: Task) {
  if (task.status === 'queued') {
    if (task.kind === 'refresh_posts') return '等待刷新'
    if (task.kind === 'rescan_library') return '等待扫描'
    if (task.kind === 'download_post') return '等待下载'
    if (task.kind === 'extract_archive') return '等待解压'
    if (task.kind === 'rename_files') return '等待整理'
    if (task.kind === 'open_login') return '等待登录'
    return '等待执行'
  }
  if (task.status === 'completed') return '已完成'
  if (task.status === 'running_refresh') {
    if (task.kind === 'rescan_library') return '扫描中'
    return '刷新中'
  }
  if (task.status === 'running_download') return '下载中'
  if (task.status === 'running_extract') return '解压中'
  if (task.status === 'running_rename') return '整理中'
  if (task.status === 'running_login') return '登录中'
  if (task.status === 'failed_download') return '下载失败'
  if (task.status === 'failed_extract') return '解压失败'
  if (task.status === 'failed_rename') return '整理失败'
  if (task.status === 'failed_auth') return '登录失败'
  if (task.status === 'failed_parse') {
    if (task.kind === 'rescan_library') return '扫描失败'
    if (task.kind === 'refresh_posts') return '刷新失败'
    return '解析失败'
  }
  return task.status
}

function getTaskProgressLabel(task: Task) {
  if (task.kind === 'refresh_posts') {
    if (task.refresh_mode === 'full') {
      if (task.progress_current !== null && task.progress_total !== null) {
        return `已校准 ${task.progress_current} / ${task.progress_total}`
      }
      if (task.progress_current !== null) {
        return `已校准 ${task.progress_current} 页`
      }
      return task.status === 'completed' ? '校准完成' : '正在校准帖子'
    }
    if (task.progress_current !== null) {
      return `已检查 ${task.progress_current} 页`
    }
    return task.status === 'completed' ? '刷新完成' : '正在检查最新页面'
  }

  if (task.kind === 'rescan_library') {
    if (task.status === 'queued') return '等待扫描本地图库'
    if (task.status === 'completed') return '本地扫描完成'
    if (task.status.startsWith('failed')) return '本地扫描失败'
    return '正在扫描本地图库'
  }

  if (task.kind === 'download_post') {
    if (task.status === 'queued') return '等待下载压缩包'
    if (task.status === 'running_download') return '正在下载压缩包'
    if (task.status === 'running_extract') return '下载完成，正在解压'
    if (task.status === 'running_rename') return '解压完成，正在整理'
    if (task.status === 'completed') return '已入库'
    if (task.status === 'failed_download') return '下载压缩包失败'
    if (task.status === 'failed_extract') return '解压失败，请查看日志'
    if (task.status === 'failed_rename') return '整理失败，请查看日志'
  }

  if (task.kind === 'extract_archive') {
    if (task.status === 'queued') return '等待解压压缩包'
    if (task.status === 'completed') return '压缩包解压完成'
    if (task.status.startsWith('failed')) return '压缩包解压失败'
    return '正在解压压缩包'
  }

  if (task.kind === 'rename_files') {
    if (task.status === 'queued') return '等待整理文件'
    if (task.status === 'completed') return '文件整理完成'
    if (task.status.startsWith('failed')) return '文件整理失败'
    return '正在整理文件'
  }

  if (task.kind === 'open_login') {
    if (task.status === 'queued') return '等待打开登录窗口'
    if (task.status === 'completed') return '登录窗口已打开'
    if (task.status.startsWith('failed')) return '打开登录窗口失败'
    return '正在打开登录窗口'
  }

  if (task.status === 'completed') return '任务已完成'
  if (task.status.startsWith('failed')) return '执行失败，请查看日志'
  if (task.status.startsWith('running')) return '正在执行'
  if (task.status === 'queued') return '等待开始'
  return '暂无进展'
}

function getVisiblePageNumbers(currentPage: number, totalPages: number) {
  if (totalPages <= 5) {
    return Array.from({ length: totalPages }, (_, index) => index + 1)
  }
  const start = Math.max(1, Math.min(currentPage - 2, totalPages - 4))
  return Array.from({ length: 5 }, (_, index) => start + index)
}

async function copyTextToClipboard(value: string) {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value)
    return
  }

  if (typeof document === 'undefined') {
    throw new Error('Clipboard unavailable')
  }

  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'absolute'
  textarea.style.left = '-9999px'
  document.body.appendChild(textarea)
  textarea.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(textarea)

  if (!copied) {
    throw new Error('Clipboard unavailable')
  }
}

function getRefreshProgressText(task: Task | null) {
  if (!task) return null
  if (task.refresh_mode === 'full') {
    const current = task.progress_current ?? 0
    const total = task.progress_total ?? current
    return `${current}/${total}`
  }
  if (task.progress_current != null) {
    return `${task.progress_current}页`
  }
  return null
}

function getRefreshButtonLabel(loading: boolean, task: Task | null) {
  if (loading) return '刷新中'
  if (task?.status === 'completed') return '刷新完成'
  return '刷新'
}

function getRescanButtonLabel(loading: boolean, task: Task | null) {
  if (loading) return '扫描中'
  if (task?.status === 'completed') return '扫描完成'
  return '重扫本地库'
}

function getPostDownloadButtonState(status: string, isPending: boolean, hasMegaUrl: boolean) {
  if (status === 'completed') {
    return {
      label: '已下载',
      icon: <CheckOutlined />,
      disabled: true,
      loading: false,
      className: 'is-complete',
      type: 'default' as const,
    }
  }

  if (isPending || status === 'running_download') {
    return {
      label: '下载中',
      icon: <DownloadOutlined />,
      disabled: true,
      loading: true,
      className: 'is-active',
      type: 'default' as const,
    }
  }

  if (status === 'queued') {
    return {
      label: '排队中',
      icon: <ClockCircleOutlined />,
      disabled: true,
      loading: false,
      className: 'is-queued',
      type: 'default' as const,
    }
  }

  if (status === 'running_extract' || status === 'running_rename' || status === 'running_refresh') {
    return {
      label: POST_STATUS_META[status]?.label ?? '处理中',
      icon: <SyncOutlined spin />,
      disabled: true,
      loading: false,
      className: 'is-active',
      type: 'default' as const,
    }
  }

  if (status.startsWith('failed_')) {
    return {
      label: '重新下载',
      icon: <ReloadOutlined />,
      disabled: !hasMegaUrl,
      loading: false,
      className: 'is-retry',
      type: 'default' as const,
    }
  }

  return {
    label: '下载',
    icon: <DownloadOutlined />,
    disabled: !hasMegaUrl,
    loading: false,
    className: 'is-ready',
    type: 'default' as const,
  }
}

export default function App({
  resolvedThemeMode,
  followSystemTheme,
  onToggleTheme,
  onFollowSystemThemeChange,
}: AppProps) {
  const { message } = AntdApp.useApp()
  const queryClient = useQueryClient()
  const [form] = Form.useForm<SettingsFormValues>()
  const [panel, setPanel] = useState<Panel>(null)
  const [tasksOpen, setTasksOpen] = useState(false)
  const [purgeYear, setPurgeYear] = useState<string>()
  const [selectedYear, setSelectedYear] = useState<string>()
  const [frontYear, setFrontYear] = useState<string>()
  const [backYear, setBackYear] = useState<string>()
  const [frontPage, setFrontPage] = useState(1)
  const [backPage, setBackPage] = useState<number | null>(null)
  const [activePageLayer, setActivePageLayer] = useState<'front' | 'back'>('front')
  const [pageTransitionTarget, setPageTransitionTarget] = useState<{
    layer: 'front' | 'back'
    page: number
    year: string
  } | null>(null)
  const [bulkDownloadBatchIds, setBulkDownloadBatchIds] = useState<string[] | null>(null)
  const [downloadingPostId, setDownloadingPostId] = useState<string | null>(null)
  const [openingLocalPathPostId, setOpeningLocalPathPostId] = useState<string | null>(null)
  const [retryingTaskId, setRetryingTaskId] = useState<string | null>(null)
  const [taskDrawerWidth, setTaskDrawerWidth] = useState(() => getOptimalTaskDrawerWidth())
  const [taskDrawerPageSize, setTaskDrawerPageSize] = useState(() => getOptimalTaskDrawerPageSize())
  const [taskPage, setTaskPage] = useState(1)
  const [privacyMode, setPrivacyMode] = useState(
    () => typeof window !== 'undefined' && window.localStorage.getItem('fanbox-dashboard-privacy-mode') === 'on',
  )
  const autoRefreshTriggeredRef = useRef(false)
  const decodedCoverUrlsRef = useRef<Set<string>>(new Set())
  const coverReadyPromisesRef = useRef<Map<string, Promise<void>>>(new Map())
  const frontGridRef = useRef<HTMLDivElement | null>(null)
  const backGridRef = useRef<HTMLDivElement | null>(null)
  const delayedSettingsSyncRef = useRef<number | null>(null)
  const taskDrawerResizeCleanupRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedThemeMode
  }, [resolvedThemeMode])

  const ensureCoverDecoded = (url: string) => {
    if (decodedCoverUrlsRef.current.has(url)) return Promise.resolve()

    const existingPromise = coverReadyPromisesRef.current.get(url)
    if (existingPromise) return existingPromise

    const promise = new Promise<void>((resolve) => {
      const image = new Image()
      let settled = false

      const finish = (markDecoded: boolean) => {
        if (settled) return
        settled = true
        if (markDecoded) {
          decodedCoverUrlsRef.current.add(url)
        }
        resolve()
      }

      const handleLoaded = () => {
        if (typeof image.decode === 'function') {
          image.decode().then(() => finish(true)).catch(() => finish(true))
          return
        }
        finish(true)
      }

      image.onload = handleLoaded
      image.onerror = () => finish(false)
      image.decoding = 'async'
      image.src = url

      if (image.complete) {
        if (image.naturalWidth > 0) {
          handleLoaded()
        } else {
          finish(false)
        }
      }
    }).finally(() => {
      coverReadyPromisesRef.current.delete(url)
    })

    coverReadyPromisesRef.current.set(url, promise)
    return promise
  }

  const postsQuery = useQuery({
    queryKey: ['posts'],
    queryFn: api.getPosts,
    refetchInterval: 30_000,
  })
  const tasksQuery = useQuery({
    queryKey: ['tasks'],
    queryFn: api.getTasks,
    refetchInterval: (query) => getTasksRefetchInterval(tasksOpen, query.state.data as Task[] | undefined),
  })
  const settingsQuery = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  })
  const authQuery = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.getAuthStatus,
    refetchInterval: 45_000,
  })

  const invalidateOperationalQueries = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['posts'] }),
      queryClient.invalidateQueries({ queryKey: ['tasks'] }),
      queryClient.invalidateQueries({ queryKey: ['auth-status'] }),
    ])
  }

  const refreshMutation = useMutation({
    mutationFn: api.refreshPosts,
    onSuccess: async (_, mode) => {
      message.success(mode === 'full' ? '已创建全量校准任务' : '已创建刷新任务')
      await invalidateOperationalQueries()
    },
  })

  const loginMutation = useMutation({
    mutationFn: api.openLogin,
    onSuccess: async () => {
      message.success('已打开登录窗口')
      await queryClient.invalidateQueries({ queryKey: ['auth-status'] })
    },
  })

  const rescanMutation = useMutation({
    mutationFn: api.rescanLibrary,
    onSuccess: async () => {
      message.success('已创建本地扫描任务')
      await invalidateOperationalQueries()
    },
  })

  const downloadMutation = useMutation({
    mutationFn: api.downloadPosts,
    onMutate: (postIds) => {
      setDownloadingPostId(postIds.length === 1 ? String(postIds[0]) : null)
    },
    onSuccess: async () => {
      message.success('已创建补档任务')
      await invalidateOperationalQueries()
    },
    onSettled: () => {
      setDownloadingPostId(null)
    },
  })

  const retryMutation = useMutation({
    mutationFn: api.retryTask,
    onMutate: (taskId) => {
      setRetryingTaskId(taskId)
    },
    onSuccess: async () => {
      message.success('已重新执行任务')
      await invalidateOperationalQueries()
    },
    onError: (error) => {
      message.error(error instanceof Error ? error.message : '重新执行任务失败')
    },
    onSettled: () => {
      setRetryingTaskId(null)
    },
  })

  const clearTasksMutation = useMutation({
    mutationFn: api.clearTasks,
    onSuccess: async (data) => {
      message.success(data.deleted_count > 0 ? `已清空 ${data.deleted_count} 条任务` : '任务队列已清空')
      setTaskPage(1)
      await queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
    onError: (error) => {
      message.error(error instanceof Error ? error.message : '清空任务队列失败')
    },
  })

  const deleteArchiveMutation = useMutation({
    mutationFn: api.deleteArchive,
    onSuccess: async (data) => {
      message[data.deleted_count > 0 ? 'success' : 'info'](
        data.deleted_count > 0 ? '已删除对应压缩包' : '没有找到可删除的压缩包，已同步状态',
      )
      await queryClient.invalidateQueries({ queryKey: ['posts'] })
    },
  })

  const openLocalPathMutation = useMutation({
    mutationFn: api.openLocalPath,
    onMutate: (postId) => {
      setOpeningLocalPathPostId(postId)
    },
    onSuccess: () => {
      message.success('已打开本地路径')
    },
    onError: (error) => {
      message.error(error instanceof Error ? error.message : '打开本地路径失败')
    },
    onSettled: () => {
      setOpeningLocalPathPostId(null)
    },
  })

  const openYearFolderMutation = useMutation({
    mutationFn: api.openYearFolder,
    onSuccess: () => {
      message.success('已打开当前年份文件夹')
    },
    onError: (error) => {
      message.error(error instanceof Error ? error.message : '打开当前年份文件夹失败')
    },
  })

  const purgeArchiveMutation = useMutation({
    mutationFn: api.purgeArchives,
    onSuccess: async (data) => {
      message.success(`已清理 ${data.deleted_count} 个压缩包`)
      await queryClient.invalidateQueries({ queryKey: ['posts'] })
    },
  })

  const saveSettingsMutation = useMutation({
    mutationFn: api.saveSettings,
    onSuccess: (data) => {
      message.success('设置已保存')
      setPanel(null)
      if (delayedSettingsSyncRef.current !== null) {
        window.clearTimeout(delayedSettingsSyncRef.current)
      }
      delayedSettingsSyncRef.current = window.setTimeout(() => {
        queryClient.setQueryData(['settings'], data)
      form.setFieldsValue({
        ...data,
        follow_system_theme: followSystemTheme,
      })
      delayedSettingsSyncRef.current = null
    }, FLOATING_PANEL_EXIT_DELAY_MS)
  },
  })

  const posts = postsQuery.data ?? []
  const tasks = tasksQuery.data ?? []
  const latestRefreshTask = tasks.find((task) => task.kind === 'refresh_posts') ?? null
  const latestRescanTask = tasks.find((task) => task.kind === 'rescan_library') ?? null
  const latestIncrementalRefreshTask =
    tasks.find((task) => task.kind === 'refresh_posts' && task.refresh_mode === 'incremental') ?? null
  const latestFullRefreshTask =
    tasks.find((task) => task.kind === 'refresh_posts' && task.refresh_mode === 'full') ?? null
  const activeRefreshTask =
    tasks.find((task) => task.kind === 'refresh_posts' && ['queued', 'running_refresh'].includes(task.status)) ?? null
  const activeRescanTask =
    tasks.find((task) => task.kind === 'rescan_library' && ['queued', 'running_refresh'].includes(task.status)) ?? null
  const requestedRefreshMode = refreshMutation.variables as RefreshMode | undefined
  const anyRefreshInFlight = Boolean(activeRefreshTask) || refreshMutation.isPending
  const rescanInFlight = Boolean(activeRescanTask) || rescanMutation.isPending
  const incrementalRefreshInFlight =
    activeRefreshTask?.refresh_mode === 'incremental' ||
    (refreshMutation.isPending && requestedRefreshMode === 'incremental')
  const fullRefreshInFlight =
    activeRefreshTask?.refresh_mode === 'full' || (refreshMutation.isPending && requestedRefreshMode === 'full')

  useEffect(() => {
    if (settingsQuery.data) {
      form.setFieldsValue({
        ...settingsQuery.data,
        follow_system_theme: followSystemTheme,
      })
    }
  }, [followSystemTheme, form, settingsQuery.data])

  useEffect(() => {
    return () => {
      if (delayedSettingsSyncRef.current !== null) {
        window.clearTimeout(delayedSettingsSyncRef.current)
      }
      taskDrawerResizeCleanupRef.current?.()
    }
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const handleResize = () => {
      setTaskDrawerWidth((current) => clampTaskDrawerWidth(current))
      setTaskDrawerPageSize(getOptimalTaskDrawerPageSize())
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  useEffect(() => {
    if (!tasksOpen) return
    setTaskDrawerWidth(getOptimalTaskDrawerWidth())
    setTaskDrawerPageSize(getOptimalTaskDrawerPageSize())
    setTaskPage(1)
  }, [tasksOpen])

  useEffect(() => {
    if (autoRefreshTriggeredRef.current || tasksQuery.isLoading || authQuery.isLoading) return
    const hasRefreshInFlight = tasks.some(
      (task) => task.kind === 'refresh_posts' && ['queued', 'running_refresh'].includes(task.status),
    )
    if (hasRefreshInFlight) {
      autoRefreshTriggeredRef.current = true
      return
    }
    if (!authQuery.data?.authenticated) return
    autoRefreshTriggeredRef.current = true
    refreshMutation.mutate('incremental')
  }, [authQuery.data?.authenticated, authQuery.isLoading, refreshMutation, tasks, tasksQuery.isLoading])

  const availableYears = useMemo(
    () =>
      Array.from(new Set(posts.map(yearOf).filter((year): year is string => Boolean(year)))).sort(
        (a, b) => Number(b) - Number(a),
      ),
    [posts],
  )

  const currentYear = activePageLayer === 'front' ? frontYear : (backYear ?? frontYear)

  useEffect(() => {
    if (availableYears.length === 0) {
      setSelectedYear(undefined)
      setFrontYear(undefined)
      setBackYear(undefined)
      return
    }

    const fallbackYear = availableYears[0]
    if (!selectedYear || !availableYears.includes(selectedYear)) {
      setSelectedYear(fallbackYear)
    }

    if (!currentYear || !availableYears.includes(currentYear)) {
      setActivePageLayer('front')
      setFrontYear(selectedYear && availableYears.includes(selectedYear) ? selectedYear : fallbackYear)
      setFrontPage(1)
      setBackYear(undefined)
      setBackPage(null)
      setPageTransitionTarget(null)
    }
  }, [activePageLayer, availableYears, currentYear, selectedYear])

  const visiblePosts = useMemo(
    () => (!currentYear ? posts : posts.filter((post) => yearOf(post) === currentYear)),
    [currentYear, posts],
  )
  const frontVisiblePosts = useMemo(
    () => (!frontYear ? posts : posts.filter((post) => yearOf(post) === frontYear)),
    [frontYear, posts],
  )
  const backVisiblePosts = useMemo(
    () => (!backYear ? [] : posts.filter((post) => yearOf(post) === backYear)),
    [backYear, posts],
  )

  const postsPerRow = Math.min(6, Math.max(3, settingsQuery.data?.posts_per_row ?? 4))
  const postsPerPage = postsPerRow * 2
  const totalPages = Math.max(1, Math.ceil(visiblePosts.length / postsPerPage))
  const currentPage = activePageLayer === 'front' ? frontPage : (backPage ?? frontPage)
  const frontPageStart = (frontPage - 1) * postsPerPage
  const frontPagedPosts = useMemo(
    () => frontVisiblePosts.slice(frontPageStart, frontPageStart + postsPerPage),
    [frontPageStart, frontVisiblePosts, postsPerPage],
  )
  const backPageStart = ((backPage ?? 1) - 1) * postsPerPage
  const backPagedPosts = useMemo(
    () => (backPage == null ? [] : backVisiblePosts.slice(backPageStart, backPageStart + postsPerPage)),
    [backPage, backPageStart, backVisiblePosts, postsPerPage],
  )

  const visiblePageNumbers = useMemo(() => getVisiblePageNumbers(currentPage, totalPages), [currentPage, totalPages])
  const taskTotalPages = Math.max(1, Math.ceil(tasks.length / taskDrawerPageSize))
  const taskVisiblePageNumbers = useMemo(() => getVisiblePageNumbers(taskPage, taskTotalPages), [taskPage, taskTotalPages])
  const taskPageStart = (taskPage - 1) * taskDrawerPageSize
  const pagedTasks = useMemo(
    () => tasks.slice(taskPageStart, taskPageStart + taskDrawerPageSize),
    [taskDrawerPageSize, taskPageStart, tasks],
  )

  useEffect(() => {
    setActivePageLayer('front')
    setFrontYear(currentYear)
    setFrontPage(1)
    setBackYear(undefined)
    setBackPage(null)
    setPageTransitionTarget(null)
  }, [postsPerPage])

  useEffect(() => {
    if (currentPage > totalPages) {
      if (activePageLayer === 'front') {
        setFrontPage(totalPages)
      } else {
        setBackPage(totalPages)
      }
    }
  }, [activePageLayer, currentPage, totalPages])

  useEffect(() => {
    if (taskPage > taskTotalPages) {
      setTaskPage(taskTotalPages)
    }
  }, [taskPage, taskTotalPages])

  useEffect(() => {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('fanbox-dashboard-privacy-mode', privacyMode ? 'on' : 'off')
    }
  }, [privacyMode])

  useEffect(() => {
    for (const url of visiblePosts.map((post) => post.cover_url).filter((value): value is string => Boolean(value))) {
      void ensureCoverDecoded(url)
    }
  }, [visiblePosts])

  const stats = useMemo(
    () => ({
      total: visiblePosts.length,
      missing: visiblePosts.filter((post) => post.status === 'missing_local').length,
      completed: visiblePosts.filter((post) => post.status === 'completed').length,
      archiveCount: visiblePosts.filter((post) => Boolean(post.archive_path)).length,
    }),
    [visiblePosts],
  )
  const failedPostCount = useMemo(
    () => visiblePosts.filter((post) => post.status.startsWith('failed')).length,
    [visiblePosts],
  )

  const bulkDownloadPostIds = useMemo(
    () =>
      visiblePosts
        .filter((post) => Boolean(post.mega_url) && post.status !== 'completed')
        .map((post) => post.post_id),
    [visiblePosts],
  )

  const bulkDownloadBatchIdSet = useMemo(
    () => new Set((bulkDownloadBatchIds ?? []).map(String)),
    [bulkDownloadBatchIds],
  )
  const bulkDownloadTotalCount = bulkDownloadBatchIds?.length ?? 0
  const bulkDownloadCompletedCount = useMemo(
    () => posts.filter((post) => bulkDownloadBatchIdSet.has(String(post.post_id)) && post.status === 'completed').length,
    [bulkDownloadBatchIdSet, posts],
  )
  const bulkDownloadRunningCount = useMemo(
    () =>
      posts.filter(
        (post) =>
          bulkDownloadBatchIdSet.has(String(post.post_id)) &&
          ['queued', 'running_download', 'running_extract', 'running_rename'].includes(post.status),
      ).length,
    [bulkDownloadBatchIdSet, posts],
  )
  const bulkDownloadInFlight =
    bulkDownloadTotalCount > 0 &&
    bulkDownloadCompletedCount < bulkDownloadTotalCount &&
    (downloadMutation.isPending || bulkDownloadRunningCount > 0)
  const bulkDownloadFinished = bulkDownloadTotalCount > 0 && bulkDownloadCompletedCount === bulkDownloadTotalCount
  useEffect(() => {
    if (
      bulkDownloadBatchIds &&
      !downloadMutation.isPending &&
      !bulkDownloadInFlight &&
      !bulkDownloadFinished &&
      bulkDownloadRunningCount === 0
    ) {
      setBulkDownloadBatchIds(null)
    }
  }, [
    bulkDownloadBatchIds,
    bulkDownloadFinished,
    bulkDownloadInFlight,
    bulkDownloadRunningCount,
    downloadMutation.isPending,
  ])

  const refreshProgressText = getRefreshProgressText(activeRefreshTask)
  const refreshButtonLabel = getRefreshButtonLabel(incrementalRefreshInFlight, latestIncrementalRefreshTask)
  const fullRefreshButtonLabel = fullRefreshInFlight
    ? '全量校准中'
    : latestFullRefreshTask?.status === 'completed'
      ? '全量校准完成'
      : '全量校准'
  const bulkDownloadButtonLabel = bulkDownloadFinished ? '下载全部完成' : '下载全部'
  const rescanButtonLabel = getRescanButtonLabel(rescanInFlight, latestRescanTask)
  const bulkDownloadProgressText =
    bulkDownloadTotalCount > 0 ? `${bulkDownloadCompletedCount}/${bulkDownloadTotalCount}` : null

  const taskColumns: ColumnsType<Task> = [
    {
      title: '任务 ID',
      dataIndex: 'id',
      width: 250,
      ellipsis: true,
      render: (value: string) => (
        <Button type="text" className="task-id-button" title={value} onClick={() => void handleCopyTaskId(value)}>
          {value}
        </Button>
      ),
    },
    {
      title: '任务类型',
      dataIndex: 'kind',
      width: 126,
      align: 'center',
      render: (_, record) => <span className="task-kind-chip">{getTaskKindLabel(record)}</span>,
    },
    {
      title: '当前状态',
      dataIndex: 'status',
      width: 110,
      align: 'center',
      render: (value: string, record) => <Tag color={STATUS_COLORS[value] ?? 'default'}>{getTaskStatusLabel(record)}</Tag>,
    },
    {
      title: '进展',
      dataIndex: 'message',
      width: 250,
      render: (_, record) => (
        <div className="task-message" title={getTaskProgressLabel(record)}>
          <span className="task-message-text">{getTaskProgressLabel(record)}</span>
        </div>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 154,
      align: 'center',
      render: (value: string) => <span className="task-created-at">{toLocalTime(value)?.format('MM-DD HH:mm') ?? '--'}</span>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 112,
      align: 'center',
      render: (_, record) => (
        <Button
          className="task-retry-button"
          disabled={!record.status.startsWith('failed') || (retryMutation.isPending && retryingTaskId !== record.id)}
          loading={retryMutation.isPending && retryingTaskId === record.id}
          onClick={() => retryMutation.mutate(record.id)}
        >
          重新执行
        </Button>
      ),
    },
  ]

  const menuItems: MenuProps['items'] = [
    { key: 'settings', label: '设置', icon: <SettingOutlined /> },
    { key: 'archive', label: 'Archive 清理', icon: <FolderOpenOutlined /> },
    { key: 'tasks', label: '任务队列', icon: <UnorderedListOutlined /> },
    { type: 'divider' },
    { key: 'login', label: '打开登录窗口', icon: <LoginOutlined /> },
  ]

  const handleMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (key === 'settings') setPanel('settings')
    if (key === 'archive') setPanel('archive')
    if (key === 'tasks') setTasksOpen(true)
    if (key === 'login') loginMutation.mutate()
  }

  const handleBulkDownload = () => {
    if (!bulkDownloadPostIds.length) return
    setBulkDownloadBatchIds(bulkDownloadPostIds.map(String))
    downloadMutation.mutate(bulkDownloadPostIds)
  }

  const handleCopyTaskId = async (taskId: string) => {
    try {
      await copyTextToClipboard(taskId)
      message.success('任务 ID 已复制')
    } catch {
      message.error('复制任务 ID 失败')
    }
  }

  const startTaskDrawerResize = (event: ReactMouseEvent<HTMLDivElement>) => {
    event.preventDefault()
    if (typeof window === 'undefined') return

    taskDrawerResizeCleanupRef.current?.()

    const handleMove = (moveEvent: MouseEvent) => {
      const nextWidth = clampTaskDrawerWidth(window.innerWidth - moveEvent.clientX)
      setTaskDrawerWidth(nextWidth)
    }

    const handleUp = () => {
      document.body.classList.remove('is-resizing-task-drawer')
      document.removeEventListener('mousemove', handleMove)
      document.removeEventListener('mouseup', handleUp)
      taskDrawerResizeCleanupRef.current = null
    }

    document.body.classList.add('is-resizing-task-drawer')
    document.addEventListener('mousemove', handleMove)
    document.addEventListener('mouseup', handleUp)
    taskDrawerResizeCleanupRef.current = handleUp
  }

  const changeTaskPage = (nextPage: number) => {
    const normalizedPage = Math.max(1, Math.min(taskTotalPages, nextPage))
    if (normalizedPage === taskPage) return
    setTaskPage(normalizedPage)
  }

  useEffect(() => {
    if (!pageTransitionTarget) return

    const targetGridRef = pageTransitionTarget.layer === 'front' ? frontGridRef : backGridRef
    const targetGrid = targetGridRef.current
    if (!targetGrid) return

    let settled = false
    let timeout: number | null = null
    const cleanups: Array<() => void> = []

    const finish = () => {
      if (settled) return
      settled = true
      if (timeout !== null) {
        window.clearTimeout(timeout)
      }
      cleanups.forEach((cleanup) => cleanup())
      startTransition(() => setActivePageLayer(pageTransitionTarget.layer))
      setPageTransitionTarget(null)
    }

    const images = Array.from(targetGrid.querySelectorAll<HTMLImageElement>('.post-cover-image'))
    if (images.length === 0) {
      finish()
      return
    }

    let remaining = 0
    for (const image of images) {
      if (image.complete && image.naturalWidth > 0) {
        continue
      }
      remaining += 1
      const handleDone = () => {
        remaining -= 1
        if (remaining <= 0) {
          finish()
        }
      }
      image.addEventListener('load', handleDone, { once: true })
      image.addEventListener('error', handleDone, { once: true })
      cleanups.push(() => {
        image.removeEventListener('load', handleDone)
        image.removeEventListener('error', handleDone)
      })
    }

    timeout = window.setTimeout(finish, COVER_READY_TIMEOUT_MS)
    if (remaining === 0) {
      finish()
    }

    return () => {
      if (settled) return
      settled = true
      if (timeout !== null) {
        window.clearTimeout(timeout)
      }
      cleanups.forEach((cleanup) => cleanup())
    }
  }, [backPagedPosts, frontPagedPosts, pageTransitionTarget])

  const changePage = (nextPage: number) => {
    const normalizedPage = Math.max(1, Math.min(totalPages, nextPage))
    if (normalizedPage === currentPage || pageTransitionTarget) return

    const targetLayer = activePageLayer === 'front' ? 'back' : 'front'
    const targetYear = currentYear
    if (!targetYear) return
    if (targetLayer === 'front') {
      setFrontYear(targetYear)
      setFrontPage(normalizedPage)
    } else {
      setBackYear(targetYear)
      setBackPage(normalizedPage)
    }
    setPageTransitionTarget({ layer: targetLayer, page: normalizedPage, year: targetYear })
  }

  const changeYear = (nextYear: string) => {
    setSelectedYear(nextYear)
    if (!nextYear || nextYear === currentYear || pageTransitionTarget) return

    const targetLayer = activePageLayer === 'front' ? 'back' : 'front'
    if (targetLayer === 'front') {
      setFrontYear(nextYear)
      setFrontPage(1)
    } else {
      setBackYear(nextYear)
      setBackPage(1)
    }
    setPageTransitionTarget({ layer: targetLayer, page: 1, year: nextYear })
  }

  const postGridStyle = {
    ['--post-grid-columns' as '--post-grid-columns']: String(postsPerRow),
  } as CSSProperties

  const handleSaveSettings = (values: SettingsFormValues) => {
    const { follow_system_theme, ...settingsPayload } = values
    onFollowSystemThemeChange(follow_system_theme)
    saveSettingsMutation.mutate(settingsPayload)
  }

  const themeButtonTitle = followSystemTheme
    ? `当前跟随系统，点击切换为手动${resolvedThemeMode === 'dark' ? '浅色' : '深色'}主题`
    : `切换到${resolvedThemeMode === 'dark' ? '浅色' : '深色'}主题`
  const currentYearLabel = currentYear ?? '--'
  const latestRefreshSummary = latestRefreshTask
    ? `${latestRefreshTask.refresh_mode === 'full' ? '全量校准' : '增量刷新'} · ${formatTaskTimestamp(latestRefreshTask.created_at)}`
    : '暂无刷新记录'
  const latestRescanSummary = latestRescanTask
    ? `${latestRescanTask.status === 'completed' ? '完成于' : '创建于'} ${formatTaskTimestamp(latestRescanTask.created_at)}`
    : '尚未扫描'

  let focusTitle = `${currentYearLabel} 年已整理完成`

  if (!authQuery.data?.authenticated) {
    focusTitle = '登录后即可继续刷新'
  } else if (anyRefreshInFlight) {
    focusTitle = '正在刷新最新帖子'
  } else if (bulkDownloadInFlight) {
    focusTitle = '正在批量补档'
  } else if (stats.missing > 0) {
    focusTitle = `还有 ${stats.missing} 个帖子待补档`
  } else if (failedPostCount > 0) {
    focusTitle = `有 ${failedPostCount} 个帖子需要重试`
  } else if (visiblePosts.length === 0) {
    focusTitle = `${currentYearLabel} 年还没有帖子`
  }

  const pulseItems = [
    {
      label: '登录',
      value: authQuery.data?.authenticated ? '已登录' : '未登录',
      tone: authQuery.data?.authenticated ? 'success' : 'warning',
      icon: authQuery.data?.authenticated ? <CheckCircleFilled /> : <WarningOutlined />,
    },
    {
      label: '最近刷新',
      value: latestRefreshSummary,
      tone: anyRefreshInFlight ? 'info' : 'neutral',
      icon: anyRefreshInFlight ? <SyncOutlined spin /> : <ReloadOutlined />,
    },
    {
      label: '最近扫描',
      value: latestRescanSummary,
      tone: stats.missing > 0 ? 'warning' : 'neutral',
      icon: <ClockCircleOutlined />,
    },
  ]

  const overviewStats = [
    { label: '帖子总数', value: stats.total },
    { label: '已入库', value: stats.completed },
  ]

  const renderPostTile = (record: Post) => {
    const isDownloadPending =
      downloadMutation.isPending && downloadingPostId != null && downloadingPostId === String(record.post_id)
    const downloadButtonState = getPostDownloadButtonState(record.status, isDownloadPending, Boolean(record.mega_url))

    return (
      <article className={`post-tile${privacyMode ? ' is-private' : ''}`} key={record.post_id}>
        <a
          className="post-cover-link"
          href={record.detail_url}
          target="_blank"
          rel="noreferrer"
          aria-label={record.title}
        >
          {privacyMode ? (
            <div className="post-cover-private">
              <div className="post-cover-private-title" title={record.title}>
                {record.title}
              </div>
            </div>
          ) : record.cover_url ? (
            <img className="post-cover-image" src={record.cover_url} alt={record.title} loading="eager" />
          ) : (
            <div className="post-cover-placeholder">
              <span>{record.title.slice(0, 1).toUpperCase()}</span>
            </div>
          )}
        </a>
        <div className={`post-tile-footer${privacyMode ? ' is-private' : ''}`}>
          <div className="post-meta">
            <div
              className={`post-tile-title${privacyMode ? ' is-hidden' : ''}`}
              title={privacyMode ? undefined : record.title}
              aria-hidden={privacyMode}
            >
              {record.title}
            </div>
            <div className="post-bottom-row">
              <div className="post-tile-date">{formatPostDate(record.published_at)}</div>
              <div className="post-actions">
                <Button
                  type={downloadButtonState.type}
                  size="small"
                  className={`post-download-button ${downloadButtonState.className}`}
                  icon={downloadButtonState.icon}
                  disabled={downloadButtonState.disabled}
                  loading={downloadButtonState.loading}
                  onClick={() => downloadMutation.mutate([record.post_id])}
                >
                  {downloadButtonState.label}
                </Button>
                {record.can_open_local_path ? (
                  <Button
                    size="small"
                    className={record.status === 'completed' ? 'post-action-icon-only' : undefined}
                    icon={<FolderOpenOutlined />}
                    loading={openLocalPathMutation.isPending && openingLocalPathPostId === record.post_id}
                    aria-label="打开"
                    onClick={() => openLocalPathMutation.mutate(record.post_id)}
                  >
                    {record.status === 'completed' ? null : '打开'}
                  </Button>
                ) : null}
                <Dropdown
                  trigger={['click']}
                  menu={{
                    items: [
                      {
                        key: 'open-local-path',
                        label: '打开本地路径',
                        disabled: !record.can_open_local_path,
                      },
                      {
                        key: 'delete-archive',
                        label: '删除本地压缩包',
                        disabled: !record.archive_path,
                      },
                    ],
                    onClick: ({ key }) => {
                      if (key === 'open-local-path') openLocalPathMutation.mutate(record.post_id)
                      if (key === 'delete-archive') deleteArchiveMutation.mutate(record.post_id)
                    },
                  }}
                >
                  <Button size="small" icon={<EllipsisOutlined />} aria-label="更多操作" />
                </Dropdown>
              </div>
            </div>
          </div>
        </div>
      </article>
    )
  }

  return (
    <Layout className="app-shell">
      <Header className="topbar">
        <div className="topbar-brand">
          <div className="brand-emblem">
            <span className="brand-emblem-text">FD</span>
          </div>
          <div className="brand-copy">
            <Title level={3} className="topbar-title">
              Fanbox Dashboard
            </Title>
          </div>
        </div>
        <div className="topbar-meta">
          <div className="topbar-actions">
            <Button
              type="text"
              size="large"
              className={`theme-trigger${resolvedThemeMode === 'dark' ? ' is-dark' : ''}`}
              icon={resolvedThemeMode === 'dark' ? <MoonOutlined /> : <SunOutlined />}
              aria-label={themeButtonTitle}
              title={themeButtonTitle}
              onClick={onToggleTheme}
            />
            <Button
              type="text"
              size="large"
              className={`privacy-trigger${privacyMode ? ' is-active' : ''}`}
              icon={privacyMode ? <EyeInvisibleOutlined /> : <EyeOutlined />}
              aria-label={privacyMode ? '关闭隐私模式' : '开启隐私模式'}
              title={privacyMode ? '隐私模式已开启' : '隐私模式已关闭'}
              onClick={() => setPrivacyMode((current) => !current)}
            />
            <Dropdown
              menu={{ items: menuItems, onClick: handleMenuClick }}
              trigger={['click']}
              placement="bottomRight"
              overlayClassName="topbar-menu-overlay"
            >
              <Button
                type="text"
                size="large"
                className="settings-trigger"
                icon={<AppstoreOutlined />}
                aria-label="工具"
                title="工具"
              />
            </Dropdown>
          </div>
        </div>
      </Header>

      <Content className="main-content">
        <div className="dashboard-shell">
          <section className="dashboard-stage">
            <div className="focus-panel">
              <Text className="panel-kicker">Today&apos;s Workflow</Text>
              <div className="focus-layout">
                <div className="focus-main">
                  <div className="focus-header">
                    <div className="focus-copy">
                      <Title level={1} className="focus-title">
                        {focusTitle}
                      </Title>
                    </div>
                  </div>

                  {authQuery.data && !authQuery.data.authenticated ? (
                    <Alert
                      className="focus-alert"
                      type="warning"
                      showIcon
                      message="当前登录已失效"
                      description={authQuery.data.reason ?? '请先打开登录窗口，重新完成 Fanbox 登录。'}
                      action={
                        <Button size="small" loading={loginMutation.isPending} onClick={() => loginMutation.mutate()}>
                          打开登录窗口
                        </Button>
                      }
                    />
                  ) : null}

                  <div className="focus-lower">
                    <div className="action-deck">
                      <Button
                        type="primary"
                        size="large"
                        className="action-button action-button--primary"
                        icon={<ReloadOutlined />}
                        loading={incrementalRefreshInFlight}
                        disabled={anyRefreshInFlight && !incrementalRefreshInFlight}
                        onClick={() => refreshMutation.mutate('incremental')}
                        >
                          <span
                            key={`refresh-${refreshButtonLabel}-${refreshProgressText ?? 'idle'}`}
                            className="action-button-content"
                          >
                            <span className="action-button-label">{refreshButtonLabel}</span>
                            {incrementalRefreshInFlight && refreshProgressText ? (
                              <span className="action-progress-pill">{refreshProgressText}</span>
                            ) : null}
                          </span>
                      </Button>
                      <Button
                        type="primary"
                        size="large"
                        className="action-button action-button--accent"
                        icon={<DownloadOutlined />}
                        disabled={bulkDownloadPostIds.length === 0}
                        loading={bulkDownloadInFlight}
                        onClick={handleBulkDownload}
                        >
                          <span
                            key={`bulk-${bulkDownloadButtonLabel}-${bulkDownloadProgressText ?? 'idle'}`}
                            className="action-button-content"
                          >
                            <span className="action-button-label">{bulkDownloadButtonLabel}</span>
                            {bulkDownloadInFlight && bulkDownloadProgressText ? (
                              <span className="action-progress-pill">{bulkDownloadProgressText}</span>
                            ) : null}
                          </span>
                      </Button>
                      <Button
                        size="large"
                        className="action-button action-button--secondary"
                        icon={<SyncOutlined />}
                        loading={fullRefreshInFlight}
                        disabled={anyRefreshInFlight && !fullRefreshInFlight}
                        onClick={() => refreshMutation.mutate('full')}
                        >
                          <span
                            key={`full-${fullRefreshButtonLabel}-${refreshProgressText ?? 'idle'}`}
                            className="action-button-content"
                          >
                            <span className="action-button-label">{fullRefreshButtonLabel}</span>
                            {fullRefreshInFlight && refreshProgressText ? (
                              <span className="action-progress-pill">{refreshProgressText}</span>
                            ) : null}
                          </span>
                      </Button>
                      <Button
                        size="large"
                        className="action-button action-button--secondary"
                        icon={<ClockCircleOutlined />}
                        loading={rescanInFlight}
                        onClick={() => rescanMutation.mutate()}
                        >
                          <span key={`rescan-${rescanButtonLabel}`} className="action-button-content">
                            <span className="action-button-label">{rescanButtonLabel}</span>
                          </span>
                        </Button>
                    </div>
                  </div>
                </div>
                <aside className="focus-aside">
                  <div className="pulse-strip">
                    {pulseItems.map((item) => (
                      <div className={`pulse-card is-${item.tone}`} key={item.label}>
                        <div className="pulse-icon">{item.icon}</div>
                        <div className="pulse-copy">
                          <span className="pulse-label">{item.label}</span>
                          <strong className="pulse-value">{item.value}</strong>
                        </div>
                      </div>
                    ))}
                  </div>
                </aside>
              </div>
            </div>

            <aside className="overview-panel">
              <Text className="panel-kicker">Library Lens</Text>
              <div className="overview-controls">
                <Text className="overview-label">年份：</Text>
                <div className="overview-picker-row">
                  <Select
                    className="overview-year-select"
                    value={selectedYear}
                    placeholder="选择年份"
                    options={availableYears.map((year) => ({ label: year, value: year }))}
                    disabled={pageTransitionTarget !== null}
                    onChange={changeYear}
                  />
                  <Button
                    className="overview-folder-button"
                    icon={<FolderOpenOutlined />}
                    disabled={!selectedYear || pageTransitionTarget !== null}
                    loading={openYearFolderMutation.isPending}
                    onClick={() => selectedYear && openYearFolderMutation.mutate(selectedYear)}
                  >
                    打开年份目录
                  </Button>
                </div>
              </div>

              <div className="overview-highlight">
                <strong className="overview-highlight-value">{stats.missing}</strong>
                <div className="overview-highlight-copy">
                  <span className="overview-highlight-label">待补档</span>
                </div>
              </div>

              <div className="overview-stat-list">
                {overviewStats.map((item) => (
                  <div className="overview-stat-row" key={item.label}>
                    <div>
                      <span className="overview-stat-label">{item.label}</span>
                    </div>
                    <strong className="overview-stat-value">{item.value}</strong>
                  </div>
                ))}
              </div>
            </aside>
          </section>

          <section className="library-section">
            <div className="section-heading">
              <div>
                <Text className="panel-kicker">Library</Text>
                <Title level={3} className="section-title">
                  {currentYearLabel} 年帖子
                </Title>
              </div>
              <div className="section-meta">
                <span className="section-chip">已入库 {stats.completed}</span>
                <span className="section-chip">压缩包 {stats.archiveCount}</span>
                <span className={`section-chip${privacyMode ? ' is-active' : ''}`}>
                  隐私模式已{privacyMode ? '开' : '关'}
                </span>
              </div>
            </div>

            {visiblePosts.length === 0 ? (
              <div className="empty-panel">
                <Empty description="这一年还没有帖子" />
              </div>
            ) : (
              <>
                <div className="post-grid-stack">
                  <div
                    ref={frontGridRef}
                    className={`post-grid post-grid-layer${activePageLayer === 'front' ? ' is-active' : ' is-hidden'}${
                      pageTransitionTarget?.layer === 'front' ? ' is-staging' : ''
                    }`}
                    style={postGridStyle}
                    aria-hidden={activePageLayer !== 'front'}
                  >
                    {frontPagedPosts.map(renderPostTile)}
                  </div>
                  {backPage !== null ? (
                    <div
                      ref={backGridRef}
                      className={`post-grid post-grid-layer${activePageLayer === 'back' ? ' is-active' : ' is-hidden'}${
                        pageTransitionTarget?.layer === 'back' ? ' is-staging' : ''
                      }`}
                      style={postGridStyle}
                      aria-hidden={activePageLayer !== 'back'}
                    >
                        {backPagedPosts.map(renderPostTile)}
                    </div>
                  ) : null}
                </div>

                <div className="post-pagination">
                  <Button
                    className="post-pagination-nav"
                    disabled={currentPage <= 1 || pageTransitionTarget !== null}
                    aria-label="回到首页"
                    onClick={() => changePage(1)}
                  >
                    <span className="post-pagination-nav-content" aria-hidden="true">
                      <span className="post-pagination-step-icon">
                        <span className="post-pagination-step-bar" />
                        <LeftOutlined />
                      </span>
                    </span>
                  </Button>
                  <Button
                    className="post-pagination-nav"
                    disabled={currentPage <= 1 || pageTransitionTarget !== null}
                    aria-label="上一页"
                    onClick={() => changePage(Math.max(1, currentPage - 1))}
                  >
                    <span className="post-pagination-nav-content" aria-hidden="true">
                      <LeftOutlined />
                    </span>
                  </Button>
                  <div className="post-pagination-pages" role="group" aria-label="页码">
                    {visiblePageNumbers.map((pageNumber, index) => (
                      <Button
                        key={`page-slot-${index}`}
                        type={pageNumber === currentPage ? 'primary' : 'default'}
                        className="post-pagination-page"
                        disabled={pageTransitionTarget !== null}
                        onClick={() => changePage(pageNumber)}
                      >
                        {pageNumber}
                      </Button>
                    ))}
                  </div>
                  <Button
                    className="post-pagination-nav"
                    disabled={currentPage >= totalPages || pageTransitionTarget !== null}
                    aria-label="下一页"
                    onClick={() => changePage(Math.min(totalPages, currentPage + 1))}
                  >
                    <span className="post-pagination-nav-content" aria-hidden="true">
                      <RightOutlined />
                    </span>
                  </Button>
                  <Button
                    className="post-pagination-nav"
                    disabled={currentPage >= totalPages || pageTransitionTarget !== null}
                    aria-label="回到尾页"
                    onClick={() => changePage(totalPages)}
                  >
                    <span className="post-pagination-nav-content" aria-hidden="true">
                      <span className="post-pagination-step-icon">
                        <RightOutlined />
                        <span className="post-pagination-step-bar" />
                      </span>
                    </span>
                  </Button>
                </div>
              </>
            )}
          </section>
        </div>
      </Content>

      <Modal
        centered
        open={panel === 'settings'}
        onCancel={() => setPanel(null)}
        footer={null}
        width={760}
        className="floating-panel-modal"
        transitionName="floating-panel-motion"
        maskTransitionName="floating-panel-mask-motion"
        title={
          <div className="floating-panel-title">
            <span className="floating-panel-title-text">设置</span>
          </div>
        }
      >
        <Form<SettingsFormValues>
          form={form}
          layout="vertical"
          onFinish={handleSaveSettings}
          initialValues={settingsQuery.data}
        >
          <div className="settings-layout">
            <section className="settings-section">
              <div className="settings-section-header">
                <Text className="settings-section-kicker">日常偏好</Text>
              </div>
              <div className="settings-grid">
                <Form.Item name="refresh_interval_minutes" label="刷新间隔（分钟）">
                  <InputNumber min={0} max={1440} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="posts_per_row" label="每行帖子数量">
                  <Select options={[3, 4, 5, 6].map((value) => ({ label: `${value}`, value }))} />
                </Form.Item>
                <Form.Item name="download_concurrency" label="下载并发数">
                  <Slider min={1} max={8} marks={{ 1: '1', 5: '5', 8: '8' }} />
                </Form.Item>
                <Form.Item name="follow_system_theme" label="跟随系统颜色" valuePropName="checked">
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              </div>
            </section>

            <section className="settings-section">
              <div className="settings-section-header">
                <Text className="settings-section-kicker">路径与目录</Text>
              </div>
              <div className="settings-grid">
                <Form.Item name="library_dir" label="图库根目录" rules={[{ required: true }]}>
                  <Input />
                </Form.Item>
                <Form.Item name="download_dir" label="下载目录" rules={[{ required: true }]}>
                  <Input />
                </Form.Item>
                <Form.Item name="temp_dir" label="临时目录" rules={[{ required: true }]}>
                  <Input />
                </Form.Item>
                <Form.Item name="auto_delete_archive" label="整理完成后自动删除压缩包" valuePropName="checked">
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              </div>
            </section>

            <section className="settings-section">
              <div className="settings-section-header">
                <Text className="settings-section-kicker">抓取环境</Text>
              </div>
              <div className="settings-grid">
                <Form.Item name="creator_url" label="Fanbox 页面地址" rules={[{ required: true }]}>
                  <Input placeholder="https://example.fanbox.cc/posts" />
                </Form.Item>
                <Form.Item name="profile_dir" label="登录浏览器目录" rules={[{ required: true }]}>
                  <Input />
                </Form.Item>
                <Form.Item name="playwright_channel" label="浏览器通道" rules={[{ required: true }]}>
                  <Input />
                </Form.Item>
              </div>
            </section>

            <section className="settings-section">
              <div className="settings-section-header">
                <Text className="settings-section-kicker">高级设置</Text>
              </div>
              <div className="settings-grid">
                <Form.Item name="mega_command" label="下载命令" rules={[{ required: true }]}>
                  <Input />
                </Form.Item>
              </div>
            </section>
          </div>
          <div className="floating-panel-actions">
            <Tag className="floating-panel-status-tag" color={authQuery.data?.authenticated ? 'green' : 'gold'}>
              {authQuery.data?.authenticated ? '已登录' : '未登录'}
            </Tag>
            <div className="floating-panel-button-group">
              <Button loading={loginMutation.isPending} onClick={() => loginMutation.mutate()}>
                打开登录窗口
              </Button>
              <Button type="primary" htmlType="submit" loading={saveSettingsMutation.isPending}>
                保存设置
              </Button>
            </div>
          </div>
        </Form>
      </Modal>

      <Modal
        centered
        open={panel === 'archive'}
        onCancel={() => setPanel(null)}
        footer={null}
        width={560}
        className="floating-panel-modal"
        transitionName="floating-panel-motion"
        maskTransitionName="floating-panel-mask-motion"
        title={<span className="archive-panel-title">Archive 清理</span>}
      >
        <Space direction="vertical" size={18} style={{ width: '100%' }}>
          <Paragraph className="archive-panel-description" style={{ marginBottom: 0 }}>
            压缩包统一保存在 <Text code>{`D:\\hmoe\\Siu\\年份\\Archive`}</Text> 的 Archive 目录。可按年份清理，也可以一次清理全部年份。
          </Paragraph>
          <div className="floating-panel-actions archive-panel-actions">
            <div className="archive-panel-control-row">
              <Select
                allowClear
                className="archive-year-select"
                placeholder="选择年份"
                value={purgeYear}
                onChange={(value) => setPurgeYear(value)}
                options={availableYears.map((year) => ({ label: year, value: year }))}
              />
              <Popconfirm
                title={purgeYear ? `清理 ${purgeYear} 年压缩包？` : '先选择年份'}
                description={
                  purgeYear ? `会删除 ${purgeYear} 年 Archive 目录中的压缩包。此操作不可撤销。` : '请选择要清理的年份。'
                }
                disabled={!purgeYear}
                onConfirm={() => purgeArchiveMutation.mutate(purgeYear)}
              >
                <Button disabled={!purgeYear} loading={purgeArchiveMutation.isPending}>
                  清理该年份
                </Button>
              </Popconfirm>
              <Popconfirm
                title="清理全部年份的压缩包？"
                description="会删除所有年份 Archive 目录中的压缩包。此操作不可撤销。"
                onConfirm={() => purgeArchiveMutation.mutate(null)}
              >
                <Button className="archive-danger-button" danger loading={purgeArchiveMutation.isPending}>
                  清理全部年份
                </Button>
              </Popconfirm>
            </div>
          </div>
        </Space>
      </Modal>

      <Drawer
        title={
          <div className="task-drawer-header">
            <div className="task-drawer-header-main">
              <span className="task-drawer-title">任务队列</span>
              <span className="task-drawer-count">{tasks.length}</span>
            </div>
            <Popconfirm
              title="清空任务队列？"
              description="只会清空已结束的任务。此操作不可撤销。"
              okText="清空"
              cancelText="取消"
              onConfirm={() => clearTasksMutation.mutate()}
              disabled={!tasks.length}
            >
              <Button
                className="task-clear-button"
                danger
                loading={clearTasksMutation.isPending}
                disabled={!tasks.length}
              >
                清空任务队列
              </Button>
            </Popconfirm>
          </div>
        }
        placement="right"
        width={taskDrawerWidth}
        open={tasksOpen}
        onClose={() => setTasksOpen(false)}
        destroyOnClose={false}
        className="task-drawer"
      >
        <div className="task-drawer-resizer" onMouseDown={startTaskDrawerResize} aria-hidden="true" />
        <div className="task-table-shell">
          <Table<Task>
            className="task-table"
            rowKey="id"
            dataSource={pagedTasks}
            columns={taskColumns}
            loading={tasksQuery.isLoading}
            tableLayout="fixed"
            rowClassName={() => 'task-table-row'}
              expandable={{
                expandedRowRender: (record) => (
                  <Space direction="vertical" size={8} className="task-row-detail">
                    {record.error ? <Alert type="error" message="失败原因" description={record.error} /> : null}
                    {record.log ? (
                      <Typography.Paragraph className="task-log" copyable>
                        {record.log}
                      </Typography.Paragraph>
                    ) : (
                      <Text type="secondary">当前没有可显示的日志</Text>
                    )}
                  </Space>
                ),
            }}
            pagination={false}
            scroll={taskDrawerWidth < MIN_TASK_TABLE_SCROLL_WIDTH ? { x: MIN_TASK_TABLE_SCROLL_WIDTH } : undefined}
          />
          {taskTotalPages > 1 ? (
            <div className="task-pagination post-pagination">
              <Button className="post-pagination-nav" disabled={taskPage <= 1} aria-label="回到首页" onClick={() => changeTaskPage(1)}>
                <span className="post-pagination-nav-content" aria-hidden="true">
                  <span className="post-pagination-step-icon">
                    <span className="post-pagination-step-bar" />
                    <LeftOutlined />
                  </span>
                </span>
              </Button>
              <Button
                className="post-pagination-nav"
                disabled={taskPage <= 1}
                aria-label="上一页"
                onClick={() => changeTaskPage(Math.max(1, taskPage - 1))}
              >
                <span className="post-pagination-nav-content" aria-hidden="true">
                  <LeftOutlined />
                </span>
              </Button>
              <div className="post-pagination-pages" role="group" aria-label="页码">
                {taskVisiblePageNumbers.map((pageNumber, index) => (
                  <Button
                    key={`task-page-slot-${index}`}
                    type={pageNumber === taskPage ? 'primary' : 'default'}
                    className="post-pagination-page"
                    onClick={() => changeTaskPage(pageNumber)}
                  >
                    {pageNumber}
                  </Button>
                ))}
              </div>
              <Button
                className="post-pagination-nav"
                disabled={taskPage >= taskTotalPages}
                aria-label="下一页"
                onClick={() => changeTaskPage(Math.min(taskTotalPages, taskPage + 1))}
              >
                <span className="post-pagination-nav-content" aria-hidden="true">
                  <RightOutlined />
                </span>
              </Button>
              <Button
                className="post-pagination-nav"
                disabled={taskPage >= taskTotalPages}
                aria-label="回到尾页"
                onClick={() => changeTaskPage(taskTotalPages)}
              >
                <span className="post-pagination-nav-content" aria-hidden="true">
                  <span className="post-pagination-step-icon">
                    <RightOutlined />
                    <span className="post-pagination-step-bar" />
                  </span>
                </span>
              </Button>
            </div>
          ) : null}
        </div>
      </Drawer>
    </Layout>
  )
}
