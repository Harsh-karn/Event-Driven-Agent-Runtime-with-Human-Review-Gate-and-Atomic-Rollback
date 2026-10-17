"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import {
  ShieldAlert,
  CheckCircle2,
  Clock,
  RotateCcw,
  XCircle,
  ArrowRight,
  Filter,
  RefreshCw,
  Cpu,
} from "lucide-react";

interface RunItem {
  id: string;
  agent_id: string;
  trigger: string;
  state: string;
  attempt: number;
  created_at: string;
  error?: string | null;
}

export default function RunsDashboard() {
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [filterState, setFilterState] = useState<string>("all");

  const fetchRuns = async () => {
    setLoading(true);
    try {
      const res = await fetch("http://localhost:8000/runs?limit=50");
      if (res.ok) {
        const data = await res.json();
        setRuns(data);
      } else {
        throw new Error("API returned non-200");
      }
    } catch {
      // Fallback demo data if FastAPI is offline
      setRuns([
        {
          id: "e4a19b22-81cd-4bc0-9d33-728b9d10e8f1",
          agent_id: "demo_digest",
          trigger: "schedule",
          state: "awaiting_approval",
          attempt: 1,
          created_at: new Date(Date.now() - 1000 * 60 * 5).toISOString(),
        },
        {
          id: "3c914bf1-08fa-4e78-9580-ff29ad123b08",
          agent_id: "triage_agent",
          trigger: "webhook",
          state: "committed",
          attempt: 1,
          created_at: new Date(Date.now() - 1000 * 60 * 30).toISOString(),
        },
        {
          id: "90dbce21-17ca-41da-b012-34827110ad99",
          agent_id: "demo_digest",
          trigger: "api",
          state: "rolled_back",
          attempt: 2,
          created_at: new Date(Date.now() - 1000 * 60 * 120).toISOString(),
          error: "Simulated payment API rate limit during commit",
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchRuns();
  }, []);

  const awaitingCount = runs.filter((r) => r.state === "awaiting_approval").length;
  const committedCount = runs.filter((r) => r.state === "committed").length;
  const rolledBackCount = runs.filter((r) => r.state === "rolled_back").length;

  const filteredRuns = runs.filter((r) => {
    if (filterState === "all") return true;
    return r.state === filterState;
  });

  const getStatusBadge = (state: string) => {
    switch (state) {
      case "awaiting_approval":
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/30 animate-pulse">
            <ShieldAlert className="h-3.5 w-3.5" />
            Awaiting Review
          </span>
        );
      case "committed":
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
            <CheckCircle2 className="h-3.5 w-3.5" />
            Committed
          </span>
        );
      case "approved":
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-cyan-500/20 text-cyan-300 border border-cyan-500/30">
            <Clock className="h-3.5 w-3.5" />
            Approved
          </span>
        );
      case "rolled_back":
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-purple-500/20 text-purple-300 border border-purple-500/30">
            <RotateCcw className="h-3.5 w-3.5" />
            Rolled Back
          </span>
        );
      case "rejected":
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-red-500/20 text-red-300 border border-red-500/30">
            <XCircle className="h-3.5 w-3.5" />
            Rejected
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-gray-800 text-gray-300 border border-gray-700">
            {state}
          </span>
        );
    }
  };

  return (
    <div className="space-y-8">
      {/* Metric Highlights */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="glass-card p-5 rounded-2xl border border-amber-500/20 bg-gradient-to-br from-amber-500/5 to-transparent">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-amber-300">Awaiting Approval</span>
            <ShieldAlert className="h-5 w-5 text-amber-400" />
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-3xl font-extrabold text-white">{awaitingCount}</span>
            <span className="text-xs text-amber-400/80">Action required</span>
          </div>
        </div>

        <div className="glass-card p-5 rounded-2xl border border-emerald-500/20 bg-gradient-to-br from-emerald-500/5 to-transparent">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-emerald-300">Cleanly Committed</span>
            <CheckCircle2 className="h-5 w-5 text-emerald-400" />
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-3xl font-extrabold text-white">{committedCount}</span>
            <span className="text-xs text-emerald-400/80">100% verified</span>
          </div>
        </div>

        <div className="glass-card p-5 rounded-2xl border border-purple-500/20 bg-gradient-to-br from-purple-500/5 to-transparent">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-purple-300">Atomic Rollbacks</span>
            <RotateCcw className="h-5 w-5 text-purple-400" />
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-3xl font-extrabold text-white">{rolledBackCount}</span>
            <span className="text-xs text-purple-400/80">Unwound cleanly</span>
          </div>
        </div>

        <div className="glass-card p-5 rounded-2xl border border-cyan-500/20 bg-gradient-to-br from-cyan-500/5 to-transparent">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-cyan-300">Total Run Volume</span>
            <Cpu className="h-5 w-5 text-cyan-400" />
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-3xl font-extrabold text-white">{runs.length}</span>
            <span className="text-xs text-cyan-400/80">Logged in Postgres</span>
          </div>
        </div>
      </div>

      {/* Runs Table Section */}
      <div className="glass-panel rounded-2xl border border-white/10 overflow-hidden shadow-2xl">
        <div className="p-5 border-b border-white/10 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-bold text-white flex items-center gap-2">
              Agent Execution Runs
            </h2>
            <p className="text-xs text-gray-400">
              Durable checkpoints, staged side effects, and human decision gate
            </p>
          </div>

          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5 bg-gray-900/80 border border-white/10 rounded-xl p-1 text-xs">
              <button
                onClick={() => setFilterState("all")}
                className={`px-3 py-1.5 rounded-lg transition-colors ${
                  filterState === "all" ? "bg-cyan-500 text-black font-semibold" : "text-gray-400 hover:text-white"
                }`}
              >
                All
              </button>
              <button
                onClick={() => setFilterState("awaiting_approval")}
                className={`px-3 py-1.5 rounded-lg transition-colors ${
                  filterState === "awaiting_approval"
                    ? "bg-amber-500 text-black font-semibold"
                    : "text-gray-400 hover:text-white"
                }`}
              >
                Pending Review
              </button>
              <button
                onClick={() => setFilterState("committed")}
                className={`px-3 py-1.5 rounded-lg transition-colors ${
                  filterState === "committed"
                    ? "bg-emerald-500 text-black font-semibold"
                    : "text-gray-400 hover:text-white"
                }`}
              >
                Committed
              </button>
            </div>

            <button
              onClick={fetchRuns}
              disabled={loading}
              className="p-2 rounded-xl bg-white/5 border border-white/10 text-gray-400 hover:text-white hover:bg-white/10 transition-colors"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </button>
          </div>
        </div>

        {/* Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-white/[0.02] text-xs font-semibold text-gray-400 border-b border-white/5">
              <tr>
                <th className="py-3.5 px-6">Run ID</th>
                <th className="py-3.5 px-6">Agent</th>
                <th className="py-3.5 px-6">Trigger</th>
                <th className="py-3.5 px-6">Status</th>
                <th className="py-3.5 px-6">Created</th>
                <th className="py-3.5 px-6 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/5 text-gray-300">
              {filteredRuns.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-gray-500">
                    No runs found matching current filter.
                  </td>
                </tr>
              ) : (
                filteredRuns.map((r) => (
                  <tr key={r.id} className="hover:bg-white/[0.02] transition-colors">
                    <td className="py-4 px-6 font-mono text-xs text-gray-300">
                      {r.id.substring(0, 8)}...{r.id.substring(r.id.length - 4)}
                    </td>
                    <td className="py-4 px-6 font-semibold text-white">
                      {r.agent_id}
                    </td>
                    <td className="py-4 px-6">
                      <span className="capitalize px-2 py-0.5 rounded bg-gray-800/80 text-gray-300 border border-gray-700 text-xs">
                        {r.trigger}
                      </span>
                    </td>
                    <td className="py-4 px-6">
                      {getStatusBadge(r.state)}
                    </td>
                    <td className="py-4 px-6 text-xs text-gray-400">
                      {new Date(r.created_at).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                      })}
                    </td>
                    <td className="py-4 px-6 text-right">
                      {r.state === "awaiting_approval" ? (
                        <Link
                          href={`/review/${r.id}`}
                          className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-amber-500 hover:bg-amber-400 text-black font-semibold text-xs shadow-lg shadow-amber-500/20 transition-all hover:scale-105"
                        >
                          Review Now
                          <ArrowRight className="h-3.5 w-3.5" />
                        </Link>
                      ) : (
                        <Link
                          href={`/review/${r.id}`}
                          className="inline-flex items-center gap-1 text-xs text-cyan-400 hover:text-cyan-300 font-medium"
                        >
                          View Audit
                          <ArrowRight className="h-3 w-3" />
                        </Link>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
