"use client";

import Link from "next/link";
import type { ReactNode } from "react";

/**
 * Shared ResearchOS design primitives (visual only — no data fetching).
 *
 * Palette: slate surfaces/text, indigo-600 primary actions.
 * Spacing scale: 4 / 8 / 12 / 16 / 24 / 32 / 48.
 */

export const PRIMARY_BUTTON =
  "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-40";

export const SECONDARY_BUTTON =
  "rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:border-slate-400 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-40";

export const SMALL_BUTTON =
  "rounded-lg border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-700 hover:border-slate-400 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-40";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-xl border border-slate-200 bg-white p-6 ${className}`}>
      {children}
    </div>
  );
}

export function PageHeader({
  breadcrumb,
  title,
  description,
  actions,
}: {
  breadcrumb?: ReactNode;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {breadcrumb && (
          <div className="text-xs font-medium text-slate-500">{breadcrumb}</div>
        )}
        <h1 className="mt-1 text-2xl font-bold tracking-tight text-slate-900">
          {title}
        </h1>
        {description && (
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-slate-500">
            {description}
          </p>
        )}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function SectionHeader({
  title,
  action,
}: {
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-3 flex items-center justify-between gap-3">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-500">
        {title}
      </h2>
      {action}
    </div>
  );
}

export function MetricCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3">
      <div className="text-2xl font-bold tracking-tight text-slate-900">{value}</div>
      <div className="mt-0.5 text-xs font-medium text-slate-500">{label}</div>
      {hint && <div className="mt-0.5 text-[11px] text-slate-400">{hint}</div>}
    </div>
  );
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white px-6 py-8 text-center">
      <p className="text-sm font-semibold text-slate-900">{title}</p>
      <p className="mx-auto mt-1 max-w-md text-[13px] leading-relaxed text-slate-500">
        {body}
      </p>
      {action && <div className="mt-4 flex justify-center gap-2">{action}</div>}
    </div>
  );
}

const DOT_COLOR: Record<string, string> = {
  green: "bg-green-500",
  amber: "bg-amber-500",
  red: "bg-red-500",
  indigo: "bg-indigo-500",
  slate: "bg-slate-300",
};

export function StatusIndicator({
  color,
  label,
}: {
  color: "green" | "amber" | "red" | "indigo" | "slate";
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-600">
      <span className={`h-2 w-2 rounded-full ${DOT_COLOR[color]}`} />
      {label}
    </span>
  );
}

export function TextLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  return (
    <Link
      href={href}
      className="text-[13px] font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
    >
      {children}
    </Link>
  );
}
