"use client"

import { RunProfile, tierFor, effectiveSteepest } from "@/lib/types"
import SlopeProfileChart from "./SlopeProfileChart"

export function PisteBadge({ difficulty }: { difficulty: string }) {
  switch (difficulty) {
    case "novice":
    case "easy":
      return (
        <svg width="10" height="10" viewBox="0 0 10 10" className="inline-block shrink-0">
          <circle cx="5" cy="5" r="4.5" fill="#22c55e" />
        </svg>
      )
    case "intermediate":
      return (
        <svg width="10" height="10" viewBox="0 0 10 10" className="inline-block shrink-0">
          <rect x="0.5" y="0.5" width="9" height="9" fill="#3b82f6" />
        </svg>
      )
    case "advanced":
      return (
        <svg width="10" height="10" viewBox="0 0 10 10" className="inline-block shrink-0">
          <rect x="1" y="1" width="8" height="8" fill="#1f2937" transform="rotate(45 5 5)" />
        </svg>
      )
    case "expert":
      return (
        <svg width="16" height="10" viewBox="0 0 16 10" className="inline-block shrink-0">
          <rect x="1" y="1" width="8" height="8" fill="#1f2937" transform="rotate(45 5 5)" />
          <rect x="7" y="1" width="8" height="8" fill="#1f2937" transform="rotate(45 11 5)" />
        </svg>
      )
    case "freeride":
      return (
        <svg width="12" height="10" viewBox="0 0 12 10" className="inline-block shrink-0">
          <ellipse cx="6" cy="5" rx="5.5" ry="4.5" fill="#f97316" />
        </svg>
      )
    default:
      return null
  }
}

interface Props {
  run: RunProfile
  highlighted: boolean
  onHover: (name: string | null) => void
  onClick: (name: string) => void
  maxDist: number
  mounted: boolean
}

export default function RunRow({ run, highlighted, onHover, onClick, maxDist, mounted }: Props) {
  const tier = tierFor(effectiveSteepest(run))
  const data = run.profile.map(([dist, slope]) => ({ dist, slope: Math.max(0, slope) }))

  return (
    <div
      className={`flex items-center gap-2 px-2 py-0.5 cursor-pointer rounded transition-colors ${
        highlighted ? "bg-blue-50" : "hover:bg-gray-50"
      }`}
      onMouseEnter={() => onHover(run.name)}
      onMouseLeave={() => onHover(null)}
      onClick={() => onClick(run.name)}
    >
      {/* Run name + steepest */}
      <div className="w-44 shrink-0 text-right pr-2">
        <div className="flex items-center justify-end gap-1">
          {run.osm_difficulty && <PisteBadge difficulty={run.osm_difficulty} />}
          {run.osm_id ? (
            <a
              href={`https://www.openstreetmap.org/way/${run.osm_id}?layers=P`}
              target="_blank"
              rel="noopener noreferrer"
              onClick={e => e.stopPropagation()}
              className="text-xs font-medium text-gray-700 leading-tight truncate hover:text-blue-600 hover:underline"
            >
              {run.name}
            </a>
          ) : (
            <span className="text-xs font-medium text-gray-700 leading-tight truncate">{run.name}</span>
          )}
        </div>
        <div className="text-xs font-bold" style={{ color: tier.color }}>{effectiveSteepest(run).toFixed(1)}°</div>
      </div>

      {/* Slope profile chart */}
      <div className="flex-1" style={{ height: 40 }}>
        <SlopeProfileChart
          profile={data}
          maxDist={maxDist}
          height={40}
          mounted={mounted}
          fillOpacity={highlighted ? 0.55 : 0.35}
          strokeWidth={highlighted ? 1.5 : 1}
        />
      </div>

      {/* Length */}
      <div className="w-12 shrink-0 text-right text-xs text-gray-400">
        {run.length_km.toFixed(1)} km
      </div>
    </div>
  )
}
