/**
 * ThemeToggle — three states, System / Light / Dark.
 *
 * Ported by copying from AgentWorth's `packages/ui/ThemeToggle.tsx` +
 * `useTheme.ts` (cross-repo imports are not allowed; the pattern is). Same
 * contract, same DOM shape, same three-state law from design.md:
 *
 *   system -> no `data-theme` attribute, so tokens.css's guarded
 *             `prefers-color-scheme` block decides
 *   light  -> `data-theme="light"`, which also disarms that guard
 *   dark   -> `data-theme="dark"`, which wins over the OS
 *
 * The `.dark` class is toggled alongside `data-theme` because tokens.css
 * defines `:root.dark` next to `:root[data-theme="dark"]` and the two must
 * never disagree.
 *
 * Storage key is pet-talk's own (`pet_talk_theme`) — AgentWorth's
 * `agentworth_theme` belongs to AgentWorth.
 */

import { useCallback, useEffect, useState } from "react";

export type Theme = "system" | "light" | "dark";

const THEME_STORAGE_KEY = "pet_talk_theme";

function readStoredTheme(): Theme {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    if (raw === "light" || raw === "dark" || raw === "system") return raw;
  } catch {
    // Private mode: fall through to system, which needs no storage.
  }
  return "system";
}

export function applyThemeToDocument(theme: Theme): void {
  const root = document.documentElement;
  const prefersDark =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches === true;
  const effectiveDark = theme === "dark" || (theme === "system" && prefersDark);
  root.classList.toggle("dark", effectiveDark);
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(readStoredTheme);

  useEffect(() => {
    applyThemeToDocument(theme);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // Not persisted; still applied for this page-load.
    }
  }, [theme]);

  // A system choice has to follow the OS while the page is open.
  useEffect(() => {
    if (theme !== "system" || typeof window === "undefined") return;
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = () => applyThemeToDocument("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  const choose = useCallback((t: Theme) => setTheme(t), []);
  return [theme, choose];
}

const ICONS: Record<Theme, JSX.Element> = {
  system: (
    <>
      <rect x="2.5" y="4" width="15" height="10" rx="1.5" />
      <path d="M7 17h6" />
    </>
  ),
  light: (
    <>
      <circle cx="10" cy="10" r="3.6" />
      <path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.5 4.5l1.4 1.4M14.1 14.1l1.4 1.4M15.5 4.5l-1.4 1.4M5.9 14.1l-1.4 1.4" />
    </>
  ),
  dark: <path d="M13.8 12.6A5.4 5.4 0 0 1 7.4 6.2a5.6 5.6 0 1 0 6.4 6.4z" />,
};

const OPTIONS: { value: Theme; label: string }[] = [
  { value: "system", label: "System theme" },
  { value: "light", label: "Light theme" },
  { value: "dark", label: "Dark theme" },
];

export function ThemeToggle({
  theme,
  onChange,
}: {
  theme: Theme;
  onChange: (t: Theme) => void;
}) {
  return (
    <div className="theme-toggle" role="group" aria-label="Theme">
      {OPTIONS.map((opt) => (
        <button
          key={opt.value}
          type="button"
          data-theme-choice={opt.value}
          aria-pressed={theme === opt.value}
          onClick={() => onChange(opt.value)}
          title={opt.label}
        >
          <svg
            viewBox="0 0 20 20"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            aria-hidden="true"
          >
            {ICONS[opt.value]}
          </svg>
          <span className="pt-sr-only">{opt.label}</span>
        </button>
      ))}
    </div>
  );
}
