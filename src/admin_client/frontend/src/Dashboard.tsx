import { useState, useRef, useEffect, useCallback } from 'react'
import { Users, FileText, UploadCloud, Clock, CheckCircle2, Globe, Server, Check, X, LogOut, RefreshCw, KeyRound, Shield, ShieldCheck, ShieldAlert } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { localFetch, getLocalSidecarSecret, getLocalSidecarUrl } from './api'

type Tab = 'rag' | 'agents' | 'audit'

interface AuditLogItem {
  id: number
  timestamp: string
  actor: string
  action: string
  status: string
  resource?: string
  actor_ip?: string
  details?: any
  prev_hash?: string
  current_hash?: string
}

interface LocalFile {
  id: string
  file: File
  name: string
  status: 'pending' | 'processing' | 'sent' | 'error' | 'skipped'
  elapsedSeconds: number
  agent_name?: string
  job_id?: string
  display_message?: string
}

// Структура заявки нормативного документа от агента (из таблицы agent_requests)
interface AgentRequest {
  id: number
  agent_name: string
  document_name_ru: string
  document_name_en: string
  justification_ru: string
  justification_en: string
  status: string
  created_at?: string
}


interface DashboardProps {
  lang: 'en' | 'ru'
  setLang: (lang: 'en' | 'ru') => void
  onLogout?: (reason?: string) => void
}

const formatAgentName = (name: string, lang: 'en' | 'ru') => {
  if (!name) return '';
  const dictionary: Record<string, { en: string, ru: string }> = {
    main: { en: 'Main agent', ru: 'Главный агент' },
    bank: { en: 'Financial agent', ru: 'Финансовый агент' },
    invest: { en: 'Invest agent', ru: 'Инвестиционный агент' },
    digital: { en: 'Digital agent', ru: 'Цифровой агент' },
  };

  const lowerName = name.toLowerCase();
  if (dictionary[lowerName]) {
    return dictionary[lowerName][lang];
  }

  const capitalized = name.charAt(0).toUpperCase() + name.slice(1);
  return `${capitalized} ${lang === 'en' ? 'agent' : 'агент'}`;
};

const translateStatusMessage = (msg: string | undefined, lang: 'en' | 'ru') => {
  if (!msg) return '';
  if (lang === 'ru') return msg;

  let translatedMsg = msg;

  // Сначала обрабатываем динамические переводы по регулярным выражениям
  const successRegex = /^Успешно! Документ (.*) отправлен в очередь на обработку\.$/;
  const match = translatedMsg.match(successRegex);
  if (match) {
    translatedMsg = `Success! Document ${match[1]} sent to processing queue.`;
    return translatedMsg; // Возвращает переведенную строку при совпадении с шаблоном
  }

  const convertRegex = /^Шаг 2: Конвертация (.*) в PDF\.\.\.$/;
  const convertMatch = translatedMsg.match(convertRegex);
  if (convertMatch) {
    return `Step 2: Converting ${convertMatch[1]} to PDF...`;
  }

  const translations: Record<string, string> = {
    'В очереди...': 'In queue...',
    'Отправка файла...': 'Sending file...',
    'Запуск...': 'Starting...',
    'Соединение прервано': 'Connection lost',
    'Ошибка загрузки': 'Upload Failed',
    'Успешно! Документ отправлен в очередь на обработку.': 'Success! Document sent to processing queue.',
    'Пропущен: Точная копия файла уже находится в процессе обработки локально': 'Skipped: Exact copy is already being processed locally',
    'Пропущен: Точная копия файла уже загружена (дубликат)': 'Skipped: Exact copy already loaded (duplicate)',
    'Пропущен: У нас уже загружена более новая версия этого документа': 'Skipped: A newer version is already loaded',
    'Ошибка сети при связи с сервером': 'Network error communicating with server',
    'Критическая ошибка:': 'Critical error:',
    'Шаг 1: Вычисление MD5 и проверка дубликатов...': 'Step 1: MD5 hash and duplicate check...',
    'Шаг 2: Извлечение текста (PyMuPDF)...': 'Step 2: Text extraction (PyMuPDF)...',
    'Шаг 3: Распознавание текста (Content AI)...': 'Step 3: OCR (Content AI)...',
    'Шаг 4: Анализ названия и типа документа (Gemini)...': 'Step 4: Title and type analysis (Gemini)...',
    'Шаг 5: Проверка актуальности версии...': 'Step 5: Version validation...',
    'Шаг 6: Поиск отмененных актов (Gemini)...': 'Step 6: Searching for repealed acts (Gemini)...',
    'Шаг 6: Поиск отмененных документов...': 'Step 6: Searching for repealed acts (Gemini)...', // Предоставляет запасной вариант сопоставления
    'Шаг 7: Подготовка и загрузка на сервер (Векторизация)...': 'Step 7: Preparation and upload (Vectorization)...',
    'Шаг 8: Отправка списка устаревших актов на удаление...': 'Step 8: Sending repealed acts for deletion...'
  };

  for (const [ru, en] of Object.entries(translations)) {
    if (translatedMsg.includes(ru)) {
      translatedMsg = translatedMsg.replace(ru, en);
      break;
    }
  }
  return translatedMsg;
};



