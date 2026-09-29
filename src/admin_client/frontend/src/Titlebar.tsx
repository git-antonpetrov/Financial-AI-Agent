import { getCurrentWindow } from '@tauri-apps/api/window';
import { X, Minus, Square, Copy } from 'lucide-react';
import { useEffect, useState } from 'react';

const appWindow = getCurrentWindow();

export default function Titlebar() {
  const [isMaximized, setIsMaximized] = useState(false);

  useEffect(() => {
    appWindow.isMaximized().then(setIsMaximized);
    const unlisten = appWindow.onResized(async () => {
      setIsMaximized(await appWindow.isMaximized());
    });
    return () => {
      unlisten.then(f => f());
    };
  }, []);

  return (
    <div
      className="h-8 w-full flex justify-between items-center shrink-0 bg-transparent z-50"
    >
      <div data-tauri-drag-region className="flex-1 h-full flex items-center px-3 text-xs text-white/50 select-none">
        Local Admin Client
      </div>
      
      <div className="flex h-full">
        <div
          className="inline-flex justify-center items-center w-12 h-full hover:bg-white/10 cursor-pointer text-white/70 transition-colors"
          onClick={() => appWindow.minimize()}
        >
          <Minus className="w-4 h-4" />
        </div>
        <div
          className="inline-flex justify-center items-center w-12 h-full hover:bg-white/10 cursor-pointer text-white/70 transition-colors"
          onClick={() => appWindow.toggleMaximize()}
        >
          {isMaximized ? <Copy className="w-3.5 h-3.5" /> : <Square className="w-3.5 h-3.5" />}
        </div>
        <div
          className="inline-flex justify-center items-center w-12 h-full hover:bg-red-500 hover:text-white cursor-pointer text-white/70 transition-colors"
          onClick={() => appWindow.close()}
        >
          <X className="w-4 h-4" />
        </div>
      </div>
    </div>
  );
}
