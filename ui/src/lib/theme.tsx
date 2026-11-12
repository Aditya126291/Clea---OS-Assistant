import { useEffect, useState, type ReactNode } from "react";
import { ThemeContext, type ResolvedTheme, type ThemePreference } from "./theme-context";

const STORAGE_KEY = "clea.theme";

function getSystemTheme(): ResolvedTheme {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "dark";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function readStoredPreference(): ThemePreference {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === "dark" || raw === "light" || raw === "system") {
      return raw;
    }
  } catch {
    // ignore storage access issues
  }
  return "system";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(readStoredPreference);
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(getSystemTheme);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const listener = (e: MediaQueryListEvent) => setSystemTheme(e.matches ? "dark" : "light");
    mq.addEventListener("change", listener);
    return () => mq.removeEventListener("change", listener);
  }, []);

  const resolved: ResolvedTheme = preference === "system" ? systemTheme : preference;

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", resolved === "dark");
    root.style.colorScheme = resolved;
  }, [resolved]);

  const setPreference = (pref: ThemePreference) => {
    setPreferenceState(pref);
    try {
      window.localStorage.setItem(STORAGE_KEY, pref);
    } catch {
      // ignore
    }
  };

  const cycleTheme = () => {
    const next: ThemePreference =
      preference === "system" ? "dark" : preference === "dark" ? "light" : "system";
    setPreference(next);
  };

  return (
    <ThemeContext.Provider value={{ preference, resolved, setPreference, cycleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}
