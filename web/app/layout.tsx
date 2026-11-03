import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ResearchOS — Research-aware paper reader",
  description: "Read the paper with its equations, figures, and tables explained in the context of your research question.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-neutral-200 text-neutral-900 antialiased" suppressHydrationWarning>{children}</body>
    </html>
  );
}
