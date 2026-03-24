"use client"

import { useEffect, useState } from "react"
import { ResortData, RunProfile, TIERS, tierFor } from "@/lib/types"
import RunRow from "@/components/RunRow"

function groupByTierAndDeg(data: ResortData): Record<string, Record<number, RunProfile[]>> {
  const groups: Record<string, Record<number, RunProfile[]>> = {}
  for (const t of TIERS) groups[t.label] = {}
  for (const run of data.runs) {
    if (run === null) continue
    const tier = tierFor(run.steepest).label
    const deg = Math.floor(run.steepest)
    if (!groups[tier][deg]) groups[tier][deg] = []
    groups[tier][deg].push(run)
  }
  return groups
}

export default function Home() {
  const [resorts, setResorts]         = useState<ResortData[]>([])
  const [hiddenTiers, setHiddenTiers] = useState<Set<string>>(new Set())
  const [maxLengthInput, setMaxLengthInput] = useState("")
  const [smoothing, setSmoothing]     = useState(3)
  const [highlighted, setHighlighted] = useState<string | null>(null)
  const [clicked, setClicked]         = useState<string | null>(null)
  const [mounted, setMounted]         = useState(false)

  useEffect(() => {
    setMounted(true)
    try {
      const prefs = JSON.parse(localStorage.getItem("ski-prefs") ?? "{}")
      if (prefs.hiddenTiers?.length)  setHiddenTiers(new Set(prefs.hiddenTiers))
      if (prefs.maxLengthInput)       setMaxLengthInput(prefs.maxLengthInput)
      if (prefs.smoothing)            setSmoothing(prefs.smoothing)
    } catch {}
  }, [])

  useEffect(() => {
    if (!mounted) return
    try {
      localStorage.setItem("ski-prefs", JSON.stringify({
        hiddenTiers:    Array.from(hiddenTiers),
        maxLengthInput,
        smoothing,
      }))
    } catch {}
  }, [hiddenTiers, maxLengthInput, smoothing, mounted])

  useEffect(() => {
    setResorts([])
    Promise.all([
      fetch(`/data/palisades_tahoe_s${smoothing}.json`).then(r => r.json()),
      fetch(`/data/northstar_s${smoothing}.json`).then(r => r.json()),
    ]).then(setResorts).catch(console.error)
  }, [smoothing])

  const maxLengthKm = maxLengthInput ? parseFloat(maxLengthInput) : null

  const maxDist = resorts.length
    ? Math.min(
        maxLengthKm ?? Infinity,
        Math.max(...resorts.flatMap(r =>
          r.runs.filter(Boolean).map(run => (run as RunProfile).length_km)
        ))
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

  if (!resorts.length) {
    return (
      <div className="flex items-center justify-center h-screen text-gray-400 text-sm">
        Loading…
      </div>
    )
  }

  const [palisades, northstar] = resorts
  const palByTier = groupByTierAndDeg(palisades)
  const norByTier = groupByTierAndDeg(northstar)

  const tiersToShow = TIERS.filter(t => !hiddenTiers.has(t.label))

  return (
    <div className="flex flex-col h-screen bg-white">
      {/* Top bar */}
      <header className="flex items-center gap-3 px-4 py-2 border-b border-gray-200 shrink-0">
        <h1 className="text-sm font-bold text-gray-800">Ski Run Comparison</h1>

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
            <option value={1}>1 — raw</option>
            <option value={2}>2 — 20m</option>
            <option value={3}>3 — 30m (SteepSeeker)</option>
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

      {/* Column headers */}
      <div className="flex border-b border-gray-200 shrink-0">
        {[palisades, northstar].map(resort => (
          <div key={resort.name} className="flex-1 flex items-baseline gap-2 px-2 py-1.5">
            <span className="text-sm font-bold text-gray-800">{resort.name}</span>
            <span className="text-xs text-gray-400">
              {resort.runs.filter(Boolean).length} runs
            </span>
          </div>
        ))}
      </div>

      {/* Scrollable body */}
      <div className="flex-1 overflow-y-auto">
        {tiersToShow.map(tier => {
          const palDegMap = palByTier[tier.label] ?? {}
          const norDegMap = norByTier[tier.label] ?? {}
          const allDegs = Array.from(new Set([
            ...Object.keys(palDegMap).map(Number),
            ...Object.keys(norDegMap).map(Number),
          ])).sort((a, b) => b - a)
          if (allDegs.length === 0) return null

          const palTotal = Object.values(palDegMap).flat().length
          const norTotal = Object.values(norDegMap).flat().length

          return (
            <div key={tier.label}>
              {/* Tier header — spans both columns, always aligned */}
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
                  {palTotal} Palisades · {norTotal} Northstar
                </span>
              </div>

              {/* Degree-aligned rows: same floor(steepest) renders side by side */}
              {allDegs.map(deg => {
                const palRuns = palDegMap[deg] ?? []
                const norRuns = norDegMap[deg] ?? []
                return (
                  <div key={deg} className="flex divide-x divide-gray-100">
                    {[
                      { resort: palisades, runs: palRuns },
                      { resort: northstar, runs: norRuns },
                    ].map(({ resort, runs }) => (
                      <div key={resort.name} className="flex-1 min-w-0">
                        {runs.map(run => (
                          <RunRow
                            key={run.name}
                            run={truncateRun(run)}
                            accentColor={resort.color}
                            highlighted={activeHighlight === run.name}
                            onHover={setHighlighted}
                            onClick={handleClick}
                            maxDist={maxDist}
                            mounted={mounted}
                          />
                        ))}
                        {runs.length === 0 && (
                          <div style={{ height: 41 }} />
                        )}
                      </div>
                    ))}
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>
    </div>
  )
}
