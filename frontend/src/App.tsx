import { HashRouter, Navigate, Route, Routes } from "react-router-dom";

/*
 * ⚠️ 用 HashRouter 而不是 BrowserRouter，是**刻意的**：
 * Vibe-Flow 的模式是用户自己部署 —— 很多人就是拿 `python -m http.server`
 * 或裸 nginx 把 `dist/` 丢上去。BrowserRouter 需要服务端把所有路径
 * 重写到 index.html，没配的话直接打开或刷新 /insiders 就是 404。
 * 我们控制不了用户的服务器，所以选零配置、到处都能跑的哈希路由。
 * 代价只是 URL 里多个 `#`，对本地自用工具完全可接受。
 */
import Shell from "./components/Shell";
import Gex from "./pages/Gex";
import Flow from "./pages/Flow";
import Scanner from "./pages/Scanner";
import Darkpool from "./pages/Darkpool";
import Congress from "./pages/Congress";
import Insiders from "./pages/Insiders";
import Institutions from "./pages/Institutions";
import Shorts from "./pages/Shorts";
import Market from "./pages/Market";

export default function App() {
  return (
    <HashRouter>
      <Shell>
        <Routes>
          <Route path="/flow" element={<Flow />} />
          <Route path="/gex" element={<Gex />} />
          <Route path="/scanner" element={<Scanner />} />
          <Route path="/darkpool" element={<Darkpool />} />
          <Route path="/congress" element={<Congress />} />
          <Route path="/insiders" element={<Insiders />} />
          <Route path="/institutions" element={<Institutions />} />
          <Route path="/shorts" element={<Shorts />} />
          <Route path="/market" element={<Market />} />
          <Route path="*" element={<Navigate to="/gex" replace />} />
        </Routes>
      </Shell>
    </HashRouter>
  );
}
