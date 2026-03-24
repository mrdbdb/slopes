"use client"

import { useState, useEffect } from "react"
import dynamic from "next/dynamic"
import Link from "next/link"
import { TIERS, tierFor } from "@/lib/types"
import type { RunGeo, LiftGeo } from "@/components/MapView"
import { AreaChart, Area, XAxis, YAxis, ReferenceLine, ResponsiveContainer, Tooltip } from "recharts"

const MapView = dynamic(() => import("@/components/MapView"), { ssr: false })

interface ResortMeta { name: string; slug: string; color: string }

interface GeoJSON {
  resort: string
  color: string
  features: {
    geometry: { type: string; coordinates: [number, number][] | [[number, number][]] }
    properties: {
      name: string; steepest: number; face_steepest?: number; is_traverse?: boolean
      osm_id?: number; slopes: number[]; line_slopes?: number[]; is_area?: boolean
    }
  }[]
}

function haversineKm([lon1, lat1]: [number, number], [lon2, lat2]: [number, number]): number {
  const R = 6371
  const dLat = (lat2 - lat1) * Math.PI / 180
  const dLon = (lon2 - lon1) * Math.PI / 180
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2
  return R * 2 * Math.asin(Math.sqrt(a))
}

function runCentroid(run: RunGeo): [number, number] {
  const n = run.coordinates.length
  return [
    run.coordinates.reduce((s, c) => s + c[0], 0) / n,
    run.coordinates.reduce((s, c) => s + c[1], 0) / n,
  ]
}

// Rename same-named runs that are geographically far apart with a (1), (2) index.
function disambiguateRuns(runs: RunGeo[], thresholdKm = 1): RunGeo[] {
  const byName = new Map<string, number[]>()
  runs.forEach((r, i) => {
    if (!byName.has(r.name)) byName.set(r.name, [])
    byName.get(r.name)!.push(i)
  })

  const renamed = new Map<number, string>()

  for (const [name, indices] of byName) {
    if (indices.length <= 1) continue
    const centroids = indices.map(i => runCentroid(runs[i]))

    // Greedy clustering: assign each run to the first cluster within threshold
    const clusterOf: number[] = new Array(indices.length).fill(-1)
    let numClusters = 0
    for (let i = 0; i < indices.length; i++) {
      if (clusterOf[i] !== -1) continue
      clusterOf[i] = numClusters
      for (let j = i + 1; j < indices.length; j++) {
        if (clusterOf[j] !== -1) continue
        if (haversineKm(centroids[i], centroids[j]) <= thresholdKm) clusterOf[j] = numClusters
      }
      numClusters++
    }
    if (numClusters <= 1) continue

    indices.forEach((runIdx, i) => renamed.set(runIdx, `${name} (${clusterOf[i] + 1})`))
  }

  return renamed.size === 0 ? runs : runs.map((r, i) => renamed.has(i) ? { ...r, name: renamed.get(i)! } : r)
}

function buildProfile(segments: RunGeo[], useFace: boolean): { dist: number; slope: number; lon: number; lat: number }[] {
  const points: { dist: number; slope: number; lon: number; lat: number }[] = []
  let cumDist = 0
  for (const seg of segments) {
    const { coordinates } = seg
    const slopes = useFace ? seg.slopes : (seg.line_slopes ?? seg.slopes)
    for (let i = 0; i < slopes.length; i++) {
      const [lon, lat] = coordinates[i]
      points.push({ dist: parseFloat(cumDist.toFixed(3)), slope: Math.max(0, slopes[i]), lon, lat })
      cumDist += haversineKm(coordinates[i], coordinates[i + 1])
    }
  }
  if (points.length > 0) {
    const last = segments[segments.length - 1]
    const [lon, lat] = last.coordinates[last.coordinates.length - 1]
    points.push({ dist: parseFloat(cumDist.toFixed(3)), slope: 0, lon, lat })
  }
  return points
}

