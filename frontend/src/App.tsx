import { HashRouter, Navigate, Route, Routes } from "react-router-dom";

/*
 * ⚠️ HashRouter rather than BrowserRouter, **deliberately**:
 * FloorZero's model is that the user hosts it themselves — plenty will simply put `dist/`
 * behind `python -m http.server` or bare nginx. BrowserRouter needs the server to rewrite
 * every path to index.html, and without that, opening or refreshing /insiders is a 404.
 * We do not control the user's server, so the hash router wins: zero configuration, runs anywhere.
 * The only cost is a `#` in the URL, which is entirely acceptable for a local tool.
 */
import Shell from "./components/Shell";
import Gex from "./pages/Gex";
import Flow from "./pages/Flow";
import Scanner from "./pages/Scanner";
import Darkpool from "./pages/Darkpool";
import StockPage from "./pages/Stock";
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
          <Route path="/stock" element={<StockPage />} />
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
