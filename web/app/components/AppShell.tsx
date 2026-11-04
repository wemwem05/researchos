"use client";

import { useState, type ReactNode } from "react";

/**
 * Desktop application shell: fixed left sidebar (240px) + flexible content
 * capped at a readable width. On small screens the sidebar becomes a
 * slide-over opened from the compact top bar.
 */
export default function AppShell({
  sidebar,
  children,
  wide = false,
}: {
  sidebar: ReactNode;
  children: ReactNode;
  /** skip the readable-width cap (used by full-bleed views) */
  wide?: boolean;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      {/* Mobile top bar */}
      <div className="sticky top-0 z-30 flex items-center gap-3 border-b border-slate-200 bg-white px-4 py-2.5 lg:hidden">
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open navigation"
          className="rounded-lg border border-slate-300 px-2.5 py-1 text-sm text-slate-700"
        >
          ☰
        </button>
        <span className="text-sm font-bold tracking-tight">ResearchOS</span>
      </div>

      <div className="flex">
        {/* Desktop sidebar */}
        <aside className="sticky top-0 hidden h-screen w-60 shrink-0 overflow-y-auto border-r border-slate-200 bg-white lg:block">
          {sidebar}
        </aside>

        {/* Mobile slide-over */}
        {open && (
          <div className="fixed inset-0 z-40 lg:hidden">
            <div
              className="absolute inset-0 bg-slate-900/40"
              onClick={() => setOpen(false)}
              aria-hidden
            />
            <aside className="absolute inset-y-0 left-0 w-72 overflow-y-auto border-r border-slate-200 bg-white p-2">
              <div className="flex justify-end p-2">
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  aria-label="Close navigation"
                  className="rounded-lg border border-slate-300 px-2.5 py-1 text-sm text-slate-700"
                >
                  ✕
                </button>
              </div>
              <div onClick={() => setOpen(false)}>{sidebar}</div>
            </aside>
          </div>
        )}

        <main className="min-w-0 flex-1">
          <div
            className={
              wide
                ? "px-4 py-6 sm:px-8"
                : "mx-auto w-full max-w-[1240px] px-4 py-6 sm:px-8 sm:py-8"
            }
          >
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
