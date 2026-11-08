import { invoke } from '@tauri-apps/api/core'

/**
 * Получает криптографический токен IPC рукопожатия, сгенерированный процессом Tauri.
 */
export const getLocalSidecarSecret = async (): Promise<string> => {
  const secret = await invoke<string>('get_sidecar_secret')
  if (!secret) {
    throw new Error('Секрет IPC sidecar не получен')
  }
  return secret
}

/**
 * Получает динамический базовый URL локального Python Sidecar (BFF).
 */
export const getLocalSidecarUrl = async (): Promise<string> => {
  const url = await invoke<string>('get_sidecar_url')
  if (!url) {
    throw new Error('Адрес sidecar не получен')
  }
  return url
}

/**
 * Выполняет защищенный HTTP-запрос к локальному Sidecar (BFF)
 * с автоматическим добавлением заголовка X-Local-Secret.
 */
export const localFetch = async (endpoint: string, options: RequestInit = {}): Promise<Response> => {
  const baseUrl = await getLocalSidecarUrl()
  const secret = await getLocalSidecarSecret()
  const headers = new Headers(options.headers || {})
  if (!headers.has('X-Local-Secret')) {
    headers.set('X-Local-Secret', secret)
  }
  const cleanEndpoint = endpoint.startsWith('/') ? endpoint : `/${endpoint}`
  return await fetch(`${baseUrl}${cleanEndpoint}`, {
    ...options,
    headers,
  })
}
