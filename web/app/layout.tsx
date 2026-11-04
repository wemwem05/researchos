import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ResearchOS — From reading papers to investigating science",
  description: "Discover literature, understand scientific papers, connect evidence, audit claims and test computational hypotheses.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-slate-50 text-slate-900 antialiased" suppressHydrationWarning>{children}</body>
    </html>
  );
}
