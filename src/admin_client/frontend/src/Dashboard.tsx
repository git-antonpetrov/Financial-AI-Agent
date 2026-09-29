import { useState, useRef, useEffect } from 'react'
import { Users, FileText, UploadCloud, Clock, CheckCircle2, Globe, Server, Check, X } from 'lucide-react'

type Tab = 'rag' | 'agents'

interface LocalFile {
  id: string
  file: File
  name: string
  status: 'processing' | 'sending' | 'sent' | 'error'
  elapsedSeconds: number
}

// Mock agent request type
interface AgentRequest {
  id: number
  agent_name: string
  document_name_ru: string
  document_name_en: string
  justification_ru: string
  justification_en: string
  status: string
}

interface DashboardProps {
  lang: 'en' | 'ru'
  setLang: (lang: 'en' | 'ru') => void
}

const formatAgentName = (name: string, lang: 'en' | 'ru') => {
  if (!name) return '';
  const dictionary: Record<string, { en: string, ru: string }> = {
    digital: { en: 'Digital agent', ru: 'Цифровой агент' },
    invest: { en: 'Invest agent', ru: 'Инвестиционный агент' },
    bank: { en: 'Bank agent', ru: 'Банковский агент' },
    main: { en: 'Main agent', ru: 'Главный агент' },
  };
  
  const lowerName = name.toLowerCase();
  if (dictionary[lowerName]) {
    return dictionary[lowerName][lang];
  }
  
  const capitalized = name.charAt(0).toUpperCase() + name.slice(1);
  return `${capitalized} ${lang === 'en' ? 'agent' : 'агент'}`;
};

