import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("cleaDesktop", {
  isElectron: true,
  getConnection: () => ipcRenderer.invoke("clea:get-connection"),
  onQuickAskFocus: (callback: () => void) => {
    const handler = () => callback();
    ipcRenderer.on("clea:quick-ask-focus", handler);
    return () => ipcRenderer.removeListener("clea:quick-ask-focus", handler);
  },
  closeQuickAsk: () => ipcRenderer.send("clea:close-quick-ask"),
});
