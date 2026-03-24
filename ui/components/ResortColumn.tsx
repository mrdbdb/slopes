"use client"

import { ResortData, tierFor, TIERS } from "@/lib/types"
import RunRow from "./RunRow"

interface Props {
  data: ResortData
  filterTier: string | null
  highlighted: string | null
  onHover: (name: string | null) => void
  onClick: (name: string) => void
  maxDist: number
}

const TIER_LABELS: Record<number, string> = {
  36: "Expert 36°+",
  27: "Advanced 27–36°",
  18: "Intermediate 18–27°",
  0:  "Beginner <18°",
}

export default function ResortColumn({ data, filterTier, highlighted, onHover, onClick, maxDist }: Props) {
  const visible = data.runs.filter(run => {
    if (run === null) return true
    if (!filterTier) return true
    return tierFor(run.steepest).label === filterTier
  })

  // Drop separators that are now adjacent or at the start/end
  const cleaned = visible.filter((run, i) => {
    if (run !== null) return true
    const prev = visible[i - 1]
    const next = visible[i + 1]
    return prev !== null && prev !== undefined && next !== null && next !== undefined
  })

  return (
    <div className="flex flex-col min-w-0">
      {/* Header */}
      <div className="px-2 py-2 border-b border-gray-200 mb-1">
        <h2 className="text-sm font-bold text-gray-800">{data.name}</h2>
        <div className="text-xs text-gray-400">
          {data.runs.filter(r => r !== null).length} runs
        </div>
      </div>

      {/* Runs */}
      <div className="overflow-y-auto flex-1">
        {cleaned.map((run, i) =>
          run === null ? (
            <div key={`sep-${i}`} className="my-1.5 border-t border-dashed border-gray-200" />
          ) : (
            <RunRow
              key={run.name}
              run={run}
              accentColor={data.color}
              highlighted={highlighted === run.name}
              onHover={onHover}
              onClick={onClick}
              maxDist={maxDist}
            />
          )
        )}
      </div>
    </div>
  )
}