export default function Dashboard({ lang, setLang }: DashboardProps) {
  const [activeTab, setActiveTab] = useState<Tab>('rag')
  const [localFiles, setLocalFiles] = useState<LocalFile[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [requests, setRequests] = useState<AgentRequest[]>([])
  const [selectedRequests, setSelectedRequests] = useState<Set<number>>(new Set())
  const [isLoadingRequests, setIsLoadingRequests] = useState(false)

  // Approve Modal State
  const [isApproveModalOpen, setIsApproveModalOpen] = useState(false)
  const [approvingRequests, setApprovingRequests] = useState<AgentRequest[]>([])
  const [requestFiles, setRequestFiles] = useState<Record<number, File>>({})

  const fetchRequests = async () => {
    setIsLoadingRequests(true)
    try {
      const token = localStorage.getItem('admin_token')
      const serverUrl = localStorage.getItem('admin_server') || ''
      const res = await fetch(`${serverUrl}/api/agent-requests`, {
        headers: {
          'Authorization': `Bearer ${token}`
        }
      })
      if (res.ok) {
        const data = await res.json()
        setRequests(data)
      }
    } catch (e) {
      console.error(e)
    } finally {
      setIsLoadingRequests(false)
    }
  }

  useEffect(() => {
    if (activeTab === 'agents') {
      fetchRequests()
    }
  }, [activeTab])

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
    try {
      const token = localStorage.getItem('admin_token')
      const serverUrl = localStorage.getItem('admin_server') || ''
      await fetch(`${serverUrl}/api/agent-requests/reject`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ request_ids: ids })
      })
      setSelectedRequests(new Set())
      fetchRequests()
    } catch (e) {
      console.error(e)
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
    const ids = approvingRequests.map(r => r.id)
    
    // First update statuses
    try {
      const token = localStorage.getItem('admin_token')
      const serverUrl = localStorage.getItem('admin_server') || ''
      await fetch(`${serverUrl}/api/agent-requests/approve`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ request_ids: ids })
      })
      
      // Then add files to RAG tab queue
      const newLocalFiles = approvingRequests.map(req => {
        const file = requestFiles[req.id]
        return {
          id: Math.random().toString(36).substring(7),
          file: file,
          name: file.name,
          status: 'processing' as const,
          elapsedSeconds: 0,
        }
      })
      
      setLocalFiles(prev => [...prev, ...newLocalFiles])
      setIsApproveModalOpen(false)
      setSelectedRequests(new Set())
      fetchRequests()
      setActiveTab('rag')
    } catch (e) {
      console.error(e)
    }
  }

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const newFiles = Array.from(e.target.files).map((f) => ({
        id: Math.random().toString(36).substring(7),
        file: f,
        name: f.name,
        status: 'processing' as const,
        elapsedSeconds: 0,
      }))
      setLocalFiles((prev) => [...prev, ...newFiles])
    }
    if (fileInputRef.current) {
      fileInputRef.current.value = ''
    }
  }

  return (
    <div className="min-h-screen bg-[#0f0728] text-purple-50 flex flex-col items-center p-8 relative">
      
      {/* Header / Language Toggle */}
      <div className="absolute top-6 right-6 z-20">
        <button
          onClick={() => setLang(lang === 'en' ? 'ru' : 'en')}
          className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/5 hover:bg-white/10 transition-colors border border-white/10 text-sm font-medium backdrop-blur-sm"
        >
          <Globe className="w-4 h-4" />
          {lang === 'en' ? 'RU' : 'EN'}
        </button>
      </div>

      {/* Main Container that remounts on language change to trigger animation */}
      <div key={lang} className="w-full flex flex-col items-center animate-fade-scale flex-1">
        {/* Pill-shaped Top Slider */}
        <div className="relative flex items-center bg-[#1a0f3c] rounded-full p-1 border border-purple-500/20 shadow-xl mb-12 w-full max-w-md">
          <div
            className="absolute top-1 bottom-1 left-1 w-[calc(50%-4px)] bg-gradient-to-r from-purple-600 to-indigo-600 rounded-full transition-transform duration-500 ease-out z-0"
            style={{ transform: activeTab === 'rag' ? 'translateX(0)' : 'translateX(100%)' }}
          />
          <button
          onClick={() => setActiveTab('rag')}
          className={`relative z-10 flex-1 py-3 px-6 text-center text-sm font-medium transition-colors duration-500 ${
            activeTab === 'rag' ? 'text-white' : 'text-purple-300 hover:text-white'
          }`}
        >
          {lang === 'en' ? 'Load to RAG' : 'Загрузить в RAG'}
        </button>
        <button
          onClick={() => setActiveTab('agents')}
          className={`relative z-10 flex-1 py-3 px-6 text-center text-sm font-medium transition-colors duration-500 ${
            activeTab === 'agents' ? 'text-white' : 'text-purple-300 hover:text-white'
          }`}
        >
          {lang === 'en' ? 'Agent Requests' : 'Заявки агентов'}
        </button>
      </div>

      {/* Main Content Area */}
      <div className="w-full max-w-5xl bg-[#1a0f3c]/60 backdrop-blur-xl border border-purple-500/20 rounded-3xl p-8 shadow-2xl relative overflow-hidden min-h-[600px]">
        
        {/* Tab 1: Load to RAG */}
        <div 
          className={`absolute inset-0 p-8 transition-all duration-700 ease-in-out ${
            activeTab === 'rag' 
              ? 'opacity-100 translate-x-0 pointer-events-auto' 
              : 'opacity-0 -translate-x-12 pointer-events-none'
          }`}
        >
          <div className="flex flex-col h-full">
            <div className="flex-none mb-10">
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
                <div className="h-full flex flex-col items-center justify-center text-purple-300/50">
                  <FileText className="w-16 h-16 mb-4 opacity-20" />
                  <p>No files are currently processing.</p>
                </div>
              ) : (
                <div className="space-y-3">
                  {localFiles.map((f, i) => (
                    <div key={f.id} className="flex items-center gap-4 bg-[#0f0728]/50 p-4 rounded-xl border border-purple-500/10">
                      <div className="flex-none w-8 text-center text-purple-400/50 font-mono text-sm">{i + 1}</div>
                      <div className="flex-none p-2 bg-purple-500/10 rounded-lg">
                        <FileText className="w-5 h-5 text-purple-400" />
                      </div>
                      <div className="flex-1 truncate font-medium text-purple-100">
                        {f.name}
                      </div>
                      <div className="flex-none flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-indigo-300 text-sm">
                        {f.status === 'processing' && <Clock className="w-4 h-4" />}
                        {f.status === 'sent' && <CheckCircle2 className="w-4 h-4 text-green-400" />}
                        <span className="capitalize">{f.status}</span>
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

        {/* Tab 2: Agent Requests */}
        <div 
          className={`absolute inset-0 p-8 transition-all duration-700 ease-in-out ${
            activeTab === 'agents' 
              ? 'opacity-100 translate-x-0 pointer-events-auto' 
              : 'opacity-0 translate-x-12 pointer-events-none'
          }`}
        >
          <div className="flex flex-col h-full">
            <div className="flex items-center gap-4 mb-6">
              <button 
                disabled={selectedRequests.size === 0}
                onClick={handleApproveSelected}
                className="flex-1 py-3 px-4 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 disabled:from-purple-900/50 disabled:to-indigo-900/50 disabled:text-white/30 disabled:cursor-not-allowed text-white font-medium rounded-xl transition-all shadow-lg shadow-purple-500/25 disabled:shadow-none active:scale-[0.98]"
              >
                {lang === 'en' ? 'Approve Selected' : 'Одобрить выбранные'}
                {selectedRequests.size > 0 && ` (${selectedRequests.size})`}
              </button>
              <button 
                disabled={selectedRequests.size === 0}
                onClick={handleRejectSelected}
                className="flex-1 py-3 px-4 bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 hover:to-rose-500 disabled:from-red-900/30 disabled:to-rose-900/30 disabled:text-white/30 disabled:cursor-not-allowed text-white font-medium rounded-xl transition-all shadow-lg shadow-red-500/25 disabled:shadow-none active:scale-[0.98]"
              >
                {lang === 'en' ? 'Reject Selected' : 'Отклонить выбранные'}
                {selectedRequests.size > 0 && ` (${selectedRequests.size})`}
              </button>
            </div>
            
            <div className="flex-1 overflow-auto pr-2">
              {isLoadingRequests ? (
                <div className="h-full flex items-center justify-center">
                  <div className="w-8 h-8 border-4 border-purple-500/30 border-t-purple-500 rounded-full animate-spin"></div>
                </div>
              ) : requests.length === 0 ? (
                <div className="h-full flex flex-col items-center justify-center text-purple-300/50">
                  <Users className="w-16 h-16 mb-4 opacity-20" />
                  <p>{lang === 'en' ? 'No pending agent requests.' : 'Нет активных заявок агентов.'}</p>
                  <p className="text-sm mt-2 opacity-50">
                    {lang === 'en' ? '(Database table is empty)' : '(Таблица базы данных пуста)'}
                  </p>
                </div>
              ) : (
                <div className="space-y-4">
                  {requests.map(req => (
                    <div key={req.id} className="flex gap-4 p-5 bg-[#1a0f3c]/80 border border-purple-500/20 rounded-2xl hover:border-purple-500/40 transition-colors">
                      <div className="flex-none">
                        <div 
                          onClick={() => toggleSelection(req.id)}
                          className={`w-6 h-6 rounded-md border-2 flex items-center justify-center cursor-pointer transition-colors ${
                            selectedRequests.has(req.id) 
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
                        <div className={`flex-1 flex items-center justify-center text-xs font-medium rounded-xl border ${
                          req.status === 'pending' ? 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20' :
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
                          className="flex-1 flex items-center justify-center rounded-xl bg-purple-500/10 text-purple-400 hover:bg-purple-500/20 hover:text-purple-300 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-purple-500/10 transition-colors w-full" title={lang === 'en' ? 'Approve' : 'Одобрить'}
                        >
                          <Check className="w-5 h-5" />
                        </button>
                        <button 
                          onClick={() => handleRejectSingle(req.id)}
                          disabled={selectedRequests.size > 0}
                          className="flex-1 flex items-center justify-center rounded-xl bg-red-500/10 text-red-400 hover:bg-red-500/20 hover:text-red-300 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-red-500/10 transition-colors w-full" title={lang === 'en' ? 'Reject' : 'Отклонить'}
                        >
                          <X className="w-5 h-5" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>

      </div>

      {/* Approve Modal Overlay */}
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
                  <div className="flex justify-between">
                    <span className="font-bold text-purple-200">{req.agent_name}</span>
                    <span className="text-sm text-purple-400">{req.document_name}</span>
                  </div>
                  <p className="text-sm text-purple-300/70">{req.justification}</p>
                  
                  {/* File Input for this request */}
                  <div className="mt-2">
                    <label className={`flex items-center justify-center gap-2 p-3 border border-dashed rounded-xl cursor-pointer transition-colors ${
                      requestFiles[req.id] ? 'border-green-500/50 bg-green-500/10 text-green-300' : 'border-purple-500/30 hover:border-purple-500/60 bg-purple-500/5 text-purple-300'
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

      </div>
    </div>
  )
}
