"use client"

import { AreaChart, Area, XAxis, YAxis, ReferenceLine, ResponsiveContainer } from "recharts"
import { RunProfile, tierFor, TIERS } from "@/lib/types"

interface Props {
  run: RunProfile
  accentColor: string
  highlighted: boolean
  onHover: (name: string | null) => void
  onClick: (name: string) => void
  maxDist: number
  mounted: boolean
}

export default function RunRow({ run, accentColor, highlighted, onHover, onClick, maxDist, mounted }: Props) {
  const tier = tierFor(run.steepest)
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
        {run.osm_id ? (
          <a
            href={`https://www.openstreetmap.org/way/${run.osm_id}?layers=P`}
            target="_blank"
            rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            className="text-xs font-medium text-gray-700 leading-tight truncate block hover:text-blue-600 hover:underline"
          >
            {run.name}
          </a>
        ) : (
          <div className="text-xs font-medium text-gray-700 leading-tight truncate">{run.name}</div>
        )}
        <div className="text-xs font-bold" style={{ color: tier.color }}>{run.steepest.toFixed(1)}°</div>
      </div>

      {/* Slope profile chart */}
      <div className="flex-1" style={{ height: 40 }}>
        {mounted && (
          <ResponsiveContainer width="100%" height={40}>
            <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
              <XAxis dataKey="dist" type="number" domain={[0, maxDist]} hide />
              <YAxis domain={[0, 50]} hide />
              {TIERS.filter(t => t.min > 0).map(t => (
                <ReferenceLine
                  key={t.min}
                  y={t.min}
                  stroke={t.color}
                  strokeWidth={0.8}
                  strokeDasharray={t.min === 36 ? "3 3" : t.min === 27 ? "4 2" : "2 2"}
                />
              ))}
              <Area
                type="monotone"
                dataKey="slope"
                stroke={accentColor}
                fill={accentColor}
                fillOpacity={highlighted ? 0.6 : 0.4}
                strokeWidth={highlighted ? 1.2 : 0.8}
                isAnimationActive={false}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Length */}
      <div className="w-12 shrink-0 text-right text-xs text-gray-400">
        {run.length_km.toFixed(1)} km
      </div>
    </div>
  )
}
