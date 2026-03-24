"use client"

import { useState, useEffect } from "react"
import Link from "next/link"
import dynamic from "next/dynamic"
import { TIERS, tierFor, effectiveSteepest } from "@/lib/types"
import type { RunGeo, LiftGeo } from "@/components/MapView"
import { PisteBadge } from "@/components/RunRow"
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
      osm_id?: number; osm_difficulty?: string; slopes: number[]; line_slopes?: number[]; is_area?: boolean
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
  const [mobilePanel, setMobilePanel] = useState<"list" | "filters" | "settings" | null>(null)
  const [bearing, setBearing]         = useState(180)
  const [showLocation, setShowLocation] = useState(false)
  const [userLocation, setUserLocation] = useState<{ lat: number; lon: number; accuracy: number } | null>(null)

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
      if (typeof prefs.useFace    === "boolean") setUseFace(prefs.useFace)
      if (typeof prefs.bearing    === "number")  setBearing(prefs.bearing)
      if (typeof prefs.showLocation === "boolean") setShowLocation(prefs.showLocation)
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
      localStorage.setItem("ski-prefs", JSON.stringify({
        ...prefs,
        hiddenTiers: Array.from(hiddenTiers),
        useFace,
        bearing,
        showLocation,
      }))
    } catch {}
  }, [hiddenTiers, useFace, bearing, showLocation, mounted])

  function toggleTier(label: string) {
    setHiddenTiers(prev => {
      const next = new Set(prev)
      if (next.has(label)) next.delete(label)
      else next.add(label)
      return next
    })
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setRuns([])
    setLifts([])
    Promise.all([
      fetch(`/data/${slug}_geo.json`).then(r => r.json()),
      fetch(`/data/${slug}_lifts.json`).then(r => r.json()).catch(() => ({ features: [] })),
    ]).then(([geo, liftsJson]) => {
      if (cancelled) return
      const parsed: RunGeo[] = (geo as GeoJSON).features.map(f => ({
        name:          f.properties.name,
        steepest:      f.properties.steepest,
        face_steepest: f.properties.face_steepest,
        is_traverse:   f.properties.is_traverse,
        osm_id:         f.properties.osm_id,
        osm_difficulty: f.properties.osm_difficulty,
        slopes:         f.properties.slopes,
        line_slopes:   f.properties.line_slopes,
        is_area:       f.properties.is_area ?? false,
        coordinates:   (f.geometry.type === "Polygon"
          ? (f.geometry.coordinates as unknown as [number,number][][])[0]
          : f.geometry.coordinates) as [number, number][],
      }))
      parsed.sort((a, b) => effectiveSteepest(b) - effectiveSteepest(a))
      setRuns(disambiguateRuns(parsed))
      setLifts(liftsJson.features.map((f: { geometry: { coordinates: [number,number][] }, properties: { name: string, type: string } }) => ({
        name:        f.properties.name,
        type:        f.properties.type,
        coordinates: f.geometry.coordinates,
      })))
      setLoading(false)
    }).catch(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [slug])

  useEffect(() => {
    if (!showLocation || !navigator.geolocation) return
    const id = navigator.geolocation.watchPosition(
      pos => setUserLocation({ lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: pos.coords.accuracy }),
      () => setShowLocation(false),
      { enableHighAccuracy: true }
    )
    return () => navigator.geolocation.clearWatch(id)
  }, [showLocation])

  function handleRunClick(name: string) {
    setPinnedRun(prev => prev === name ? null : name)
    setMobilePanel(null)
  }

  const pinnedRunData  = pinnedRun ? runs.find(r => r.name === pinnedRun) : undefined
  const effectivePin   = pinnedRunData && hiddenTiers.has(tierFor(effectiveSteepest(pinnedRunData)).label) ? null : pinnedRun


  const uniqueRuns = Array.from(
    runs.reduce((map, r) => {
      const existing = map.get(r.name)
      if (!existing || effectiveSteepest(r) > effectiveSteepest(existing)) map.set(r.name, r)
      return map
    }, new Map<string, RunGeo>()).values()
  )

  const grouped = TIERS.map(tier => ({
    tier,
    runs: uniqueRuns.filter(r =>
      tierFor(effectiveSteepest(r)).label === tier.label &&
      !hiddenTiers.has(tier.label)
    ),
  })).filter(g => g.runs.length > 0)

  const runListContent = (
    <>
      {loading && (
        <div className="flex items-center justify-center h-20 text-gray-400">Loading…</div>
      )}
      {grouped.map(({ tier, runs: tierRuns }) => (
        <div key={tier.label}>
          <div
            className="sticky top-0 z-10 px-3 py-1 font-semibold text-xs border-b"
            style={{ background: tier.color, color: "white", borderColor: tier.color }}
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
                <span className={`flex-1 truncate text-gray-700 transition-all flex items-center gap-1 ${isPinned ? "font-bold text-sm" : ""}`}>
                  {run.osm_difficulty && <PisteBadge difficulty={run.osm_difficulty} />}
                  {run.name}
                </span>
                {run.is_traverse && (
                  <span className="text-amber-400 text-xs shrink-0" title={`face ${run.face_steepest?.toFixed(1)}°`}>▲</span>
                )}
                <span className={`shrink-0 font-medium tabular-nums transition-all ${isPinned ? "text-sm" : ""}`} style={{ color: tier.color }}>
                  {effectiveSteepest(run).toFixed(1)}°
                </span>
              </div>
            )
          })}
        </div>
      ))}
    </>
  )

  const tierFilterContent = (
    <div className="flex gap-1 items-center flex-wrap">
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
  )

  const settingsContent = (
    <div className="flex items-center gap-2">
      <div className="flex gap-0.5">
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
      <button
        onClick={() => { setShowLocation(p => !p); if (showLocation) setUserLocation(null) }}
        className={`p-1.5 rounded transition-colors ${showLocation ? "text-blue-500" : "text-gray-400 hover:text-gray-700"}`}
        title="My location"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <circle cx="8" cy="8" r="3" />
          <line x1="8" y1="1" x2="8" y2="4" />
          <line x1="8" y1="12" x2="8" y2="15" />
          <line x1="1" y1="8" x2="4" y2="8" />
          <line x1="12" y1="8" x2="15" y2="8" />
        </svg>
      </button>
    </div>
  )

  return (
    <div className="flex flex-col h-screen bg-white">
      {/* Header */}
      <header className="flex items-center gap-3 px-4 py-2 border-b border-gray-200 shrink-0 text-sm" style={{ paddingTop: "max(0.5rem, env(safe-area-inset-top))" }}>
        <img src="/icon-192.png" alt="SlopesDB" className="h-7 w-7 rounded-lg" />

        <select
          value={slug}
          onChange={e => setSlug(e.target.value)}
          className="px-2 py-0.5 border border-gray-200 rounded text-sm font-bold text-gray-800 focus:outline-none focus:border-gray-400"
        >
          {resorts.map(r => (
            <option key={r.slug} value={r.slug}>{r.name}</option>
          ))}
        </select>

        {!loading && <span className="hidden md:inline text-xs text-gray-400">{runs.length} runs</span>}

        {/* Desktop: tier filters */}
        <div className="hidden md:flex gap-1 items-center">
          {tierFilterContent}
        </div>

        {/* Desktop: compare + settings */}
        <div className="hidden md:flex ml-auto items-center gap-2">
          <Link href="/chart" className="p-1.5 rounded border border-gray-200 text-gray-400 hover:text-gray-700 transition-colors" title="Compare resorts">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <rect x="1" y="3" width="5" height="10" rx="0.5" />
              <rect x="10" y="3" width="5" height="10" rx="0.5" />
            </svg>
          </Link>
          <div className="flex items-center gap-2 border-l border-gray-200 pl-3">
            {settingsContent}
          </div>
        </div>

        {/* Mobile: icon buttons */}
        <div className="flex md:hidden ml-auto items-center gap-1">
          <button
            onClick={() => setMobilePanel(p => p === "list" ? null : "list")}
            className={`p-1.5 rounded transition-colors ${mobilePanel === "list" ? "bg-gray-800 text-white" : "text-gray-500"}`}
            title="Run list"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <line x1="2" y1="4" x2="14" y2="4" />
              <line x1="2" y1="8" x2="14" y2="8" />
              <line x1="2" y1="12" x2="14" y2="12" />
            </svg>
          </button>
          <button
            onClick={() => setMobilePanel(p => p === "filters" ? null : "filters")}
            className={`flex items-center gap-0.5 px-1.5 py-1.5 rounded transition-colors ${mobilePanel === "filters" ? "bg-gray-50" : ""}`}
            title="Difficulty filters"
          >
            {TIERS.map(t => {
              const visible = !hiddenTiers.has(t.label)
              const fill = visible ? t.color : "none"
              const stroke = visible ? t.color : "#bbb"
              const sw = visible ? 0 : 1
              if (t.label === "Expert") return (
                <svg key={t.label} width="15" height="8" viewBox="0 0 15 8">
                  <polygon points="4,0.5 7.5,4 4,7.5 0.5,4" fill={fill} stroke={stroke} strokeWidth={sw} />
                  <polygon points="11,0.5 14.5,4 11,7.5 7.5,4" fill={fill} stroke={stroke} strokeWidth={sw} />
                </svg>
              )
              if (t.label === "Advanced") return (
                <svg key={t.label} width="8" height="8" viewBox="0 0 8 8">
                  <polygon points="4,0.5 7.5,4 4,7.5 0.5,4" fill={fill} stroke={stroke} strokeWidth={sw} />
                </svg>
              )
              if (t.label === "Intermediate") return (
                <svg key={t.label} width="8" height="8" viewBox="0 0 8 8">
                  <rect x="0.5" y="0.5" width="7" height="7" fill={fill} stroke={stroke} strokeWidth={sw} />
                </svg>
              )
              return (
                <svg key={t.label} width="8" height="8" viewBox="0 0 8 8">
                  <circle cx="4" cy="4" r="3.5" fill={fill} stroke={stroke} strokeWidth={sw} />
                </svg>
              )
            })}
          </button>
          <Link href="/chart" className="p-1.5 rounded text-gray-400 hover:text-gray-700 transition-colors" title="Compare resorts">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <rect x="1" y="3" width="5" height="10" rx="0.5" />
              <rect x="10" y="3" width="5" height="10" rx="0.5" />
            </svg>
          </Link>
          <button
            onClick={() => setMobilePanel(p => p === "settings" ? null : "settings")}
            className={`p-1.5 rounded transition-colors ${mobilePanel === "settings" ? "bg-gray-800 text-white" : "text-gray-500"}`}
            title="Settings"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <line x1="2" y1="5" x2="14" y2="5" />
              <circle cx="5.5" cy="5" r="2" fill="white" strokeWidth="1.5" />
              <line x1="2" y1="11" x2="14" y2="11" />
              <circle cx="10.5" cy="11" r="2" fill="white" strokeWidth="1.5" />
            </svg>
          </button>
        </div>
      </header>

      {/* Mobile: backdrop */}
      {mobilePanel && (
        <div
          className="fixed inset-0 z-[1999] bg-black/30 md:hidden"
          onClick={() => setMobilePanel(null)}
        />
      )}

      {/* Mobile: list panel (slides in from left) */}
      {mobilePanel === "list" && (
        <div className="fixed inset-y-0 left-0 w-72 z-[2000] bg-white shadow-xl overflow-y-auto text-xs md:hidden" style={{ paddingTop: "max(0px, env(safe-area-inset-top))" }}>
          {runListContent}
        </div>
      )}

      {/* Mobile: filters panel (bottom sheet) */}
      {mobilePanel === "filters" && (
        <div className="fixed bottom-0 left-0 right-0 z-[2000] bg-white shadow-xl px-4 pt-3 pb-8 md:hidden">
          <div className="text-xs font-semibold text-gray-500 mb-3">Difficulty</div>
          {tierFilterContent}
        </div>
      )}

      {/* Mobile: settings panel (bottom sheet) */}
      {mobilePanel === "settings" && (
        <div className="fixed bottom-0 left-0 right-0 z-[2000] bg-white shadow-xl px-4 pt-3 pb-8 md:hidden">
          <div className="text-xs font-semibold text-gray-500 mb-3">Settings</div>
          {settingsContent}
        </div>
      )}

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Run list — desktop only */}
        <div className="hidden md:block w-60 shrink-0 border-r border-gray-100 overflow-y-auto text-xs">
          {runListContent}
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
            useFace={useFace}
            chartHoverCoord={chartHoverCoord}
            bearing={bearing}
            userLocation={userLocation}
          />
          {/* Slope profile chart overlay */}
          {effectivePin && mounted && (() => {
            const segments   = runs.filter(r => r.name === effectivePin)
            const profile    = buildProfile(segments, useFace)
            const run0       = segments[0]
            const tier       = tierFor(run0 ? effectiveSteepest(run0) : 0)
            const totalKm    = profile.length > 0 ? profile[profile.length - 1].dist : 0
            const lineDelta  = run0 ? effectiveSteepest(run0) - run0.steepest : null
            return (
              <div className="absolute bottom-0 left-0 right-0 bg-white/95 border-t border-gray-200 z-[1000] px-4 pt-2 pb-1" style={{ backdropFilter: "blur(4px)" }}>
                <div className="flex items-baseline gap-2">
                  {run0?.osm_difficulty && <PisteBadge difficulty={run0.osm_difficulty} />}
                  <span className="text-xs font-bold text-gray-800">{effectivePin}</span>
                  <span className="text-xs font-bold" style={{ color: tier.color }}>{run0 ? effectiveSteepest(run0).toFixed(1) : ""}°</span>
                  {run0 && lineDelta != null && lineDelta >= 1 && (
                    <span className="text-xs text-gray-400">
                      line {run0.steepest.toFixed(1)}°
                    </span>
                  )}
                  <span className="text-xs text-gray-400">{totalKm.toFixed(2)} km</span>
                </div>
                <ResponsiveContainer width="100%" height={70}>
                  <AreaChart
                    data={profile}
                    margin={{ top: 2, right: 4, bottom: 0, left: 0 }}
                    onMouseMove={(state: any) => {
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
