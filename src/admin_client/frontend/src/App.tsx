import { useState } from 'react'
import { Globe, Lock, Server, ArrowRight, Loader2, Eye, EyeOff, KeyRound } from 'lucide-react'
import Dashboard from './Dashboard'

// Словарь локализации
const translations = {
  en: {
    title: 'Admin Access',
    subtitle: 'Sign in to Financial AI Agent',
    serverLabel: 'Server Address',
    serverPlaceholder: 'admin.fin-ai-agent.ru',
    passwordLabel: 'Password',
    passwordPlaceholder: 'Enter your password',
    otpLabel: '2FA Code (TOTP)',
    otpPlaceholder: '6 digits from authenticator (if enabled)',
    loginBtn: 'Connect',
    connecting: 'Connecting...',
    errorAuth: 'Invalid password or server error',
    errorInvalidUrl: 'Invalid server address format',
    errorEmptyPassword: 'Password cannot be empty',
    errorInvalidDomain: 'Please enter a valid domain',
    errorUnknown: 'An unexpected error occurred',
    errorSessionExpired: 'Session expired. Please log in again.',
    errorBadOtp: 'Invalid 2FA TOTP code',
    errorEmptyOtp: '2FA code is required for this server',
    errorOtpReused: 'TOTP code already used. Please wait 30 seconds.',
    otpHelp: 'First time? Run "python scripts/setup_2fa.py" to scan QR code.',
  },
  ru: {
    title: 'Доступ администратора',
    subtitle: 'Войдите в Financial AI Agent',
    serverLabel: 'Адрес сервера',
    serverPlaceholder: 'admin.fin-ai-agent.ru',
    passwordLabel: 'Пароль',
    passwordPlaceholder: 'Введите ваш пароль',
    otpLabel: 'Код 2FA (TOTP)',
    otpPlaceholder: '6 цифр из приложения (если включено 2FA)',
    loginBtn: 'Подключиться',
    connecting: 'Соединение...',
    errorAuth: 'Неверный пароль или ошибка сервера',
    errorInvalidUrl: 'Неверный формат адреса сервера',
    errorEmptyPassword: 'Пароль не может быть пустым',
    errorInvalidDomain: 'Пожалуйста, введите корректный домен',
    errorUnknown: 'Произошла неизвестная ошибка',
    errorSessionExpired: 'Сессия истекла. Пожалуйста, войдите снова.',
    errorBadOtp: 'Неверный одноразовый код 2FA',
    errorEmptyOtp: 'Для входа на сервер требуется код 2FA',
    errorOtpReused: 'Код 2FA уже использован. Подождите 30 секунд.',
    otpHelp: 'Первый раз? Запустите "python scripts/setup_2fa.py" для сканирования QR.',
  }
}

