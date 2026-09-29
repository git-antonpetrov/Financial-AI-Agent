import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

import Titlebar from './Titlebar.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <div className="flex flex-col h-screen w-screen overflow-hidden bg-[#0f0728] relative text-white">
      {/* Background Decorative Elements */}
      <div className="absolute top-[-20%] left-[-10%] w-[50%] h-[50%] rounded-full bg-purple-600/20 blur-[120px] pointer-events-none z-0" />
      <div className="absolute bottom-[-20%] right-[-10%] w-[50%] h-[50%] rounded-full bg-fuchsia-600/10 blur-[120px] pointer-events-none z-0" />

      <div className="relative z-50">
        <Titlebar />
      </div>
      <div className="flex-1 overflow-y-auto relative z-10">
        <App />
      </div>
    </div>
  </StrictMode>,
)