export default function MapPage() {
  const [resorts, setResorts]         = useState<ResortMeta[]>([])
  const [slug, setSlug]               = useState("palisades_tahoe")
  const [runs, setRuns]               = useState<RunGeo[]>([])
  const [lifts, setLifts]             = useState<LiftGeo[]>([])
  const [loading, setLoading]         = useState(true)
  const [hovered, setHovered]         = useState<string | null>(null)
  const [pinnedRun, setPinnedRun]     = useState<string | null>(null)
  const [mounted, setMounted]         = useState(false)
  const [hiddenTiers, setHiddenTiers]         = useState<Set<string>>(new Set())
  const [chartHoverCoord, setChartHoverCoord] = useState<[number, number] | null>(null)
  const [useFace, setUseFace]                 = useState(true)
  const [minDelta, setMinDelta]               = useState(0)

  useEffect(() => {
    setMounted(true)
    fetch("/data/index.json").then(r => r.json()).then((data: ResortMeta[]) => {
      setResorts(data)
    })
    try {
      const saved = localStorage.getItem("ski-map-slug")
      if (saved) setSlug(saved)
      const prefs = JSON.parse(localStorage.getItem("ski-prefs") ?? "{}")
      if (prefs.hiddenTiers?.length) setHiddenTiers(new Set(prefs.hiddenTiers))
    } catch {}
  }, [])

  useEffect(() => {
    if (!mounted) return
    try { localStorage.setItem("ski-map-slug", slug) } catch {}
  }, [slug, mounted])

  useEffect(() => {
    if (!mounted) return
    try {
      const prefs = JSON.parse(localStorage.getItem("ski-prefs") ?? "{}")
      localStorage.setItem("ski-prefs", JSON.stringify({ ...prefs, hiddenTiers: Array.from(hiddenTiers) }))
    } catch {}
  }, [hiddenTiers, mounted])

  function toggleTier(label: string) {
    setHiddenTiers(prev => {
      const next = new Set(prev)
      if (next.has(label)) next.delete(label)
      else next.add(label)
      return next
    })
  }

  useEffect(() => {
    setLoading(true)
    setRuns([])
    setLifts([])
    Promise.all([
      fetch(`/data/${slug}_geo.json`).then(r => r.json()),
      fetch(`/data/${slug}_lifts.json`).then(r => r.json()).catch(() => ({ features: [] })),
    ]).then(([geo, liftsJson]) => {
      const parsed: RunGeo[] = (geo as GeoJSON).features.map(f => ({
        name:          f.properties.name,
        steepest:      f.properties.steepest,
        face_steepest: f.properties.face_steepest,
        is_traverse:   f.properties.is_traverse,
        osm_id:        f.properties.osm_id,
        slopes:        f.properties.slopes,
        line_slopes:   f.properties.line_slopes,
        is_area:       f.properties.is_area ?? false,
        coordinates:   (f.geometry.type === "Polygon"
          ? (f.geometry.coordinates as unknown as [number,number][][])[0]
          : f.geometry.coordinates) as [number, number][],
      }))
      parsed.sort((a, b) => b.steepest - a.steepest)
      setRuns(disambiguateRuns(parsed))
      setLifts(liftsJson.features.map((f: { geometry: { coordinates: [number,number][] }, properties: { name: string, type: string } }) => ({
        name:        f.properties.name,
        type:        f.properties.type,
        coordinates: f.geometry.coordinates,
      })))
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [slug])

  function handleRunClick(name: string) {
    setPinnedRun(prev => prev === name ? null : name)
  }

  // Suppress pin if its tier is currently hidden
  const pinnedRunData  = pinnedRun ? runs.find(r => r.name === pinnedRun) : undefined
  const effectivePin   = pinnedRunData && hiddenTiers.has(tierFor(pinnedRunData.steepest).label) ? null : pinnedRun

  // Runs dimmed by delta filter: have face data but delta < threshold
  const dimmedByDelta = new Set(
    minDelta > 0
      ? runs
          .filter(r => r.face_steepest != null && (r.face_steepest - r.steepest) < minDelta)
          .map(r => r.name)
      : []
  )

  // Deduplicate by name for the sidebar (multiple OSM segments can share a name)
  const uniqueRuns = Array.from(
    runs.reduce((map, r) => {
      const existing = map.get(r.name)
      if (!existing || r.steepest > existing.steepest) map.set(r.name, r)
      return map
    }, new Map<string, RunGeo>()).values()
  )

  // Group runs by tier for the sidebar (only visible tiers, respecting delta filter)
  const grouped = TIERS.map(tier => ({
    tier,
    runs: uniqueRuns.filter(r =>
      tierFor(r.steepest).label === tier.label &&
      !hiddenTiers.has(tier.label) &&
      !dimmedByDelta.has(r.name)
    ),
  })).filter(g => g.runs.length > 0)

  return (
    <div className="flex flex-col h-screen bg-white">
      {/* Header */}
      <header className="flex items-center gap-3 px-4 py-2 border-b border-gray-200 shrink-0 text-sm">
        <Link href="/" className="text-gray-400 hover:text-gray-700 text-xs">← Charts</Link>
        <span className="font-bold text-gray-800">Run Map</span>

        <select
          value={slug}
          onChange={e => setSlug(e.target.value)}
          className="px-2 py-0.5 border border-gray-200 rounded text-xs text-gray-700 focus:outline-none focus:border-gray-400"
        >
          {resorts.map(r => (
            <option key={r.slug} value={r.slug}>{r.name}</option>
          ))}
        </select>

        {!loading && <span className="text-xs text-gray-400">{runs.length} runs</span>}

        <div className="flex gap-1 items-center">
          <button
            onClick={() => setHiddenTiers(new Set())}
            className={`px-2 py-0.5 rounded text-xs font-medium border transition-colors ${
              hiddenTiers.size === 0
                ? "bg-gray-800 text-white border-gray-800"
                : "text-gray-500 border-gray-200 hover:border-gray-400"
            }`}
          >
            All
          </button>
          {TIERS.map(t => {
            const visible = !hiddenTiers.has(t.label)
            return (
              <button
                key={t.label}
                onClick={() => toggleTier(t.label)}
                className="px-2 py-0.5 rounded text-xs font-medium border transition-colors"
                style={visible
                  ? { background: t.color, color: "#fff", borderColor: t.color }
                  : { color: "#aaa", borderColor: "#ddd" }}
              >
                {t.label}
              </button>
            )
          })}
        </div>

        <div className="ml-auto flex items-center gap-2 border-l border-gray-200 pl-3">
          <span className="text-gray-400 text-xs">Δ≥</span>
          <input
            type="range" min={0} max={20} step={1} value={minDelta}
            onChange={e => setMinDelta(Number(e.target.value))}
            className="w-20 accent-gray-700"
          />
          <span className="tabular-nums text-xs text-gray-600 w-5">{minDelta}°</span>
          <div className="flex gap-0.5 ml-1">
            {(["Face", "Line"] as const).map(mode => (
              <button
                key={mode}
                onClick={() => setUseFace(mode === "Face")}
                className="px-2 py-0.5 rounded text-xs font-medium border transition-colors"
                style={
                  (mode === "Face") === useFace
                    ? { background: "#1f2937", color: "#fff", borderColor: "#1f2937" }
                    : { color: "#aaa", borderColor: "#ddd" }
                }
              >
                {mode}
              </button>
            ))}
          </div>
        </div>
      </header>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Run list */}
        <div className="w-60 shrink-0 border-r border-gray-100 overflow-y-auto text-xs">
          {loading && (
            <div className="flex items-center justify-center h-20 text-gray-400">Loading…</div>
          )}
          {grouped.map(({ tier, runs: tierRuns }) => (
            <div key={tier.label}>
              <div
                className="px-3 py-1 font-semibold text-xs border-b"
                style={{ color: tier.color, background: tier.color + "12", borderColor: tier.color + "33" }}
              >
                {tier.label} {tier.min > 0 ? `≥ ${tier.min}°` : ""}
              </div>
              {tierRuns.map((run, i) => {
                const isPinned  = effectivePin === run.name
                const isHovered = hovered   === run.name
                return (
                  <div
                    key={`${run.name}-${i}`}
                    data-run={run.name}
                    className={`flex items-center gap-2 px-3 cursor-pointer transition-all ${
                      isPinned ? "py-2.5" : "py-1.5"
                    }`}
                    style={{
                      background: isPinned ? "#fef9c3" : isHovered ? "#f3f4f6" : undefined,
                    }}
                    onMouseEnter={() => setHovered(run.name)}
                    onMouseLeave={() => setHovered(null)}
                    onClick={() => handleRunClick(run.name)}
                  >
                    <span className={`flex-1 truncate text-gray-700 transition-all ${isPinned ? "font-bold text-sm" : ""}`}>
                      {run.name}
                    </span>
                    {run.is_traverse && (
                      <span className="text-amber-400 text-xs shrink-0" title={`face ${run.face_steepest?.toFixed(1)}°`}>▲</span>
                    )}
                    <span className={`shrink-0 font-medium tabular-nums transition-all ${isPinned ? "text-sm" : ""}`} style={{ color: tier.color }}>
                      {run.steepest.toFixed(1)}°
                    </span>
                  </div>
                )
              })}
            </div>
          ))}
        </div>

        {/* Map — always mounted to avoid Leaflet DOM teardown errors */}
        <div className="flex-1 relative">
          {loading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/60 text-gray-400 text-sm">
              Loading…
            </div>
          )}
          <MapView
            key={slug}
            runs={runs}
            lifts={lifts}
            hovered={hovered}
            pinned={effectivePin}
            onHover={setHovered}
            onRunClick={handleRunClick}
            focusRun={effectivePin}
            hiddenTiers={hiddenTiers}
            dimmedRuns={dimmedByDelta}
            useFace={useFace}
            chartHoverCoord={chartHoverCoord}
          />
          {/* Slope profile chart overlay */}
          {effectivePin && mounted && (() => {
            const segments   = runs.filter(r => r.name === effectivePin)
            const profile    = buildProfile(segments, useFace)
            const run0       = segments[0]
            const tier       = tierFor(run0?.steepest ?? 0)
            const totalKm    = profile.length > 0 ? profile[profile.length - 1].dist : 0
            const faceDelta  = run0?.face_steepest != null ? run0.face_steepest - run0.steepest : null
            return (
              <div className="absolute bottom-0 left-0 right-0 bg-white/95 border-t border-gray-200 z-[1000] px-4 pt-2 pb-1" style={{ backdropFilter: "blur(4px)" }}>
                <div className="flex items-baseline gap-2">
                  <span className="text-xs font-bold text-gray-800">{effectivePin}</span>
                  <span className="text-xs font-bold" style={{ color: tier.color }}>{run0?.steepest.toFixed(1)}°</span>
                  {run0?.face_steepest != null && (
                    <span className="text-xs text-gray-400">
                      face {run0.face_steepest.toFixed(1)}°
                      {faceDelta != null && faceDelta >= 1 && (
                        <span style={{ color: run0.is_traverse ? "#f59e0b" : "#6b7280" }}> (+{faceDelta.toFixed(1)}°)</span>
                      )}
                    </span>
                  )}
                  <span className="text-xs text-gray-400">{totalKm.toFixed(2)} km</span>
                </div>
                <ResponsiveContainer width="100%" height={70}>
                  <AreaChart
                    data={profile}
                    margin={{ top: 2, right: 4, bottom: 0, left: 0 }}
                    onMouseMove={(state) => {
                      const pt = state?.activePayload?.[0]?.payload
                      if (pt) setChartHoverCoord([pt.lon, pt.lat])
                    }}
                    onMouseLeave={() => setChartHoverCoord(null)}
                  >
                    <XAxis
                      dataKey="dist"
                      type="number"
                      domain={[0, totalKm]}
                      tickFormatter={v => `${(v as number).toFixed(1)}`}
                      tick={{ fontSize: 9, fill: "#9ca3af" }}
                      tickLine={false}
                      axisLine={false}
                    />
                    <YAxis domain={[0, 55]} hide />
                    <Tooltip
                      cursor={{ stroke: "#94a3b8", strokeWidth: 1, strokeDasharray: "3 3" }}
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
                      stroke={tier.color}
                      fill={tier.color}
                      fillOpacity={0.35}
                      strokeWidth={1.5}
                      isAnimationActive={false}
                      dot={false}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )
          })()}
        </div>
      </div>
    </div>
  )
}
