import { useState, useRef } from 'react'
import { Users, FileText, UploadCloud, Clock, CheckCircle2, Globe } from 'lucide-react'

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
  document_name: string
  justification: string
  status: string
}

interface DashboardProps {
  lang: 'en' | 'ru'
  setLang: (lang: 'en' | 'ru') => void
}

export default function Dashboard({ lang, setLang }: DashboardProps) {
  const [activeTab, setActiveTab] = useState<Tab>('rag')
  const [localFiles, setLocalFiles] = useState<LocalFile[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Mock data for requests
  const [requests] = useState<AgentRequest[]>([])

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
                disabled={requests.length === 0}
                className="flex-1 py-3 px-4 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 disabled:from-purple-900/50 disabled:to-indigo-900/50 disabled:text-white/30 disabled:cursor-not-allowed text-white font-medium rounded-xl transition-all shadow-lg shadow-purple-500/25 disabled:shadow-none active:scale-[0.98]"
              >
                {lang === 'en' ? 'Approve Selected' : 'Одобрить выбранные'}
              </button>
              <button 
                disabled={requests.length === 0}
                className="flex-1 py-3 px-4 bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 hover:to-rose-500 disabled:from-red-900/30 disabled:to-rose-900/30 disabled:text-white/30 disabled:cursor-not-allowed text-white font-medium rounded-xl transition-all shadow-lg shadow-red-500/25 disabled:shadow-none active:scale-[0.98]"
              >
                {lang === 'en' ? 'Reject Selected' : 'Отклонить выбранные'}
              </button>
            </div>
            
            <div className="flex-1 overflow-auto">
              {requests.length === 0 ? (
                <div className="h-full flex flex-col items-center justify-center text-purple-300/50">
                  <Users className="w-16 h-16 mb-4 opacity-20" />
                  <p>{lang === 'en' ? 'No pending agent requests.' : 'Нет активных заявок агентов.'}</p>
                  <p className="text-sm mt-2 opacity-50">
                    {lang === 'en' ? '(Database table is empty)' : '(Таблица базы данных пуста)'}
                  </p>
                </div>
              ) : (
                <div className="space-y-3">
                  {/* We will populate this when we fetch from the DB */}
                </div>
              )}
            </div>
          </div>
        </div>

      </div>
      </div>
    </div>
  )
}
