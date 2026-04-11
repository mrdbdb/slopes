"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { ResortData, RunProfile, TIERS, tierFor, effectiveSteepest } from "@/lib/types"
import { fetchData } from "@/lib/dataFetch"
import RunRow from "@/components/RunRow"

interface ResortMeta { name: string; slug: string; color: string; region?: string }

const REGION_ORDER = ["California", "Canada", "Colorado", "Japan", "Switzerland"]

function groupByRegion(resorts: ResortMeta[]): [string, ResortMeta[]][] {
  const groups = new Map<string, ResortMeta[]>()
  for (const r of resorts) {
    const region = r.region ?? "Other"
    if (!groups.has(region)) groups.set(region, [])
    groups.get(region)!.push(r)
  }
  const ordered: [string, ResortMeta[]][] = []
  for (const region of REGION_ORDER) {
    const items = groups.get(region)
    if (items) ordered.push([region, items])
  }
  for (const [region, items] of groups) {
    if (!REGION_ORDER.includes(region)) ordered.push([region, items])
  }
  return ordered
}

function groupByTierAndDeg(data: ResortData): Record<string, Record<number, RunProfile[]>> {
  const groups: Record<string, Record<number, RunProfile[]>> = {}
  for (const t of TIERS) groups[t.label] = {}
  for (const run of data.runs) {
    if (run === null) continue
    const tier = tierFor(effectiveSteepest(run)).label
    const deg = Math.floor(effectiveSteepest(run))
    if (!groups[tier][deg]) groups[tier][deg] = []
    groups[tier][deg].push(run)
  }
  return groups
}

function useResortData(slug: string, smoothing: number) {
  const [data, setData] = useState<ResortData | null>(null)
  useEffect(() => {
    if (!slug) return
    setData(null)
    fetchData(`/data/${slug}_s${smoothing}.json`)
      .then(r => r.json())
      .then(setData)
      .catch(console.error)
  }, [slug, smoothing])
  return data
}

