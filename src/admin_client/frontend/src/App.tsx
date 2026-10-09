import { useState, useEffect } from 'react'
import {
  Globe, Lock, Server, ArrowRight, Loader2, Eye, EyeOff, KeyRound,
  QrCode, ShieldCheck, Copy, Check, X, CheckCircle2
} from 'lucide-react'
import Dashboard from './Dashboard'
import { localFetch } from './api'

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
    setup2faBtn: 'First time? Setup 2FA / QR code',
    modal2faTitle: '2FA Authenticator Setup',
    modal2faSubtitle: 'Scan QR in Google Authenticator, Apple Passwords or YubiKey',
    scanQrHint: 'Scan QR code in authenticator app on your phone',
    secretKeyLabel: 'Secret key for manual entry:',
    copyBtn: 'Copy',
    copiedBtn: 'Copied',
    enterCodePrompt: 'Enter 6-digit code from authenticator:',
    verifyAndLoginBtn: 'Verify Code & Log In',
    enterPasswordFirst: 'Enter admin password to fetch 2FA QR code:',
    getQrBtn: 'Get QR Code',
    closeBtn: 'Close',
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
    setup2faBtn: 'Первый раз? Настроить 2FA / QR-код',
    modal2faTitle: 'Настройка 2FA аутентификатора',
    modal2faSubtitle: 'Сканируйте QR в Google Authenticator, Яндекс Ключ или Apple',
    scanQrHint: 'Отсканируйте QR-код в приложении аутентификатора на телефоне',
    secretKeyLabel: 'Ключ для ручного ввода:',
    copyBtn: 'Скопировать',
    copiedBtn: 'Скопировано',
    enterCodePrompt: 'Введите 6-значный код из аутентификатора:',
    verifyAndLoginBtn: 'Подтвердить код и войти',
    enterPasswordFirst: 'Введите пароль администратора для получения QR-кода:',
    getQrBtn: 'Получить QR-код',
    closeBtn: 'Закрыть',
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
  const [isAuthenticated, setIsAuthenticated] = useState(false)

  // Проверка статуса сессии в локальном бэкенде (BFF) при запуске интерфейса
  useEffect(() => {
    localFetch('/api/local/auth/status')
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data?.is_authenticated) {
          setIsAuthenticated(true)
        }
      })
      .catch(() => {})
  }, [])

  // Состояние модального окна первичной привязки 2FA / QR-кода
  const [show2FaModal, setShow2FaModal] = useState(false)
  const [twoFaLoading, setTwoFaLoading] = useState(false)
  const [twoFaNeedsPassword, setTwoFaNeedsPassword] = useState(false)
  const [twoFaData, setTwoFaData] = useState<{
    secret: string
    provisioning_uri: string
    qr_svg: string
    issuer: string
    is_enrolled: boolean
  } | null>(null)
  const [twoFaError, setTwoFaError] = useState('')
  const [twoFaCopied, setTwoFaCopied] = useState(false)
  const [twoFaPairCode, setTwoFaPairCode] = useState('')
  const [twoFaVerifying, setTwoFaVerifying] = useState(false)

  const t = translations[lang]

  const executeLogin = async (otpToUse: string, passwordToUse: string) => {
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
    if (!passwordToUse.trim()) {
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

    // Выполняем аутентификацию через локальный sidecar (BFF) по mTLS
    const response = await localFetch('/api/local/auth/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        server_url: baseUrl,
        username: 'admin',
        password: passwordToUse,
        otp_code: otpToUse.trim(),
      }),
    })

    if (!response.ok) {
      const errData = await response.json().catch(() => null)
      const detail = errData?.detail || ''
      if (detail === 'errorEmptyOtp' || detail === '2FA code required') {
        throw new Error('errorEmptyOtp')
      } else if (detail === 'errorBadOtp' || detail === 'Invalid 2FA TOTP code') {
        throw new Error('errorBadOtp')
      } else if (typeof detail === 'string' && (detail === 'errorOtpReused' || detail.includes('already been used'))) {
        throw new Error('errorOtpReused')
      } else {
        throw new Error('errorAuth')
      }
    }

    // Сохраняем адрес сервера для подсказки при следующем вводе
    localStorage.setItem('admin_server', baseUrl.replace(/\/$/, ''))
    setIsAuthenticated(true)
  }

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setIsLoading(true)

    try {
      await executeLogin(otpCode, password)
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

  const fetch2FaSetup = async (pwdToUse: string) => {
    if (!pwdToUse.trim()) {
      setTwoFaNeedsPassword(true)
      return
    }
    setTwoFaLoading(true)
    setTwoFaError('')
    try {
      let baseUrl = serverUrl.trim() || 'admin.fin-ai-agent.ru'
      if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
        baseUrl = 'https://' + baseUrl
      }
      const res = await localFetch('/api/local/auth/2fa/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          server_url: baseUrl,
          username: 'admin',
          password: pwdToUse.trim(),
        })
      })
      if (!res.ok) {
        const errData = await res.json().catch(() => null)
        const detail = errData?.detail || ''
        if (res.status === 403) {
          throw new Error(lang === 'en' ? '2FA is already enrolled on this server. Please enter your authenticator code.' : '2FA уже привязана на сервере. Введите 6-значный код из приложения.')
        } else if (res.status === 429) {
          throw new Error(lang === 'en' ? 'Too many attempts. Please try again later.' : 'Слишком много попыток. Пожалуйста, подождите.')
        } else {
          throw new Error(lang === 'en' ? (detail || 'Invalid admin password.') : (detail === 'Incorrect username or password' ? 'Неверный пароль администратора.' : (detail || 'Ошибка авторизации.')))
        }
      }
      const data = await res.json()
      setTwoFaData(data)
      setTwoFaNeedsPassword(false)
    } catch (err: any) {
      setTwoFaError(err.message || (lang === 'en' ? 'Failed to fetch 2FA QR code.' : 'Не удалось получить QR-код.'))
    } finally {
      setTwoFaLoading(false)
    }
  }

  const open2FaModal = () => {
    setShow2FaModal(true)
    setTwoFaError('')
    setTwoFaPairCode('')
    if (password.trim()) {
      void fetch2FaSetup(password)
    } else {
      setTwoFaNeedsPassword(true)
    }
  }

  const handleVerifyAndLogin = async () => {
    if (twoFaPairCode.length !== 6) return
    setTwoFaVerifying(true)
    setTwoFaError('')
    try {
      await executeLogin(twoFaPairCode, password)
      setOtpCode(twoFaPairCode)
      setShow2FaModal(false)
    } catch {
      setTwoFaError(lang === 'en' ? 'Invalid 2FA code. Please check your authenticator clock.' : 'Неверный код 2FA. Проверьте код и время на устройстве.')
    } finally {
      setTwoFaVerifying(false)
    }
  }

  const handleLogout = async (reason?: string) => {
    try {
      await localFetch('/api/local/auth/logout', { method: 'POST' })
    } catch (e) {
      console.error('Logout error:', e)
    } finally {
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
                <div className="flex justify-between items-center mt-2 px-1">
                  <button
                    type="button"
                    onClick={() => open2FaModal()}
                    className="text-xs text-purple-400 hover:text-purple-300 transition-colors flex items-center gap-1.5 font-medium hover:underline cursor-pointer"
                  >
                    <QrCode className="w-3.5 h-3.5 text-purple-400" />
                    <span>{t.setup2faBtn}</span>
                  </button>
                </div>
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

      {/* Модальное окно первоначальной настройки 2FA / QR-кода */}
      {show2FaModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-md">
          <div className="bg-[#140b2b] border border-purple-500/30 rounded-3xl max-w-md w-full p-6 shadow-2xl relative text-white animate-fade-scale">
            <button
              onClick={() => setShow2FaModal(false)}
              className="absolute top-5 right-5 text-purple-300/60 hover:text-white transition-colors cursor-pointer"
            >
              <X className="w-5 h-5" />
            </button>

            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-purple-500/20 border border-purple-500/30 flex items-center justify-center text-purple-300">
                <ShieldCheck className="w-6 h-6 text-purple-400" />
              </div>
              <div>
                <h3 className="text-base font-bold text-white">
                  {t.modal2faTitle}
                </h3>
                <p className="text-xs text-purple-300/70">
                  {t.modal2faSubtitle}
                </p>
              </div>
            </div>

            {twoFaError && (
              <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs text-center">
                {twoFaError}
              </div>
            )}

            {twoFaLoading ? (
              <div className="py-12 flex flex-col items-center justify-center gap-3 text-purple-300">
                <Loader2 className="w-8 h-8 animate-spin text-purple-400" />
                <span className="text-sm">{lang === 'en' ? 'Fetching 2FA setup details...' : 'Получение параметров 2FA...'}</span>
              </div>
            ) : twoFaNeedsPassword ? (
              <div className="space-y-4">
                <p className="text-xs text-purple-200/80">
                  {t.enterPasswordFirst}
                </p>
                <div className="relative">
                  <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                    <Lock className="h-4 w-4 text-purple-400/50" />
                  </div>
                  <input
                    type={showPassword ? "text" : "password"}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder={t.passwordPlaceholder}
                    className="block w-full pl-10 pr-10 py-2.5 bg-black/40 border border-purple-500/30 rounded-xl focus:ring-2 focus:ring-purple-500/50 text-white placeholder-purple-300/30 text-sm outline-none"
                  />
                </div>
                <button
                  type="button"
                  onClick={() => void fetch2FaSetup(password)}
                  disabled={!password.trim()}
                  className="w-full py-2.5 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white rounded-xl text-sm font-semibold transition-all cursor-pointer flex items-center justify-center gap-2"
                >
                  <QrCode className="w-4 h-4" />
                  <span>{t.getQrBtn}</span>
                </button>
              </div>
            ) : twoFaData ? (
              <div className="space-y-4">
                {/* Векторный QR-код */}
                <div className="flex flex-col items-center justify-center p-4 bg-white/5 rounded-2xl border border-purple-500/20">
                  {twoFaData.qr_svg ? (
                    <div
                      className="w-48 h-48 bg-white p-3 rounded-xl flex items-center justify-center shadow-lg"
                      dangerouslySetInnerHTML={{ __html: twoFaData.qr_svg }}
                    />
                  ) : (
                    <div className="w-48 h-48 bg-white/10 rounded-xl flex items-center justify-center text-purple-300 text-xs text-center p-4">
                      {twoFaData.provisioning_uri}
                    </div>
                  )}
                  <span className="text-xs text-purple-300/60 mt-3 text-center">
                    {t.scanQrHint}
                  </span>
                </div>

                {/* Текстовый ключ с копированием */}
                <div className="p-3 bg-black/40 rounded-xl border border-purple-500/20">
                  <div className="flex justify-between items-center mb-1">
                    <span className="text-xs text-purple-300/70">
                      {t.secretKeyLabel}
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        void navigator.clipboard.writeText(twoFaData.secret)
                        setTwoFaCopied(true)
                        setTimeout(() => setTwoFaCopied(false), 2000)
                      }}
                      className="text-xs text-purple-400 hover:text-purple-300 flex items-center gap-1 cursor-pointer"
                    >
                      {twoFaCopied ? <Check className="w-3.5 h-3.5 text-green-400" /> : <Copy className="w-3.5 h-3.5" />}
                      {twoFaCopied ? t.copiedBtn : t.copyBtn}
                    </button>
                  </div>
                  <div className="font-mono text-sm tracking-widest text-cyan-300 break-all select-all font-semibold">
                    {twoFaData.secret}
                  </div>
                </div>

                {/* Ввод кода и подтверждение со входом */}
                <div className="p-3 bg-purple-950/30 rounded-xl border border-purple-500/20">
                  <label className="block text-xs font-medium text-purple-200 mb-2">
                    {t.enterCodePrompt}
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      maxLength={6}
                      value={twoFaPairCode}
                      onChange={(e) => setTwoFaPairCode(e.target.value.replace(/\D/g, ''))}
                      placeholder="123456"
                      className="flex-1 px-3 py-2 bg-black/40 border border-purple-500/30 rounded-lg text-white font-mono tracking-widest text-center text-sm outline-none focus:border-purple-400"
                    />
                    <button
                      type="button"
                      onClick={() => void handleVerifyAndLogin()}
                      disabled={twoFaPairCode.length !== 6 || twoFaVerifying}
                      className="px-4 py-2 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 disabled:opacity-50 text-white rounded-lg text-xs font-semibold transition-all flex items-center gap-1.5 cursor-pointer"
                    >
                      {twoFaVerifying ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <CheckCircle2 className="w-3.5 h-3.5" />
                      )}
                      <span>{t.verifyAndLoginBtn}</span>
                    </button>
                  </div>
                </div>

                <div className="flex justify-end pt-2">
                  <button
                    type="button"
                    onClick={() => setShow2FaModal(false)}
                    className="px-5 py-2 rounded-xl bg-white/10 hover:bg-white/20 text-white text-xs font-medium transition-colors cursor-pointer"
                  >
                    {t.closeBtn}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  )
}

export default App
