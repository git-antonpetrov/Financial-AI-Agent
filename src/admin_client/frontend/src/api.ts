import { invoke } from '@tauri-apps/api/core'

/**
 * Получает криптографический токен IPC рукопожатия, сгенерированный процессом Tauri.
 */
export const getLocalSidecarSecret = async (): Promise<string> => {
  try {
    return await invoke<string>('get_sidecar_secret')
  } catch {
    return ''
  }
}

/**
 * Получает динамический базовый URL локального Python Sidecar (BFF).
 */
export const getLocalSidecarUrl = async (): Promise<string> => {
  try {
    return await invoke<string>('get_sidecar_url')
  } catch {
    return 'http://127.0.0.1:8005'
  }
}

/**
 * Выполняет защищенный HTTP-запрос к локальному Sidecar (BFF)
 * с автоматическим добавлением заголовка X-Local-Secret.
 */
export const localFetch = async (endpoint: string, options: RequestInit = {}): Promise<Response> => {
  const baseUrl = await getLocalSidecarUrl()
  const secret = await getLocalSidecarSecret()
  const headers = new Headers(options.headers || {})
  if (secret && !headers.has('X-Local-Secret')) {
    headers.set('X-Local-Secret', secret)
  }
  const cleanEndpoint = endpoint.startsWith('/') ? endpoint : `/${endpoint}`
  return await fetch(`${baseUrl}${cleanEndpoint}`, {
    ...options,
    headers,
  })
}
