// 自定义背景图：按预设 id 存在本机 IndexedDB。

export const WALLPAPER_MAX_BYTES = 2 * 1024 * 1024
const TYPES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif'])
const DB_NAME = 'aiink-theme'
const STORE = 'files'
const LEGACY_KEY = 'wallpaper'

const memory = new Map<string, Blob>()

export function wallpaperError(file: File): string | null {
  if (!TYPES.has(file.type)) return '只要 jpg / png / webp / gif'
  if (file.size > WALLPAPER_MAX_BYTES) return '图片不要超过 2 MB'
  return null
}

function openDb(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === 'undefined') return Promise.resolve(null)
  return new Promise((resolve) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE)
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => resolve(null)
  })
}

function getBlob(db: IDBDatabase, key: string): Promise<Blob | null> {
  return new Promise((resolve) => {
    const req = db.transaction(STORE, 'readonly').objectStore(STORE).get(key)
    req.onsuccess = () => resolve(req.result instanceof Blob ? req.result : null)
    req.onerror = () => resolve(null)
  })
}

export async function readWallpaper(id: string): Promise<Blob | null> {
  const db = await openDb()
  if (!db) return memory.get(id) ?? (id === 'legacy' ? memory.get(LEGACY_KEY) ?? null : null)
  const found = await getBlob(db, id)
  if (found) return found
  if (id !== 'legacy') return null
  return getBlob(db, LEGACY_KEY)
}

export async function writeWallpaper(id: string, blob: Blob): Promise<void> {
  memory.set(id, blob)
  const db = await openDb()
  if (!db) return
  await new Promise<void>((resolve) => {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).put(blob, id)
    tx.oncomplete = () => resolve()
    tx.onerror = () => resolve()
  })
}

export async function clearWallpaper(id: string): Promise<void> {
  memory.delete(id)
  if (id === 'legacy') memory.delete(LEGACY_KEY)
  const db = await openDb()
  if (!db) return
  await new Promise<void>((resolve) => {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).delete(id)
    if (id === 'legacy') tx.objectStore(STORE).delete(LEGACY_KEY)
    tx.oncomplete = () => resolve()
    tx.onerror = () => resolve()
  })
}
