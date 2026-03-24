"use client"

import { useEffect, useRef, useState } from "react"
import dynamic from "next/dynamic"
import Link from "next/link"
import { TIERS, tierFor } from "@/lib/types"
import type { RunGeo, LiftGeo } from "@/components/MapView"

const MapView = dynamic(() => import("@/components/MapView"), { ssr: false })

interface ResortMeta { name: string; slug: string; color: string }

interface GeoJSON {
  resort: string
  color: string
  features: {
    geometry: { coordinates: [number, number][] }
    properties: { name: string; steepest: number; osm_id?: number; slopes: number[] }
  }[]
}

export default function MapPage() {
  const [resorts, setResorts]   = useState<ResortMeta[]>([])
  const [slug, setSlug]         = useState("palisades_tahoe")
  const [runs, setRuns]         = useState<RunGeo[]>([])
  const [lifts, setLifts]       = useState<LiftGeo[]>([])
  const [loading, setLoading]   = useState(true)
  const [hovered, setHovered]   = useState<string | null>(null)
  const listRef                 = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetch("/data/index.json").then(r => r.json()).then((data: ResortMeta[]) => {
      setResorts(data)
    })
  }, [])

  useEffect(() => {
    setLoading(true)
    setRuns([])
    setLifts([])
    Promise.all([
      fetch(`/data/${slug}_geo.json`).then(r => r.json()),
      fetch(`/data/${slug}_lifts.json`).then(r => r.json()).catch(() => ({ features: [] })),
    ]).then(([geo, liftsJson]) => {
      const parsed: RunGeo[] = (geo as GeoJSON).features.map(f => ({
        name:        f.properties.name,
        steepest:    f.properties.steepest,
        osm_id:      f.properties.osm_id,
        slopes:      f.properties.slopes,
        coordinates: f.geometry.coordinates,
      }))
      parsed.sort((a, b) => b.steepest - a.steepest)
      setRuns(parsed)
      setLifts(liftsJson.features.map((f: { geometry: { coordinates: [number,number][] }, properties: { name: string, type: string } }) => ({
        name:        f.properties.name,
        type:        f.properties.type,
        coordinates: f.geometry.coordinates,
      })))
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [slug])

  // Scroll hovered run to the top of the list
  useEffect(() => {
    if (!hovered || !listRef.current) return
    const el = listRef.current.querySelector(`[data-run="${CSS.escape(hovered)}"]`)
    if (!el) return
    const container = listRef.current
    const elTop = (el as HTMLElement).getBoundingClientRect().top - container.getBoundingClientRect().top
    container.scrollTop += elTop
  }, [hovered])

  // Group runs by tier for the sidebar
  const grouped = TIERS.map(tier => ({
    tier,
    runs: runs.filter(r => tierFor(r.steepest).label === tier.label),
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

        <div className="ml-auto flex gap-3 text-xs text-gray-400">
          {TIERS.map(t => (
            <span key={t.label} className="flex items-center gap-1">
              <span className="inline-block w-3 h-1 rounded" style={{ background: t.color }} />
              {t.label} ≥{t.min > 0 ? ` ${t.min}°` : " 0°"}
            </span>
          ))}
        </div>
      </header>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Run list */}
        <div ref={listRef} className="w-60 shrink-0 border-r border-gray-100 overflow-y-auto text-xs">
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
              {tierRuns.map(run => {
                const isHovered = hovered === run.name
                return (
                  <div
                    key={run.name}
                    data-run={run.name}
                    className={`flex items-center gap-2 px-3 cursor-default transition-all ${
                      isHovered ? "py-2.5" : "hover:bg-gray-50 py-1.5"
                    }`}
                    style={isHovered ? { background: "#fef9c3" } : undefined}
                    onMouseEnter={() => setHovered(run.name)}
                    onMouseLeave={() => setHovered(null)}
                  >
                    <span className={`flex-1 truncate text-gray-700 transition-all ${isHovered ? "font-bold text-sm" : ""}`}>
                      {run.name}
                    </span>
                    <span className={`shrink-0 font-medium tabular-nums transition-all ${isHovered ? "text-sm" : ""}`} style={{ color: tier.color }}>
                      {run.steepest.toFixed(1)}°
                    </span>
                  </div>
                )
              })}
            </div>
          ))}
        </div>

        {/* Map */}
        <div className="flex-1 relative">
          {loading ? (
            <div className="flex items-center justify-center h-full text-gray-400 text-sm">Loading…</div>
          ) : (
            <MapView runs={runs} lifts={lifts} hovered={hovered} onHover={setHovered} />
          )}
        </div>
      </div>
    </div>
  )
}