function App() {
  const [lang, setLang] = useState<'en' | 'ru'>('ru')
  const [serverUrl, setServerUrl] = useState(() => localStorage.getItem('admin_server') || '')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [otpCode, setOtpCode] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState('')
  const [isAuthenticated, setIsAuthenticated] = useState(() => !!sessionStorage.getItem('admin_token'))

  const t = translations[lang]

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setIsLoading(true)

    try {
      let baseUrl = serverUrl.trim() || 'admin.fin-ai-agent.ru'
      
      if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
        baseUrl = 'https://' + baseUrl
      }

      // 1. Проверка на валидность URL-адреса
      try {
        new URL(baseUrl)
      } catch {
        throw new Error('errorInvalidUrl')
      }

      // 2. Проверка, что пароль не пустой
      if (!password.trim()) {
        throw new Error('errorEmptyPassword')
      }

      // 3. Запрещаем отправку запросов на локальные и приватные адреса, 
      // если только это не localhost (для тестов)
      const parsedUrl = new URL(baseUrl)
      if (parsedUrl.hostname !== 'localhost' && parsedUrl.hostname !== '127.0.0.1') {
        if (!parsedUrl.hostname.includes('.')) {
          throw new Error('errorInvalidDomain')
        }
      }

      // Используем локальный прокси Vite для обхода CORS только в dev-режиме
      let loginEndpoint = `${baseUrl.replace(/\/$/, '')}/login`
      if (import.meta.env.DEV && parsedUrl.hostname === 'admin.fin-ai-agent.ru') {
        loginEndpoint = '/api_proxy/login'
      }

      const formData = new URLSearchParams()
      formData.append('username', 'admin')
      formData.append('password', password)
      if (otpCode.trim()) {
        formData.append('otp_code', otpCode.trim())
      }

      const headers: Record<string, string> = {
        'Content-Type': 'application/x-www-form-urlencoded',
      }
      if (otpCode.trim()) {
        headers['X-OTP-Code'] = otpCode.trim()
      }

      const response = await fetch(loginEndpoint, {
        method: 'POST',
        headers,
        body: formData,
      })

      if (!response.ok) {
        const errData = await response.json().catch(() => null)
        const detail = errData?.detail || ''
        if (detail === '2FA code required') {
          throw new Error('errorEmptyOtp')
        } else if (detail === 'Invalid 2FA TOTP code') {
          throw new Error('errorBadOtp')
        } else if (typeof detail === 'string' && detail.includes('already been used')) {
          throw new Error('errorOtpReused')
        } else {
          throw new Error('errorAuth')
        }
      }

      const data = await response.json()
      
      if (data.access_token) {
        // Сохраняем токен в sessionStorage (автоматически очищается при закрытии сессии/окна)
        sessionStorage.setItem('admin_token', data.access_token)
        if (data.refresh_token) {
          sessionStorage.setItem('admin_refresh_token', data.refresh_token)
        }
        localStorage.setItem('admin_server', baseUrl)
        setIsAuthenticated(true)
      } else {
        throw new Error('errorAuth')
      }
    } catch (err: any) {
      const knownKeys = ['errorInvalidUrl', 'errorEmptyPassword', 'errorInvalidDomain', 'errorAuth', 'errorEmptyOtp', 'errorBadOtp', 'errorOtpReused']
      if (knownKeys.includes(err.message)) {
        setError(err.message)
      } else {
        setError('errorUnknown')
      }
    } finally {
      setIsLoading(false)
    }
  }

  const handleLogout = async (reason?: string) => {
    try {
      const token = sessionStorage.getItem('admin_token')
      const refreshToken = sessionStorage.getItem('admin_refresh_token')
      const serverUrl = localStorage.getItem('admin_server') || ''
      if (token && serverUrl && reason !== 'errorSessionExpired') {
        await fetch(`${serverUrl}/api/auth/logout`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ refresh_token: refreshToken })
        })
      }
    } catch (e) {
      console.error('Logout error:', e)
    } finally {
      sessionStorage.removeItem('admin_token')
      sessionStorage.removeItem('admin_refresh_token')
      setIsAuthenticated(false)
      if (reason) {
        setError(reason)
      }
    }
  }

  if (isAuthenticated) {
    return <Dashboard lang={lang} setLang={setLang} onLogout={handleLogout} />
  }

  return (
    <div className="min-h-full flex flex-col relative">

      {/* Шапка интерфейса: переключатель языка */}
      <div className="p-6 flex justify-end relative z-10">
        <button
          onClick={() => setLang(lang === 'en' ? 'ru' : 'en')}
          className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/5 hover:bg-white/10 transition-colors border border-white/10 text-sm font-medium backdrop-blur-sm"
        >
          <Globe className="w-4 h-4 text-purple-400" />
          {lang === 'en' ? 'RU' : 'EN'}
        </button>
      </div>

      {/* Основная форма входа в панель администратора */}
      <div className="flex-1 flex items-center justify-center p-6 relative z-10">
        <div className="w-full max-w-md">
          <div className="bg-[#1a0f3c]/60 backdrop-blur-xl border border-purple-500/20 rounded-2xl p-8 shadow-2xl">
            {/* Ключ key={lang} перезапускает монтирование блока при смене языка для запуска анимации */}
            <div key={lang} className="animate-fade-scale">
              <div className="text-center mb-8">
                <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-purple-600 to-indigo-600 shadow-lg shadow-purple-500/30 mb-6">
                  <Lock className="w-8 h-8 text-white" />
                </div>
                <h1 className="text-3xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-white to-purple-200">
                  {t.title}
                </h1>
                <p className="text-purple-300/70 mt-2 text-sm">
                  {t.subtitle}
                </p>
              </div>

              <form onSubmit={handleLogin} className="space-y-6">
              <div>
                <label className="block mb-3 text-sm font-medium text-purple-200 ml-1">
                  {t.serverLabel}
                </label>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                    <Server className="h-5 w-5 text-purple-400/50" />
                  </div>
                  <input
                    type="text"
                    value={serverUrl}
                    onChange={(e) => setServerUrl(e.target.value)}
                    placeholder={t.serverPlaceholder}
                    className="block w-full pl-11 pr-4 py-3 bg-black/20 border border-purple-500/20 rounded-xl focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all text-white placeholder-purple-300/30 outline-none"
                  />
                </div>
              </div>

              <div>
                <label className="block mb-3 text-sm font-medium text-purple-200 ml-1">
                  {t.passwordLabel}
                </label>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                    <Lock className="h-5 w-5 text-purple-400/50" />
                  </div>
                  <input
                    type={showPassword ? "text" : "password"}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder={t.passwordPlaceholder}
                    className="block w-full pl-11 pr-12 py-3 bg-black/20 border border-purple-500/20 rounded-xl focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all text-white placeholder-purple-300/30 outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute inset-y-0 right-0 pr-4 flex items-center text-purple-400/50 hover:text-purple-300 focus:outline-none transition-colors"
                  >
                    {showPassword ? (
                      <EyeOff className="h-5 w-5" />
                    ) : (
                      <Eye className="h-5 w-5" />
                    )}
                  </button>
                </div>
              </div>

              <div>
                <label className="block mb-3 text-sm font-medium text-purple-200 ml-1">
                  {t.otpLabel}
                </label>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                    <KeyRound className="h-5 w-5 text-purple-400/50" />
                  </div>
                  <input
                    type="text"
                    maxLength={6}
                    value={otpCode}
                    onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, ''))}
                    placeholder={t.otpPlaceholder}
                    className="block w-full pl-11 pr-4 py-3 bg-black/20 border border-purple-500/20 rounded-xl focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all text-white placeholder-purple-300/30 outline-none tracking-widest font-mono text-center text-lg"
                  />
                </div>
                <p className="text-xs text-purple-300/40 mt-1.5 ml-1">
                  {t.otpHelp}
                </p>
              </div>

              {error && (
                <div key={error} className="animate-slide-down p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm text-center">
                  {t[error as keyof typeof t]}
                </div>
              )}

              <button
                type="submit"
                disabled={isLoading}
                className="w-full flex items-center justify-center gap-2 py-3 px-4 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white font-medium rounded-xl transition-all shadow-lg shadow-purple-500/25 active:scale-[0.98] disabled:opacity-70 disabled:cursor-not-allowed group"
              >
                {isLoading ? (
                  <Loader2 className="w-5 h-5 animate-spin" />
                ) : (
                  <>
                    <span>{t.loginBtn}</span>
                    <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
                  </>
                )}
              </button>
            </form>
          </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default App
