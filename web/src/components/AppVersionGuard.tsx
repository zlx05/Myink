import { useEffect } from 'react'
import { bundlePathFromIndex, isDifferentBundle } from './AppVersionGuard.utils'

const CHECK_INTERVAL_MS = 30_000

function activeBundlePath(): string | null {
  const script = Array.from(document.scripts).find((item) =>
    item.src.includes('/assets/index-'),
  )
  if (!script) return null

  try {
    return new URL(script.src, window.location.href).pathname
  } catch {
    return null
  }
}

/**
 * 检测网关是否已经发布了新的前端入口。旧 SPA 仍在浏览器内存中运行时，
 * 它不会自动加载容器里的新 bundle；发现入口 hash 改变后刷新一次即可切换。
 */
export function AppVersionGuard() {
  useEffect(() => {
    let stopped = false
    let checking = false

    const check = async () => {
      if (checking || stopped) return
      checking = true
      try {
        const response = await fetch(`/?_myink_version=${Date.now()}`, {
          cache: 'no-store',
          credentials: 'same-origin',
        })
        if (!response.ok || stopped) return

        const latest = bundlePathFromIndex(await response.text())
        if (isDifferentBundle(activeBundlePath(), latest)) {
          window.location.reload()
        }
      } catch {
        // 网关重启或短时断网时保持当前页面，下次轮询再检查。
      } finally {
        checking = false
      }
    }

    const onFocus = () => void check()
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') void check()
    }

    const timer = window.setInterval(() => void check(), CHECK_INTERVAL_MS)
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibilityChange)
    void check()

    return () => {
      stopped = true
      window.clearInterval(timer)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [])

  return null
}
