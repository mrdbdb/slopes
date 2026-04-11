"use client"

import { useId } from "react"
import { AreaChart, Area, XAxis, YAxis, ReferenceLine, ResponsiveContainer, Tooltip } from "recharts"
import { TIERS, tierFor } from "@/lib/types"

export interface ProfilePoint {
  dist: number
  slope: number
}

interface Props<P extends ProfilePoint> {
  profile: P[]
  maxDist: number
  height: number
  mounted: boolean
  showAxis?: boolean
  showTooltip?: boolean
  fillOpacity?: number
  strokeWidth?: number
  onScrub?: (pt: P) => void
}

export default function SlopeProfileChart<P extends ProfilePoint>({
  profile,
  maxDist,
  height,
  mounted,
  showAxis = false,
  showTooltip = false,
  fillOpacity = 0.4,
  strokeWidth = 1,
  onScrub,
}: Props<P>) {
  const rid = useId().replace(/[^a-zA-Z0-9]/g, "")
  const gradId = `slopeGrad-${rid}`
  const runLen = profile.length > 0 ? profile[profile.length - 1].dist : 0

  if (!mounted) return null

  const scrub = onScrub
    ? (e: React.PointerEvent<HTMLDivElement>) => {
        const rect = e.currentTarget.getBoundingClientRect()
        const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
        const target = frac * runLen
        let best = 0
        for (let i = 1; i < profile.length; i++) {
          if (Math.abs(profile[i].dist - target) < Math.abs(profile[best].dist - target)) best = i
        }
        const pt = profile[best]
        if (pt) onScrub(pt)
      }
    : undefined

  const chart = (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={profile} margin={{ top: 2, right: showAxis ? 4 : 0, bottom: 0, left: 0 }}>
        <XAxis
          dataKey="dist"
          type="number"
          domain={[0, maxDist]}
          hide={!showAxis}
          tickFormatter={showAxis ? v => `${(v as number).toFixed(1)}` : undefined}
          tick={showAxis ? { fontSize: 9, fill: "#9ca3af" } : undefined}
          tickLine={false}
          axisLine={false}
        />
        <YAxis domain={[0, 55]} hide />
        {showTooltip && (
          <Tooltip
            cursor={{ stroke: "#94a3b8", strokeWidth: 1, strokeDasharray: "3 3" }}
            isAnimationActive={false}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const slope = payload[0]?.value as number
              const t = tierFor(slope)
              return (
                <div className="text-xs font-bold px-1.5 py-0.5 rounded shadow-sm bg-white border border-gray-200" style={{ color: t.color }}>
                  {slope.toFixed(1)}°
                </div>
              )
            }}
          />
        )}
        {TIERS.filter(t => t.min > 0).map(t => (
          <ReferenceLine
            key={t.min}
            y={t.min}
            stroke={t.color}
            strokeWidth={0.8}
            strokeDasharray={t.min === 36 ? "3 3" : t.min === 27 ? "4 2" : "2 2"}
          />
        ))}
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="0">
            {profile.map((pt, i) => {
              const pct = runLen > 0 ? (pt.dist / runLen) * 100 : 0
              return <stop key={i} offset={`${pct}%`} stopColor={tierFor(pt.slope).color} />
            })}
          </linearGradient>
        </defs>
        <Area
          type="monotone"
          dataKey="slope"
          stroke={`url(#${gradId})`}
          fill={`url(#${gradId})`}
          fillOpacity={fillOpacity}
          strokeWidth={strokeWidth}
          isAnimationActive={false}
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  )

  if (!scrub) return chart
  return <div onPointerMove={scrub} onPointerDown={scrub}>{chart}</div>
}
