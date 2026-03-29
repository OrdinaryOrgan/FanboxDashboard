import React, { useEffect, useMemo, useState } from 'react'
import ReactDOM from 'react-dom/client'
import { App as AntApp, ConfigProvider, theme as antdTheme } from 'antd'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import App from './App'
import './styles.css'

const queryClient = new QueryClient()
type ThemeMode = 'light' | 'dark'

const THEME_MODE_KEY = 'fanbox-dashboard-theme-mode'
const FOLLOW_SYSTEM_THEME_KEY = 'fanbox-dashboard-follow-system-theme'

function getSystemThemeMode(): ThemeMode {
  if (typeof window === 'undefined') return 'light'
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function getStoredThemeMode(): ThemeMode {
  if (typeof window === 'undefined') return 'light'
  const stored = window.localStorage.getItem(THEME_MODE_KEY)
  return stored === 'dark' ? 'dark' : 'light'
}

function getStoredFollowSystemTheme(): boolean {
  if (typeof window === 'undefined') return true
  const stored = window.localStorage.getItem(FOLLOW_SYSTEM_THEME_KEY)
  return stored == null ? true : stored === 'true'
}

function RootApp() {
  const [themeMode, setThemeMode] = useState<ThemeMode>(getStoredThemeMode)
  const [followSystemTheme, setFollowSystemTheme] = useState<boolean>(getStoredFollowSystemTheme)
  const [systemThemeMode, setSystemThemeMode] = useState<ThemeMode>(getSystemThemeMode)

  useEffect(() => {
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    const updateThemeMode = (event?: MediaQueryList | MediaQueryListEvent) => {
      const matches = 'matches' in (event ?? mediaQuery) ? (event ?? mediaQuery).matches : mediaQuery.matches
      setSystemThemeMode(matches ? 'dark' : 'light')
    }

    updateThemeMode(mediaQuery)

    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', updateThemeMode)
      return () => mediaQuery.removeEventListener('change', updateThemeMode)
    }

    mediaQuery.addListener(updateThemeMode)
    return () => mediaQuery.removeListener(updateThemeMode)
  }, [])

  useEffect(() => {
    window.localStorage.setItem(THEME_MODE_KEY, themeMode)
  }, [themeMode])

  useEffect(() => {
    window.localStorage.setItem(FOLLOW_SYSTEM_THEME_KEY, String(followSystemTheme))
  }, [followSystemTheme])

  const resolvedThemeMode = followSystemTheme ? systemThemeMode : themeMode

  const themeConfig = useMemo(
    () => ({
      algorithm: resolvedThemeMode === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
      token:
        resolvedThemeMode === 'dark'
          ? {
              colorPrimary: '#6f8cff',
              colorInfo: '#6f8cff',
              colorSuccess: '#86b89b',
              colorWarning: '#d0a56f',
              colorError: '#d67d76',
              borderRadius: 22,
              colorBgLayout: '#0d121a',
              colorBgContainer: '#121824',
              colorBgElevated: '#1a2130',
              colorText: '#edf3fb',
              colorTextSecondary: '#a8b4c8',
              colorBorderSecondary: '#2b3344',
              fontFamily: '"IBM Plex Sans", "Segoe UI", sans-serif',
            }
          : {
              colorPrimary: '#13796f',
              colorInfo: '#13796f',
              colorSuccess: '#61845b',
              colorWarning: '#be8750',
              colorError: '#b55d52',
              borderRadius: 22,
              colorBgLayout: '#f3ecdf',
              colorBgContainer: '#fffaf2',
              colorBgElevated: '#fffaf6',
              colorText: '#1d2628',
              colorTextSecondary: '#5f6c68',
              colorBorderSecondary: '#d9d2c7',
              fontFamily: '"IBM Plex Sans", "Segoe UI", sans-serif',
            },
    }),
    [resolvedThemeMode],
  )

  const handleToggleTheme = () => {
    setFollowSystemTheme(false)
    setThemeMode((current) => {
      const base = followSystemTheme ? resolvedThemeMode : current
      return base === 'dark' ? 'light' : 'dark'
    })
  }

  return (
    <ConfigProvider theme={themeConfig}>
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <App
            resolvedThemeMode={resolvedThemeMode}
            followSystemTheme={followSystemTheme}
            onToggleTheme={handleToggleTheme}
            onFollowSystemThemeChange={setFollowSystemTheme}
          />
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('app')!).render(
  <React.StrictMode>
    <RootApp />
  </React.StrictMode>,
)