export default function Dashboard({ lang, setLang, onLogout }: DashboardProps) {
  const [activeTab, setActiveTab] = useState<Tab>('rag')
  const [localFiles, setLocalFiles] = useState<LocalFile[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [requests, setRequests] = useState<AgentRequest[]>([])
  const [selectedRequests, setSelectedRequests] = useState<Set<number>>(new Set())
  const [isLoadingRequests, setIsLoadingRequests] = useState(false)

  // Состояние модального окна одобрения
  const [isApproveModalOpen, setIsApproveModalOpen] = useState(false)
  const [approvingRequests, setApprovingRequests] = useState<AgentRequest[]>([])
  const [requestFiles, setRequestFiles] = useState<Record<number, File>>({})
  const [selectedAgent, setSelectedAgent] = useState('main')
  // Состояние управления ключами и безопасностью агентов
  const [showAgentKeysModal, setShowAgentKeysModal] = useState(false)
  const [agentList, setAgentList] = useState<any[]>([])
  const [loadingAgents, setLoadingAgents] = useState(false)

  const langRef = useRef(lang);
  useEffect(() => {
    langRef.current = lang;
  }, [lang]);

  const fetchRequests = useCallback(async () => {
    setIsLoadingRequests(true)
    try {
      const res = await localFetch('/api/local/agent-requests')
      if (res.status === 401) {
        onLogout?.('errorSessionExpired')
        return
      }
      if (res.ok) {
        const data = await res.json()
        setRequests(data)
      }
    } catch (e) {
      console.error(e)
    } finally {
      setIsLoadingRequests(false)
    }
  }, [onLogout])

  const fetchAgentList = useCallback(async () => {
    setLoadingAgents(true)
    try {
      const res = await localFetch('/api/local/agents')
      if (res.status === 401) {
        onLogout?.('errorSessionExpired')
        return
      }
      if (res.ok) {
        const data = await res.json()
        setAgentList(data)
      }
    } catch (e) {
      console.error(e)
    } finally {
      setLoadingAgents(false)
    }
  }, [onLogout])

  const handleSuspendAgent = async (agentName: string) => {
    try {
      const res = await localFetch(`/api/local/agents/${agentName}/suspend`, { method: 'POST' })
      if (res.status === 401) {
        onLogout?.('errorSessionExpired')
        return
      }
      void fetchAgentList()
    } catch (e) {
      console.error(e)
    }
  }

  const handleReactivateAgent = async (agentName: string) => {
    try {
      const res = await localFetch(`/api/local/agents/${agentName}/reactivate`, { method: 'POST' })
      if (res.status === 401) {
        onLogout?.('errorSessionExpired')
        return
      }
      void fetchAgentList()
    } catch (e) {
      console.error(e)
    }
  }

  const handleRevokeKey = async (agentName: string, kid?: string) => {
    const reason = prompt(lang === 'en' ? 'Revocation reason:' : 'Причина отзыва:') || 'Compromised'
    try {
      const res = await localFetch(`/api/local/agents/${agentName}/revoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kid, reason })
      })
      if (res.status === 401) {
        onLogout?.('errorSessionExpired')
        return
      }
      void fetchAgentList()
    } catch (e) {
      console.error(e)
    }
  }

  // Состояние неизменяемого журнала аудита (Zero-Trust Audit Trail)
  const [auditLogs, setAuditLogs] = useState<AuditLogItem[]>([])
  const [auditVerifyResult, setAuditVerifyResult] = useState<{
    status: string
    verified: boolean
    total_events?: number
    verified_events?: number
    message?: string
  } | null>(null)
  const [isLoadingAudit, setIsLoadingAudit] = useState(false)

  const fetchAuditData = useCallback(async () => {
    setIsLoadingAudit(true)
    try {
      const [logsRes, verifyRes] = await Promise.all([
        localFetch('/api/local/audit/logs?limit=50'),
        localFetch('/api/local/audit/verify')
      ])
      if (logsRes.status === 401 || verifyRes.status === 401) {
        onLogout?.('errorSessionExpired')
        return
      }
      if (logsRes.ok) {
        const data = await logsRes.json()
        setAuditLogs(data.items || [])
      }
      if (verifyRes.ok) {
        const vData = await verifyRes.json()
        setAuditVerifyResult(vData)
      }
    } catch (e) {
      console.error(e)
    } finally {
      setIsLoadingAudit(false)
    }
  }, [onLogout])

  useEffect(() => {
    if (activeTab === 'agents') {
      const timer = setTimeout(() => {
        void fetchRequests()
      }, 0)
      return () => clearTimeout(timer)
    } else if (activeTab === 'audit') {
      const timer = setTimeout(() => {
        void fetchAuditData()
      }, 0)
      return () => clearTimeout(timer)
    }
  }, [activeTab, fetchRequests, fetchAuditData])

  const toggleSelection = (id: number) => {
    const newSet = new Set(selectedRequests)
    if (newSet.has(id)) {
      newSet.delete(id)
    } else {
      newSet.add(id)
    }
    setSelectedRequests(newSet)
  }

  const rejectRequests = async (ids: number[]) => {
    const prevRequests = requests
    setRequests(prev => prev.map(r => ids.includes(r.id) ? { ...r, status: 'rejected' } : r))
    setSelectedRequests(new Set())
    try {
      const res = await localFetch('/api/local/agent-requests/reject', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ request_ids: ids })
      })
      if (res.status === 401) {
        onLogout?.('errorSessionExpired')
        return
      }
      if (!res.ok) {
        throw new Error(`Reject failed with status ${res.status}`)
      }
      fetchRequests()
    } catch (e) {
      console.error(e)
      setRequests(prevRequests)
    }
  }

  const handleRejectSelected = () => rejectRequests(Array.from(selectedRequests))
  const handleRejectSingle = (id: number) => rejectRequests([id])

  const openApproveModal = (reqs: AgentRequest[]) => {
    setApprovingRequests(reqs)
    setRequestFiles({})
    setIsApproveModalOpen(true)
  }

  const handleApproveSelected = () => {
    const reqs = requests.filter(r => selectedRequests.has(r.id))
    openApproveModal(reqs)
  }

  const handleFileForRequest = (reqId: number, file: File) => {
    setRequestFiles(prev => ({ ...prev, [reqId]: file }))
  }

  const confirmApprove = async () => {
    const prevRequests = requests
    const ids = approvingRequests.map(r => r.id)
    try {
      setRequests(prev => prev.map(r => ids.includes(r.id) ? { ...r, status: 'approved' } : r))

      // Добавляем файлы в очередь вкладки RAG
      const newLocalFiles = approvingRequests.map(req => {
        const file = requestFiles[req.id]
        return {
          id: Math.random().toString(36).substring(7),
          file: file,
          name: file ? file.name : 'Unknown file',
          status: 'pending' as const,
          elapsedSeconds: 0,
          agent_name: req.agent_name,
          display_message: 'В очереди...'
        }
      }).filter(f => f.file !== undefined)

      setLocalFiles(prev => [...prev, ...newLocalFiles])

      setIsApproveModalOpen(false)
      setSelectedRequests(new Set())
      setActiveTab('rag')

      // Обновляем статусы через локальный BFF
      const res = await localFetch('/api/local/agent-requests/approve', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ request_ids: ids })
      })
      if (res.status === 401) {
        onLogout?.('errorSessionExpired')
        return
      }
      if (!res.ok) {
        throw new Error(`Approve failed with status ${res.status}`)
      }
      fetchRequests()
    } catch (e: any) {
      console.error('Error in confirmApprove:', e)
      setRequests(prevRequests)
    }
  }

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    try {
      if (e.target.files && e.target.files.length > 0) {
        const newFiles = Array.from(e.target.files).map((f) => ({
          id: Math.random().toString(36).substring(7),
          file: f,
          name: f.name,
          status: 'pending' as const,
          elapsedSeconds: 0,
          agent_name: selectedAgent || 'main', // Агент по умолчанию для ручной загрузки
          display_message: 'В очереди...'
        }))
        setLocalFiles((prev) => [...prev, ...newFiles])
      }
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    } catch (e: any) {
      console.error('Error in handleFileUpload:', e)
    }
  }

  const processFile = useCallback(async (fileObj: LocalFile) => {
    try {
      // Устанавливает статус обработки для исключения повторного захвата
      setLocalFiles(prev => prev.map(f => f.id === fileObj.id ? { ...f, status: 'processing', display_message: 'Отправка файла...' } : f))

      const formData = new FormData()
      formData.append('file', fileObj.file)
      formData.append('agent_name', fileObj.agent_name || 'main')

      const localSecret = await getLocalSidecarSecret()
      const sidecarBaseUrl = await getLocalSidecarUrl()

      const uploadHeaders: Record<string, string> = {}
      if (localSecret) {
        uploadHeaders['X-Local-Secret'] = localSecret
      }

      const uploadRes = await fetch(`${sidecarBaseUrl}/api/local/process`, {
        method: 'POST',
        headers: uploadHeaders,
        body: formData
      })

      if (uploadRes.status === 401) {
        onLogout?.('errorSessionExpired')
        throw new Error('Session expired (401)')
      }

      if (!uploadRes.ok) throw new Error('Upload failed with status ' + uploadRes.status)

      const { job_id } = await uploadRes.json()

      setLocalFiles(prev => prev.map(f => f.id === fileObj.id ? { ...f, job_id } : f))

      // Подключается к потоку SSE для отслеживания прогресса обработки с валидацией токена
      const progressUrl = localSecret 
        ? `${sidecarBaseUrl}/api/local/progress/${job_id}?secret=${encodeURIComponent(localSecret)}`
        : `${sidecarBaseUrl}/api/local/progress/${job_id}`
      const eventSource = new EventSource(progressUrl)

      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          const msg = data.message;

          if (data.status === 'done' || data.status === 'completed') {
            setLocalFiles(prev => prev.map(f => f.id === fileObj.id ? { ...f, status: 'sent', display_message: msg } : f))
            processingRef.current.delete(fileObj.id)
            eventSource.close()
          } else if (data.status === 'error') {
            setLocalFiles(prev => prev.map(f => f.id === fileObj.id ? { ...f, status: 'error', display_message: msg } : f))
            processingRef.current.delete(fileObj.id)
            eventSource.close()
          } else if (data.status === 'skipped') {
            setLocalFiles(prev => prev.map(f => f.id === fileObj.id ? { ...f, status: 'skipped', display_message: msg } : f))
            processingRef.current.delete(fileObj.id)
            eventSource.close()
          } else {
            setLocalFiles(prev => prev.map(f => f.id === fileObj.id ? { ...f, display_message: msg } : f))
          }
        } catch (e) {
          console.error('Failed to parse SSE', e)
        }
      }

      eventSource.onerror = () => {
        setLocalFiles(prev => prev.map(f => f.id === fileObj.id ? { ...f, status: 'error', display_message: 'Соединение прервано' } : f))
        processingRef.current.delete(fileObj.id)
        eventSource.close()
      }

    } catch (e: any) {
      console.error('Error in processFile:', e)
      setLocalFiles(prev => prev.map(f => f.id === fileObj.id ? { ...f, status: 'error', display_message: 'Ошибка загрузки' } : f))
      processingRef.current.delete(fileObj.id)
    }
  }, [onLogout])

  const processingRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const timer = setTimeout(() => {
      const processingCount = localFiles.filter(f => f.status === 'processing').length;
      if (processingCount >= 3) return;

      const pendingFiles = localFiles.filter(f => f.status === 'pending');
      const filesToStart = pendingFiles.slice(0, 3 - processingCount);
      if (filesToStart.length === 0) return;

      const idsToStart = new Set(filesToStart.map(f => f.id));

      // Обновление состояния запускаемых задач
      setLocalFiles(prevFiles =>
        prevFiles.map(f =>
          idsToStart.has(f.id)
            ? { ...f, status: 'processing' as const, display_message: 'Запуск...' }
            : f
        )
      );

      // Асинхронный запуск задач вне функции обновления состояния
      filesToStart.forEach(f => {
        if (!processingRef.current.has(f.id)) {
          processingRef.current.add(f.id);
          processFile(f);
        }
      });
    }, 0);

    return () => clearTimeout(timer);
  }, [localFiles, processFile]);

  // Таймер для прошедших секунд обработки
  useEffect(() => {
    const timer = setInterval(() => {
      setLocalFiles(prev => prev.map(f => {
        if (f.status === 'processing') {
          return { ...f, elapsedSeconds: f.elapsedSeconds + 1 }
        }
        return f
      }))
    }, 1000)
    return () => clearInterval(timer)
  }, [])

  return (
    <div className="min-h-full flex flex-col items-center p-8 relative">

      {/* Шапка интерфейса: переключатель языка и выход из системы */}
      <div className="absolute top-6 right-6 z-20 flex items-center gap-3">
        <button
          onClick={() => setLang(lang === 'en' ? 'ru' : 'en')}
          className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/5 hover:bg-white/10 transition-colors border border-white/10 text-sm font-medium backdrop-blur-sm"
        >
          <Globe className="w-4 h-4" />
          {lang === 'en' ? 'RU' : 'EN'}
        </button>
        {onLogout && (
          <button
            onClick={() => onLogout()}
            className="flex items-center gap-2 px-4 py-2 rounded-full bg-red-500/10 hover:bg-red-500/20 text-red-400 transition-colors border border-red-500/20 text-sm font-medium backdrop-blur-sm"
            title={lang === 'en' ? 'Logout' : 'Выйти'}
          >
            <LogOut className="w-4 h-4" />
            <span className="hidden sm:inline">{lang === 'en' ? 'Logout' : 'Выйти'}</span>
          </button>
        )}
      </div>

      {/* Основной контейнер с перемонтированием при смене языка для запуска анимации */}
      <div key={lang} className="w-full flex flex-col items-center animate-fade-scale flex-1">
        {/* Верхний переключатель вкладок */}
        <div className="relative flex items-center bg-[#1a0f3c] rounded-full p-1 border border-purple-500/20 shadow-xl mb-12 w-full max-w-lg">
          <div
            className="absolute top-1 bottom-1 left-1 w-[calc(33.333%-2px)] bg-gradient-to-r from-purple-600 to-indigo-600 rounded-full transition-transform duration-500 ease-out z-0"
            style={{
              transform:
                activeTab === 'rag'
                  ? 'translateX(0)'
                  : activeTab === 'agents'
                  ? 'translateX(100%)'
                  : 'translateX(200%)',
            }}
          />
          <button
            onClick={() => setActiveTab('rag')}
            className={`relative z-10 flex-1 py-3 px-4 text-center text-sm font-medium transition-colors duration-500 ${
              activeTab === 'rag' ? 'text-white' : 'text-purple-300 hover:text-white'
            }`}
          >
            {lang === 'en' ? 'Load to RAG' : 'Загрузить в RAG'}
          </button>
          <button
            onClick={() => setActiveTab('agents')}
            className={`relative z-10 flex-1 py-3 px-4 text-center text-sm font-medium transition-colors duration-500 ${
              activeTab === 'agents' ? 'text-white' : 'text-purple-300 hover:text-white'
            }`}
          >
            {lang === 'en' ? 'Agent Requests' : 'Заявки агентов'}
          </button>
          <button
            onClick={() => setActiveTab('audit')}
            className={`relative z-10 flex-1 py-3 px-4 text-center text-sm font-medium transition-colors duration-500 ${
              activeTab === 'audit' ? 'text-white' : 'text-purple-300 hover:text-white'
            }`}
          >
            {lang === 'en' ? 'Audit Trail' : 'Журнал аудита'}
          </button>
        </div>

        {/* Основная область содержимого */}
        <div className="w-full max-w-5xl bg-[#1a0f3c]/60 backdrop-blur-xl border border-purple-500/20 rounded-3xl p-8 shadow-2xl relative overflow-hidden min-h-[600px]">


          {/* Вкладка 1: Загрузка в RAG */}
          <div
            className={`absolute inset-0 p-8 transition-all duration-700 ease-in-out ${activeTab === 'rag'
                ? 'opacity-100 translate-x-0 pointer-events-auto'
                : 'opacity-0 -translate-x-12 pointer-events-none'
              }`}
          >
            <div className="flex flex-col h-full">
              <div className="flex-none mb-10">
                <div className="mb-6 w-full">
                  <div className="relative flex w-full bg-[#1a0f3c]/80 backdrop-blur-md border border-purple-500/20 rounded-2xl p-1 z-10 shadow-xl">
                    <div
                      className="absolute top-1 bottom-1 bg-gradient-to-r from-purple-600 to-indigo-600 rounded-xl transition-all duration-500 ease-out shadow-lg"
                      style={{
                        left: `calc(${['main', 'bank', 'invest', 'digital'].indexOf(selectedAgent) * 25}% + 4px)`,
                        width: 'calc(25% - 8px)'
                      }}
                    />
                    {['main', 'bank', 'invest', 'digital'].map((agent) => (
                      <button
                        key={agent}
                        onClick={() => setSelectedAgent(agent)}
                        className={`relative z-10 flex-1 py-3 px-2 text-center text-sm font-medium transition-colors duration-500 ${selectedAgent === agent ? 'text-white' : 'text-purple-300 hover:text-white'
                          }`}
                      >
                        {formatAgentName(agent, lang)}
                      </button>
                    ))}
                  </div>
                </div>
                <input
                  type="file"
                  multiple
                  className="hidden"
                  ref={fileInputRef}
                  onChange={handleFileUpload}
                />
                <button
                  onClick={() => fileInputRef.current?.click()}
                  className="w-full group relative px-8 py-5 bg-gradient-to-r from-[#1a0f3c] to-[#241352] hover:from-purple-900/40 hover:to-indigo-900/40 rounded-2xl border border-purple-500/30 border-dashed hover:border-solid hover:border-purple-400 transition-all duration-300 active:scale-[0.99] overflow-hidden"
                >
                  <div className="absolute inset-0 bg-gradient-to-r from-purple-600/0 via-purple-600/5 to-indigo-600/0 opacity-0 group-hover:opacity-100 transition-opacity duration-500"></div>
                  <div className="relative flex items-center justify-center gap-4">
                    <div className="p-3 bg-purple-500/20 rounded-xl group-hover:bg-purple-500/30 transition-colors">
                      <UploadCloud className="w-6 h-6 text-purple-300" />
                    </div>
                    <div className="flex flex-col items-start">
                      <span className="text-lg font-bold text-white tracking-wide">
                        {lang === 'en' ? 'Upload Documents' : 'Загрузить документы'}
                      </span>
                      <span className="text-sm text-purple-300/70">
                        {lang === 'en' ? 'Select multiple files to process into RAG' : 'Выберите несколько файлов для обработки в RAG'}
                      </span>
                    </div>
                  </div>
                </button>
              </div>

              <div className="flex-1 overflow-auto pr-2">
                {localFiles.length === 0 ? (
                  <div key={lang} className="h-full flex flex-col items-center justify-center text-purple-300/50 animate-fade-scale">
                    <FileText className="w-16 h-16 mb-4 opacity-20" />
                    <p>{lang === 'en' ? 'No files are currently processing.' : 'Нет файлов в процессе обработки.'}</p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {localFiles.map((f, i) => (
                      <div key={f.id} className="flex items-center gap-4 bg-[#0f0728]/50 p-4 rounded-xl border border-purple-500/10">
                        <div className="flex-none w-8 text-center text-purple-400/50 font-mono text-sm">{i + 1}</div>
                        <div className="flex-none p-2 bg-purple-500/10 rounded-lg">
                          <FileText className="w-5 h-5 text-purple-400" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="truncate font-medium text-purple-100">
                            {f.name}
                          </div>
                          {f.display_message && (
                            <div className="text-xs text-purple-300/60 truncate mt-1">
                              {translateStatusMessage(f.display_message, lang)}
                            </div>
                          )}
                        </div>
                        <div className={`flex-none flex items-center gap-2 px-3 py-1 rounded-full text-sm border ${f.status === 'error' ? 'bg-red-500/10 border-red-500/20 text-red-400' :
                            f.status === 'skipped' ? 'bg-yellow-500/10 border-yellow-500/20 text-yellow-400' :
                              f.status === 'sent' ? 'bg-green-500/10 border-green-500/20 text-green-400' :
                                f.status === 'pending' ? 'bg-gray-500/10 border-gray-500/20 text-gray-400' :
                                  'bg-indigo-500/10 border-indigo-500/20 text-indigo-300'
                          }`}>
                          {f.status === 'processing' && <Clock className="w-4 h-4 animate-spin" />}
                          {f.status === 'sent' && <CheckCircle2 className="w-4 h-4" />}
                          {f.status === 'error' && <X className="w-4 h-4" />}
                          <span className="capitalize">
                            {f.status === 'processing' ? (lang === 'en' ? 'Processing' : 'В процессе') :
                              f.status === 'sent' ? (lang === 'en' ? 'Sent' : 'Отправлено') :
                                f.status === 'error' ? (lang === 'en' ? 'Error' : 'Ошибка') :
                                  f.status === 'skipped' ? (lang === 'en' ? 'Skipped' : 'Пропущено') :
                                    (lang === 'en' ? 'Pending' : 'В ожидании')}
                          </span>
                        </div>
                        <div className="flex-none w-20 text-right text-sm text-purple-400 font-mono">
                          {f.elapsedSeconds}s
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Вкладка 2: Заявки агентов */}
          <div
            className={`absolute inset-0 p-8 transition-all duration-700 ease-in-out ${activeTab === 'agents'
                ? 'opacity-100 translate-x-0 pointer-events-auto'
                : 'opacity-0 translate-x-12 pointer-events-none'
              }`}
          >
            <div className="flex flex-col h-full">
              <div className="flex items-center gap-4 mb-6">
                <button
                  disabled={selectedRequests.size === 0}
                  onClick={handleApproveSelected}
                  className="flex-1 py-3 px-4 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 disabled:from-purple-900/50 disabled:to-indigo-900/50 disabled:text-white/30 disabled:cursor-not-allowed text-white font-medium rounded-xl transition-all duration-300 shadow-lg shadow-purple-500/25 disabled:shadow-none active:scale-[0.98]"
                >
                  {lang === 'en' ? 'Approve Selected' : 'Одобрить выбранные'}
                  {selectedRequests.size > 0 && ` (${selectedRequests.size})`}
                </button>
                <button
                  disabled={selectedRequests.size === 0}
                  onClick={handleRejectSelected}
                  className="flex-1 py-3 px-4 bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 hover:to-rose-500 disabled:from-red-900/30 disabled:to-rose-900/30 disabled:text-white/30 disabled:cursor-not-allowed text-white font-medium rounded-xl transition-all duration-300 shadow-lg shadow-red-500/25 disabled:shadow-none active:scale-[0.98]"
                >
                  {lang === 'en' ? 'Reject Selected' : 'Отклонить выбранные'}
                  {selectedRequests.size > 0 && ` (${selectedRequests.size})`}
                </button>
                <button
                  onClick={() => { setShowAgentKeysModal(true); void fetchAgentList(); }}
                  className="px-4 py-3 bg-white/5 hover:bg-white/10 text-purple-300 border border-purple-500/30 rounded-xl transition-all flex items-center gap-2 text-sm font-medium whitespace-nowrap cursor-pointer"
                  title={lang === 'en' ? 'Agent Keys & Security' : 'Ключи и безопасность агентов'}
                >
                  <KeyRound className="w-4 h-4 text-purple-400" />
                  <span>{lang === 'en' ? 'Keys & Status' : 'Ключи и статус'}</span>
                </button>
              </div>

              <div className="flex-1 overflow-auto pr-2">
                {isLoadingRequests ? (
                  <div className="h-full flex items-center justify-center">
                    <div className="w-8 h-8 border-4 border-purple-500/30 border-t-purple-500 rounded-full animate-spin"></div>
                  </div>
                ) : requests.filter(req => req.status === 'pending').length === 0 ? (
                  <div className="h-full flex flex-col items-center justify-center text-purple-300/50">
                    <Users className="w-16 h-16 mb-4 opacity-20" />
                    <p>{lang === 'en' ? 'No pending agent requests.' : 'Нет активных заявок агентов.'}</p>
                  </div>
                ) : (
                  <div className="space-y-4">
                    <AnimatePresence>
                      {requests.filter(req => req.status === 'pending').map(req => (
                        <motion.div
                          layout
                          initial={{ opacity: 0, scale: 0.95 }}
                          animate={{ opacity: 1, scale: 1 }}
                          exit={{ opacity: 0, scale: 0.95, height: 0, marginBottom: 0, overflow: 'hidden' }}
                          transition={{ duration: 0.3 }}
                          key={req.id}
                          className="flex gap-4 p-5 bg-[#1a0f3c]/80 border border-purple-500/20 rounded-2xl hover:border-purple-500/40 transition-colors"
                        >
                          <div className="flex-none">
                            <div
                              onClick={() => toggleSelection(req.id)}
                              className={`w-6 h-6 rounded-md border-2 flex items-center justify-center cursor-pointer transition-colors ${selectedRequests.has(req.id)
                                  ? 'bg-purple-500 border-purple-500 text-white'
                                  : 'border-purple-500/30 hover:border-purple-500/50 text-transparent'
                                }`}
                            >
                              <Check className="w-4 h-4" strokeWidth={3} />
                            </div>
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center justify-between mb-2">
                              <div className="flex items-center gap-2">
                                <Server className="w-4 h-4 text-purple-400" />
                                <span className="font-bold text-white truncate">{formatAgentName(req.agent_name, lang)}</span>
                              </div>
                            </div>
                            <h3 className="text-lg font-medium text-purple-100 mb-1">
                              {lang === 'en' ? req.document_name_en : req.document_name_ru}
                            </h3>
                            <p className="text-sm text-purple-300/70 leading-relaxed line-clamp-2">
                              {lang === 'en' ? req.justification_en : req.justification_ru}
                            </p>
                          </div>
                          <div className="flex-none flex flex-col gap-2 pl-4 border-l border-purple-500/10 w-28">
                            <div className={`flex-1 flex items-center justify-center text-xs font-medium rounded-xl border ${req.status === 'pending' ? 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20' :
                                req.status === 'approved' ? 'bg-green-500/10 text-green-400 border-green-500/20' :
                                  'bg-red-500/10 text-red-400 border-red-500/20'
                              }`}>
                              {req.status === 'pending' ? (lang === 'en' ? 'Pending' : 'Ожидает') :
                                req.status === 'approved' ? (lang === 'en' ? 'Approved' : 'Одобрена') :
                                  (lang === 'en' ? 'Rejected' : 'Отклонена')}
                            </div>
                            <button
                              onClick={() => openApproveModal([req])}
                              disabled={selectedRequests.size > 0}
                              className="flex-1 flex items-center justify-center rounded-xl bg-purple-500/10 text-purple-400 hover:bg-purple-500/20 hover:text-purple-300 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-purple-500/10 transition-all duration-300 w-full" title={lang === 'en' ? 'Approve' : 'Одобрить'}
                            >
                              <Check className="w-5 h-5" />
                            </button>
                            <button
                              onClick={() => handleRejectSingle(req.id)}
                              disabled={selectedRequests.size > 0}
                              className="flex-1 flex items-center justify-center rounded-xl bg-red-500/10 text-red-400 hover:bg-red-500/20 hover:text-red-300 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-red-500/10 transition-all duration-300 w-full" title={lang === 'en' ? 'Reject' : 'Отклонить'}
                            >
                              <X className="w-5 h-5" />
                            </button>
                          </div>
                        </motion.div>
                      ))}
                    </AnimatePresence>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Вкладка 3: Неизменяемый журнал аудита и криптографическая целостность */}
          <div
            className={`absolute inset-0 p-8 transition-all duration-700 ease-in-out ${activeTab === 'audit'
                ? 'opacity-100 translate-x-0 pointer-events-auto'
                : 'opacity-0 translate-x-12 pointer-events-none'
              }`}
          >
            <div className="flex flex-col h-full">
              {/* Верхняя статусная панель аудита */}
              <div className="flex flex-wrap items-center justify-between gap-4 mb-6 pb-4 border-b border-purple-500/10">
                <div className="flex items-center gap-3">
                  {auditVerifyResult?.verified ? (
                    <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs font-semibold">
                      <ShieldCheck className="w-4 h-4 text-emerald-400" />
                      <span>{lang === 'en' ? 'Cryptographic Hash-Chain: Verified' : 'Криптографическая цепочка: Подтверждена'}</span>
                    </div>
                  ) : auditVerifyResult ? (
                    <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-semibold">
                      <ShieldAlert className="w-4 h-4 text-rose-400" />
                      <span>{lang === 'en' ? 'Integrity Tamper Alert!' : 'Нарушение целостности цепочки!'}</span>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-purple-500/10 border border-purple-500/30 text-purple-300 text-xs font-semibold">
                      <Shield className="w-4 h-4 text-purple-400" />
                      <span>{lang === 'en' ? 'Immutable Audit Trail' : 'Неизменяемый журнал аудита'}</span>
                    </div>
                  )}

                  {auditVerifyResult?.total_events !== undefined && (
                    <span className="text-xs text-purple-300/70">
                      {lang === 'en'
                        ? `Events: ${auditVerifyResult.total_events} (Verified: ${auditVerifyResult.verified_events ?? auditVerifyResult.total_events})`
                        : `Событий: ${auditVerifyResult.total_events} (Проверено: ${auditVerifyResult.verified_events ?? auditVerifyResult.total_events})`}
                    </span>
                  )}
                </div>

                <button
                  onClick={() => void fetchAuditData()}
                  disabled={isLoadingAudit}
                  className="px-4 py-2 bg-purple-600/20 hover:bg-purple-600/30 text-purple-300 border border-purple-500/30 rounded-xl transition-all flex items-center gap-2 text-xs font-medium cursor-pointer disabled:opacity-50"
                  title={lang === 'en' ? 'Verify and refresh chain' : 'Проверить и обновить цепочку'}
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${isLoadingAudit ? 'animate-spin' : ''}`} />
                  <span>{lang === 'en' ? 'Verify & Refresh' : 'Проверить и обновить'}</span>
                </button>
              </div>

              {/* Таблица событий аудита */}
              <div className="flex-1 overflow-auto pr-2">
                {isLoadingAudit && auditLogs.length === 0 ? (
                  <div className="h-full flex items-center justify-center">
                    <div className="w-8 h-8 border-4 border-purple-500/30 border-t-purple-500 rounded-full animate-spin"></div>
                  </div>
                ) : auditLogs.length === 0 ? (
                  <div className="h-full flex flex-col items-center justify-center text-purple-300/50">
                    <FileText className="w-16 h-16 mb-4 opacity-20" />
                    <p>{lang === 'en' ? 'No audit events found.' : 'Записи в журнале аудита отсутствуют.'}</p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {auditLogs.map((log) => (
                      <div
                        key={log.id}
                        className="p-4 bg-[#1a0f3c]/80 border border-purple-500/20 rounded-2xl hover:border-purple-500/40 transition-colors flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs"
                      >
                        <div className="flex items-start gap-3 min-w-0">
                          <div className="p-2 rounded-xl bg-purple-500/10 border border-purple-500/20 text-purple-400 mt-0.5">
                            <Clock className="w-4 h-4" />
                          </div>
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2 mb-1">
                              <span className="font-semibold text-white">{log.actor}</span>
                              <span className="px-2 py-0.5 rounded-md bg-purple-500/20 text-purple-300 font-mono text-[11px]">
                                {log.action}
                              </span>
                              <span
                                className={`px-2 py-0.5 rounded-md text-[11px] font-medium ${
                                  log.status === 'SUCCESS'
                                    ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                                    : log.status === 'WARNING'
                                    ? 'bg-yellow-500/10 text-yellow-400 border border-yellow-500/20'
                                    : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'
                                }`}
                              >
                                {log.status}
                              </span>
                            </div>
                            <div className="text-purple-300/70 font-mono text-[11px] truncate">
                              {log.resource ? `resource: ${log.resource}` : ''}
                              {log.actor_ip ? ` | ip: ${log.actor_ip}` : ''}
                            </div>
                            <div className="text-purple-400/50 font-mono text-[10px] mt-1 truncate">
                              prev: {log.prev_hash ? log.prev_hash.slice(0, 16) + '...' : 'GENESIS'} → hash: {log.current_hash ? log.current_hash.slice(0, 16) + '...' : ''}
                            </div>
                          </div>
                        </div>

                        <div className="text-right text-purple-300/60 font-mono text-[11px] whitespace-nowrap self-end sm:self-center">
                          {log.timestamp ? new Date(log.timestamp).toLocaleString() : ''}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>

        </div>

        {/* Модальное окно подтверждения и прикрепления файлов */}
        {isApproveModalOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-[#0f0728]/80 backdrop-blur-sm animate-fade-scale">
            <div className="bg-[#1a0f3c] border border-purple-500/30 rounded-3xl w-full max-w-2xl shadow-2xl flex flex-col max-h-[90vh]">
              <div className="flex justify-between items-center p-6 border-b border-purple-500/10">
                <h3 className="text-xl font-bold text-white">
                  {lang === 'en' ? 'Attach Documents' : 'Прикрепить документы'}
                </h3>
                <button
                  onClick={() => setIsApproveModalOpen(false)}
                  className="p-2 text-purple-300 hover:text-white bg-white/5 hover:bg-white/10 rounded-full transition-colors"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="p-6 overflow-y-auto flex-1 space-y-4">
                {approvingRequests.map(req => (
                  <div key={req.id} className="p-4 bg-[#0f0728]/50 rounded-xl border border-purple-500/10 flex flex-col gap-3">
                    <div className="mb-1">
                      <span className="font-bold text-purple-200">
                        {lang === 'en' ? req.document_name_en : req.document_name_ru}
                      </span>
                    </div>
                    <p className="text-sm text-purple-300/70">
                      {lang === 'en' ? req.justification_en : req.justification_ru}
                    </p>

                    {/* Поле выбора файла для конкретной заявки */}
                    <div className="mt-2">
                      <label className={`flex items-center justify-center gap-2 p-3 border border-dashed rounded-xl cursor-pointer transition-colors ${requestFiles[req.id] ? 'border-green-500/50 bg-green-500/10 text-green-300' : 'border-purple-500/30 hover:border-purple-500/60 bg-purple-500/5 text-purple-300'
                        }`}>
                        <input
                          type="file"
                          className="hidden"
                          onChange={(e) => {
                            if (e.target.files && e.target.files[0]) {
                              handleFileForRequest(req.id, e.target.files[0])
                            }
                          }}
                        />
                        {requestFiles[req.id] ? (
                          <>
                            <CheckCircle2 className="w-5 h-5" />
                            <span className="truncate max-w-[200px]">{requestFiles[req.id].name}</span>
                          </>
                        ) : (
                          <>
                            <UploadCloud className="w-5 h-5" />
                            <span>{lang === 'en' ? 'Select File' : 'Выбрать файл'}</span>
                          </>
                        )}
                      </label>
                    </div>
                  </div>
                ))}
              </div>

              <div className="p-6 border-t border-purple-500/10 bg-[#0f0728]/30 rounded-b-3xl">
                <button
                  onClick={confirmApprove}
                  disabled={Object.keys(requestFiles).length !== approvingRequests.length}
                  className="w-full py-4 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 disabled:from-purple-900/50 disabled:to-indigo-900/50 disabled:text-white/30 disabled:cursor-not-allowed text-white font-bold rounded-xl transition-all shadow-lg shadow-purple-500/25 disabled:shadow-none"
                >
                  {lang === 'en' ? 'Confirm and Upload to RAG' : 'Подтвердить и загрузить в RAG'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Модальное окно управления ключами и статусом агентов */}
        {showAgentKeysModal && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
            <div className="bg-[#140b2b] border border-purple-500/30 rounded-3xl max-w-2xl w-full p-6 shadow-2xl relative text-white max-h-[85vh] flex flex-col">
              <button
                onClick={() => setShowAgentKeysModal(false)}
                className="absolute top-5 right-5 text-purple-300/60 hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>

              <div className="flex items-center gap-3 mb-5">
                <div className="w-10 h-10 rounded-xl bg-purple-500/20 border border-purple-500/30 flex items-center justify-center text-purple-300">
                  <KeyRound className="w-6 h-6 text-purple-400" />
                </div>
                <div>
                  <h3 className="text-base font-bold text-white">
                    {lang === 'en' ? 'Agent Keys & Security Lifecycle' : 'Ключи и безопасность агентов'}
                  </h3>
                  <p className="text-xs text-purple-300/70">
                    {lang === 'en' ? 'Manage cryptographic RSA keys, rotation, and emergency suspension' : 'Управление RSA-ключами, плановая ротация и экстренный отзыв'}
                  </p>
                </div>
              </div>

              <div className="flex-1 overflow-auto pr-2 space-y-4">
                {loadingAgents ? (
                  <div className="py-12 flex items-center justify-center gap-2 text-purple-300 text-sm">
                    <RefreshCw className="w-5 h-5 animate-spin text-purple-400" />
                    <span>{lang === 'en' ? 'Loading agents...' : 'Загрузка списка агентов...'}</span>
                  </div>
                ) : agentList.length === 0 ? (
                  <div className="py-12 text-center text-purple-300/60 text-sm">
                    {lang === 'en' ? 'No registered agents found.' : 'Нет зарегистрированных агентов.'}
                  </div>
                ) : (
                  agentList.map(agent => (
                    <div key={agent.id} className="p-4 bg-white/5 border border-purple-500/20 rounded-2xl space-y-3">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2.5">
                          <span className="font-semibold text-white tracking-wide capitalize">{agent.name}</span>
                          <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium uppercase tracking-wider ${
                            agent.status === 'active' ? 'bg-green-500/20 text-green-300 border border-green-500/30' :
                            agent.status === 'suspended' ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30' :
                            'bg-red-500/20 text-red-300 border border-red-500/30'
                          }`}>
                            {agent.status}
                          </span>
                        </div>
                        <div className="flex items-center gap-2">
                          {agent.status === 'active' ? (
                            <>
                              <button
                                onClick={() => void handleRevokeKey(agent.name, agent.active_key_id)}
                                className="px-2.5 py-1 text-xs bg-red-500/20 hover:bg-red-500/30 text-red-300 border border-red-500/30 rounded-lg transition-colors cursor-pointer"
                                title={lang === 'en' ? 'Revoke Key' : 'Отозвать ключ'}
                              >
                                {lang === 'en' ? 'Revoke Key' : 'Отозвать ключ'}
                              </button>
                              <button
                                onClick={() => void handleSuspendAgent(agent.name)}
                                className="px-2.5 py-1 text-xs bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/30 rounded-lg transition-colors cursor-pointer"
                                title={lang === 'en' ? 'Emergency Kill Switch' : 'Приостановить агента'}
                              >
                                {lang === 'en' ? 'Suspend' : 'Приостановить'}
                              </button>
                            </>
                          ) : (
                            <button
                              onClick={() => void handleReactivateAgent(agent.name)}
                              className="px-2.5 py-1 text-xs bg-green-500/20 hover:bg-green-500/30 text-green-300 border border-green-500/30 rounded-lg transition-colors cursor-pointer"
                            >
                              {lang === 'en' ? 'Reactivate' : 'Возобновить'}
                            </button>
                          )}
                        </div>
                      </div>

                      {/* Ключи агента */}
                      <div className="space-y-1.5 pt-1">
                        <div className="text-[11px] text-purple-300/60 uppercase tracking-wider font-semibold">
                          {lang === 'en' ? 'Keys History & TTL:' : 'История ключей и срок действия:'}
                        </div>
                        {agent.keys && agent.keys.length > 0 ? (
                          agent.keys.map((k: any) => (
                            <div key={k.kid} className="flex items-center justify-between text-xs p-2 bg-black/30 rounded-lg border border-purple-500/10 font-mono">
                              <div className="truncate mr-2">
                                <span className={k.status === 'active' ? 'text-cyan-300 font-semibold' : 'text-purple-300/50'}>
                                  {k.kid}
                                </span>
                                <span className="text-purple-400/60 text-[10px] ml-2 font-sans">
                                  ({k.fingerprint})
                                </span>
                              </div>
                              <div className="flex items-center gap-2 whitespace-nowrap text-[11px]">
                                <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                                  k.status === 'active' ? 'bg-green-500/20 text-green-300' :
                                  k.status === 'revoked' ? 'bg-red-500/20 text-red-300 line-through' :
                                  'bg-purple-500/10 text-purple-300/50'
                                }`}>
                                  {k.status}
                                </span>
                                <span className="text-purple-300/50 text-[10px] font-sans">
                                  {k.expires_at ? new Date(k.expires_at).toLocaleDateString() : ''}
                                </span>
                              </div>
                            </div>
                          ))
                        ) : (
                          <div className="text-xs text-purple-300/40 italic">
                            {lang === 'en' ? 'No keys registered.' : 'Ключи отсутствуют.'}
                          </div>
                        )}
                      </div>
                    </div>
                  ))
                )}
              </div>

              <div className="pt-4 border-t border-purple-500/20 flex justify-between items-center mt-3">
                <button
                  onClick={() => void fetchAgentList()}
                  className="text-xs text-purple-400 hover:text-purple-300 flex items-center gap-1 cursor-pointer"
                >
                  <RefreshCw className="w-3 h-3" />
                  <span>{lang === 'en' ? 'Refresh' : 'Обновить'}</span>
                </button>
                <button
                  onClick={() => setShowAgentKeysModal(false)}
                  className="px-5 py-2 rounded-xl bg-white/10 hover:bg-white/20 text-white text-xs font-medium transition-colors cursor-pointer"
                >
                  {lang === 'en' ? 'Close' : 'Закрыть'}
                </button>
              </div>
            </div>
          </div>
        )}

      </div>
    </div>
  )
}
