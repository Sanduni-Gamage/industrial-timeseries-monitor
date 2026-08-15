/**
 * Application shell: navigation, theme control, and a persistent service-status line.
 *
 * The status line is deliberately always visible. If the API goes down, an operator
 * should see that immediately rather than inferring it from panels that quietly stop
 * updating.
 */

import { NavLink, Route, Routes } from "react-router-dom";

import { api } from "./api/client";
import { StatusBadge } from "./components/Status";
import { useApi, useTheme } from "./hooks";
import { Anomalies } from "./screens/Anomalies";
import { DataQuality } from "./screens/DataQuality";
import { EquipmentHealthScreen } from "./screens/EquipmentHealth";
import { Overview } from "./screens/Overview";
import { SensorExplorer } from "./screens/SensorExplorer";
import "./app.css";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/sensors", label: "Sensor explorer", end: false },
  { to: "/health", label: "Equipment health", end: false },
  { to: "/anomalies", label: "Anomalies", end: false },
  { to: "/quality", label: "Data quality", end: false },
];

export default function App() {
  const [theme, setTheme] = useTheme();
  // Polled so an outage surfaces on its own rather than on the next navigation.
  const health = useApi((signal) => api.health(signal), []);

  const serviceStatus =
    health.error !== null ? "CRITICAL" : health.data?.status === "healthy" ? "NORMAL" : "WARNING";
  const serviceLabel =
    health.error !== null
      ? "Service unreachable"
      : health.data?.status === "healthy"
        ? "Service healthy"
        : health.initialLoading
          ? "Checking…"
          : "Service degraded";

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <h1>APU Monitoring</h1>
            <p className="brand-sub">Metro train air production unit</p>
          </div>
        </div>

        <nav aria-label="Primary">
          <ul className="nav">
            {NAV.map((item) => (
              <li key={item.to}>
                <NavLink to={item.to} end={item.end}>
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="topbar-right">
          <StatusBadge status={serviceStatus} label={serviceLabel} size="sm" />
          <label className="field theme-picker">
            <span className="visually-hidden">Colour theme</span>
            <select value={theme} onChange={(event) => setTheme(event.target.value as never)}>
              <option value="system">Theme: system</option>
              <option value="light">Theme: light</option>
              <option value="dark">Theme: dark</option>
            </select>
          </label>
        </div>
      </header>

      <main id="main" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/sensors" element={<SensorExplorer />} />
          <Route path="/health" element={<EquipmentHealthScreen />} />
          <Route path="/anomalies" element={<Anomalies />} />
          <Route path="/quality" element={<DataQuality />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>

      <footer className="footer">
        <p>
          Historical data from the UCI MetroPT-3 dataset (DOI 10.24432/C5VW3R).
        </p>
      </footer>
    </>
  );
}

function NotFound() {
  return (
    <div className="screen">
      <h2>Page not found</h2>
      <p className="secondary">
        That address does not match any screen. Use the navigation above.
      </p>
    </div>
  );
}
