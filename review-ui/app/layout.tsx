import "./globals.css";
import React from "react";
import Link from "next/link";
import { ShieldCheck, Activity, Terminal, RefreshCw } from "lucide-react";

export const metadata = {
  title: "Tether | Human Review Gate & Runtime",
  description: "Durable, event-triggered agent runtime on Supabase and GCP with atomic rollback.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-tether-bg text-gray-100 antialiased selection:bg-cyan-500 selection:text-black">
        {/* Navigation Bar */}
        <header className="sticky top-0 z-50 glass-panel border-b border-white/10 px-6 py-4">
          <div className="max-w-7xl mx-auto flex items-center justify-between">
            <Link href="/" className="flex items-center gap-3 group">
              <div className="h-10 w-10 rounded-xl bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center shadow-lg shadow-cyan-500/20 group-hover:scale-105 transition-transform">
                <ShieldCheck className="h-6 w-6 text-white" />
              </div>
              <div>
                <span className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
                  Tether <span className="text-xs px-2 py-0.5 rounded-full bg-cyan-500/20 text-cyan-300 border border-cyan-500/30">Review Gate</span>
                </span>
                <p className="text-xs text-gray-400">Execution Layer & Human Gate</p>
              </div>
            </Link>

            <div className="flex items-center gap-4">
              <div className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-lg bg-gray-900/60 border border-white/5 text-xs text-gray-300">
                <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse"></span>
                <span>Runtime Live</span>
                <span className="text-gray-500">|</span>
                <span>Postgres & pgmq</span>
              </div>
              <Link
                href="/"
                className="text-sm font-medium text-gray-300 hover:text-white px-3 py-1.5 rounded-lg hover:bg-white/5 transition-colors flex items-center gap-1.5"
              >
                <Activity className="h-4 w-4 text-cyan-400" />
                Runs
              </Link>
            </div>
          </div>
        </header>

        {/* Main Content Area */}
        <main className="max-w-7xl mx-auto px-6 py-8">
          {children}
        </main>
      </body>
    </html>
  );
}