export default function Home() {
  const [allResorts, setAllResorts]   = useState<ResortMeta[]>([])
  const [leftSlug, setLeftSlug]       = useState("palisades_tahoe")
  const [rightSlug, setRightSlug]     = useState("northstar")
  const [hiddenTiers, setHiddenTiers] = useState<Set<string>>(new Set())
  const [maxLengthInput, setMaxLengthInput] = useState("")
  const [smoothing, setSmoothing]     = useState(30)
  const [highlighted, setHighlighted] = useState<string | null>(null)
  const [clicked, setClicked]         = useState<string | null>(null)
  const [mounted, setMounted]         = useState(false)

  useEffect(() => {
    setMounted(true)
    fetchData("/data/index.json").then(r => r.json()).then(setAllResorts).catch(console.error)
    try {
      const prefs = JSON.parse(localStorage.getItem("ski-prefs") ?? "{}")
      if (prefs.hiddenTiers?.length)  setHiddenTiers(new Set(prefs.hiddenTiers))
      if (prefs.maxLengthInput)       setMaxLengthInput(prefs.maxLengthInput)
      if ([2, 10, 30].includes(prefs.smoothing)) setSmoothing(prefs.smoothing)
      if (prefs.leftSlug)             setLeftSlug(prefs.leftSlug)
      if (prefs.rightSlug)            setRightSlug(prefs.rightSlug)
    } catch {}
  }, [])

  useEffect(() => {
    if (!mounted) return
    try {
      localStorage.setItem("ski-prefs", JSON.stringify({
        hiddenTiers: Array.from(hiddenTiers),
        maxLengthInput,
        smoothing,
        leftSlug,
        rightSlug,
      }))
    } catch {}
  }, [hiddenTiers, maxLengthInput, smoothing, leftSlug, rightSlug, mounted])

  const leftData  = useResortData(leftSlug,  smoothing)
  const rightData = useResortData(rightSlug, smoothing)

  const maxLengthKm = maxLengthInput ? parseFloat(maxLengthInput) : null

  const maxDist = (leftData || rightData)
    ? Math.min(
        maxLengthKm ?? Infinity,
        Math.max(
          ...[leftData, rightData].flatMap(d =>
            d ? d.runs.filter(Boolean).map(r => (r as RunProfile).length_km) : [0]
          )
        )
      )
    : 1

  const activeHighlight = highlighted ?? clicked

  function handleClick(name: string) {
    setClicked(prev => prev === name ? null : name)
  }

  function toggleTier(label: string) {
    setHiddenTiers(prev => {
      const next = new Set(prev)
      if (next.has(label)) next.delete(label)
      else next.add(label)
      return next
    })
  }

  function truncateRun(run: RunProfile): RunProfile {
    if (!maxLengthKm || run.length_km <= maxLengthKm) return run
    return {
      ...run,
      length_km: maxLengthKm,
      profile: run.profile.filter(([d]) => d <= maxLengthKm),
    }
  }

  const tiersToShow = TIERS.filter(t => !hiddenTiers.has(t.label))

  const columns = [
    { slug: leftSlug,  setSlug: setLeftSlug,  data: leftData  },
    { slug: rightSlug, setSlug: setRightSlug, data: rightData },
  ]

  const byTier = columns.map(c => c.data ? groupByTierAndDeg(c.data) : null)

  return (
    <div className="flex flex-col h-screen bg-white">
      {/* Top bar */}
      <header className="flex items-center gap-3 px-4 py-2 border-b border-gray-200 shrink-0" style={{ paddingTop: "max(0.5rem, env(safe-area-inset-top))" }}>
        <h1 className="text-sm font-bold text-gray-800">Ski Run Comparison</h1>
        <Link href="/" className="text-xs text-gray-400 hover:text-gray-700 border border-gray-200 rounded px-2 py-0.5">Map →</Link>

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

        <div className="flex items-center gap-1.5 text-xs text-gray-500">
          <label htmlFor="smoothing" className="whitespace-nowrap">Smoothing</label>
          <select
            id="smoothing"
            value={smoothing}
            onChange={e => setSmoothing(Number(e.target.value))}
            className="px-1.5 py-0.5 border border-gray-200 rounded text-xs text-gray-700 focus:outline-none focus:border-gray-400"
          >
            <option value={2}>2m (raw)</option>
            <option value={10}>10m</option>
            <option value={30}>30m (SteepSeeker)</option>
          </select>
        </div>

        <div className="flex items-center gap-1.5 text-xs text-gray-500">
          <label htmlFor="maxlen" className="whitespace-nowrap">Max length</label>
          <input
            id="maxlen"
            type="number"
            min="0"
            step="0.5"
            placeholder="km"
            value={maxLengthInput}
            onChange={e => setMaxLengthInput(e.target.value)}
            className="w-16 px-1.5 py-0.5 border border-gray-200 rounded text-xs text-gray-700 focus:outline-none focus:border-gray-400"
          />
          <span className="text-gray-400">km</span>
        </div>

        <div className="ml-auto flex gap-4 text-xs text-gray-400">
          <span>← longer runs use more chart width</span>
        </div>
      </header>

      {/* Column headers with resort dropdowns */}
      <div className="flex border-b border-gray-200 shrink-0">
        {columns.map(({ slug, setSlug, data }) => (
          <div key={slug} className="flex-1 flex items-baseline gap-2 px-2 py-1.5">
            <select
              value={slug}
              onChange={e => setSlug(e.target.value)}
              className="text-sm font-bold text-gray-800 bg-transparent border-none outline-none cursor-pointer"
            >
              {groupByRegion(allResorts).map(([region, items]) => (
                <optgroup key={region} label={region}>
                  {items.map(r => (
                    <option key={r.slug} value={r.slug}>{r.name}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            {data && (
              <span className="text-xs text-gray-400">
                {data.runs.filter(Boolean).length} runs
              </span>
            )}
          </div>
        ))}
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto">
        {(!leftData || !rightData) && (
          <div className="flex items-center justify-center h-32 text-gray-400 text-sm">
            Loading…
          </div>
        )}
        {leftData && rightData && tiersToShow.map(tier => {
          const degMaps = byTier.map(bt => bt?.[tier.label] ?? {})
          const allDegs = Array.from(new Set(
            degMaps.flatMap(m => Object.keys(m).map(Number))
          )).sort((a, b) => b - a)
          if (allDegs.length === 0) return null

          const totals = degMaps.map(m => Object.values(m).flat().length)

          return (
            <div key={tier.label}>
              <div
                className="flex items-center gap-2 px-3 py-1 sticky top-0 z-10 border-y border-dashed text-xs font-semibold"
                style={{
                  color: tier.color,
                  borderColor: tier.color + "44",
                  background: tier.color + "0d",
                }}
              >
                <span>{tier.label}</span>
                <span className="font-normal text-gray-400">
                  {tier.min > 0 ? `≥ ${tier.min}°` : `< ${TIERS[TIERS.length - 2].min}°`}
                  {" · "}
                  {columns.map((c, i) => `${totals[i]} ${allResorts.find(r => r.slug === c.slug)?.name ?? c.slug}`).join(" · ")}
                </span>
              </div>

              {allDegs.map(deg => (
                <div key={deg} className="flex divide-x divide-gray-100">
                  {columns.map((col, i) => {
                    const runs = degMaps[i][deg] ?? []
                    const resort = col.data!
                    return (
                      <div key={col.slug} className="flex-1 min-w-0">
                        {runs.map(run => (
                          <RunRow
                            key={`${run.osm_id ?? run.name}-${run.steepest}-${run.name}`}
                            run={truncateRun(run)}
                            accentColor={resort.color}
                            highlighted={activeHighlight === run.name}
                            onHover={setHighlighted}
                            onClick={handleClick}
                            maxDist={maxDist}
                            mounted={mounted}
                          />
                        ))}
                        {runs.length === 0 && <div style={{ height: 41 }} />}
                      </div>
                    )
                  })}
                </div>
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}
