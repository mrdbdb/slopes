"use client"

import { useEffect, useState } from "react"
import { MapContainer, TileLayer, Polyline, Polygon, Marker, CircleMarker, useMap, useMapEvents } from "react-leaflet"
import type { LatLngBoundsExpression } from "leaflet"
import L from "leaflet"
import "leaflet/dist/leaflet.css"
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore – no types for leaflet-rotate
import "leaflet-rotate"
import { TIERS, tierFor } from "@/lib/types"

declare module "leaflet" {
  interface Map { setBearing(deg: number): this }
  interface MapOptions { rotate?: boolean; bearing?: number }
}

export interface RunGeo {
  name: string
  steepest: number
  face_steepest?: number
  is_traverse?: boolean
  osm_id?: number
  slopes: number[]      // face steepness per segment
  line_slopes?: number[] // directional steepness per segment
  is_area?: boolean
  coordinates: [number, number][] // [lon, lat]
}

export interface LiftGeo {
  name: string
  type: string
  coordinates: [number, number][] // [lon, lat]
}

interface Props {
  runs: RunGeo[]
  lifts: LiftGeo[]
  hovered: string | null
  pinned: string | null
  onHover: (name: string | null) => void
  onRunClick: (name: string) => void
  focusRun: string | null
  hiddenTiers?: Set<string>
  dimmedRuns?: Set<string>
  useFace?: boolean
  chartHoverCoord?: [number, number] | null  // [lon, lat]
}

function slopeColor(deg: number): string {
  for (const tier of TIERS) {
    if (deg >= tier.min) return tier.color
  }
  return TIERS[TIERS.length - 1].color
}

function buildGroups(coordinates: [number, number][], slopes: number[]) {
  const groups: { color: string; positions: [number, number][] }[] = []
  let cur: { color: string; positions: [number, number][] } | null = null
  for (let i = 0; i < slopes.length; i++) {
    const color = slopeColor(slopes[i])
    const p1: [number, number] = [coordinates[i][1],     coordinates[i][0]]
    const p2: [number, number] = [coordinates[i + 1][1], coordinates[i + 1][0]]
    if (!cur || cur.color !== color) {
      if (cur) groups.push(cur)
      cur = { color, positions: [p1, p2] }
    } else {
      cur.positions.push(p2)
    }
  }
  if (cur) groups.push(cur)
  return groups
}

// Normalize a raw CSS angle to ±90° so text is never upside-down
function normalizeAngle(css: number): number {
  css = ((css % 360) + 360) % 360   // → [0, 360)
  if (css > 180) css -= 360          // → (-180, 180]
  if (css >  90) css -= 180          // flip upside-down text
  if (css < -90) css += 180
  return css
}

// CSS rotation angle so text aligns with the line direction, accounting for map bearing
// (leaflet-rotate keeps markers upright, so angles are in screen-space = geo - mapBearing)
function liftAngleDeg(coords: [number, number][], mapBearing: number): number {
  const n = coords.length
  const [lon1, lat1] = coords[Math.max(0, Math.floor(n * 0.25))]
  const [lon2, lat2] = coords[Math.min(n - 1, Math.floor(n * 0.75))]
  return normalizeAngle(Math.atan2(lon2 - lon1, lat2 - lat1) * 180 / Math.PI - 90 - mapBearing)
}

function liftMid(coords: [number, number][]): [number, number] {
  const [lon, lat] = coords[Math.floor(coords.length / 2)]
  return [lat, lon]
}

function makeLiftIcon(name: string, angle: number) {
  return L.divIcon({
    className: "",
    html: `<div style="transform:translate(-50%,-50%) rotate(${angle}deg);white-space:nowrap;font-size:10px;font-style:italic;font-weight:600;color:#334155;text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;pointer-events:none;letter-spacing:0.02em">${name}</div>`,
    iconSize:   [0, 0],
    iconAnchor: [0, 0],
  })
}

function makeRunLabelIcon(name: string, angle: number) {
  return L.divIcon({
    className: "",
    html: `<div style="transform:translate(-50%,-50%) rotate(${angle}deg);white-space:nowrap;font-size:11px;font-weight:700;color:#1e293b;text-shadow:0 0 4px #fff,0 0 4px #fff,0 0 4px #fff,0 0 4px #fff;pointer-events:none;letter-spacing:0.02em">${name}</div>`,
    iconSize:   [0, 0],
    iconAnchor: [0, 0],
  })
}

const LABEL_MIN_ZOOM = 13
const RUN_LABEL_MIN_ZOOM = 16

function LiftLabels({ lifts, bearing }: { lifts: LiftGeo[]; bearing: number }) {
  const map = useMap()
  const [zoom, setZoom] = useState(map.getZoom())
  useMapEvents({ zoom: () => setZoom(map.getZoom()) })
  if (zoom < LABEL_MIN_ZOOM) return null
  return (
    <>
      {lifts.map((lift, i) => (
        <Marker
          key={`${lift.name}-${i}`}
          position={liftMid(lift.coordinates)}
          icon={makeLiftIcon(lift.name, liftAngleDeg(lift.coordinates, bearing))}
          interactive={false}
        />
      ))}
    </>
  )
}

function runMid(coords: [number, number][]): [number, number] {
  const [lon, lat] = coords[Math.floor(coords.length / 2)]
  return [lat, lon]
}

function areaCentroid(coords: [number, number][]): [number, number] {
  const lat = coords.reduce((s, c) => s + c[1], 0) / coords.length
  const lon = coords.reduce((s, c) => s + c[0], 0) / coords.length
  return [lat, lon]
}

function runAngleDeg(coords: [number, number][], mapBearing: number): number {
  const n = coords.length
  const [lon1, lat1] = coords[Math.max(0, Math.floor(n * 0.25))]
  const [lon2, lat2] = coords[Math.min(n - 1, Math.floor(n * 0.75))]
  return normalizeAngle(Math.atan2(lon2 - lon1, lat2 - lat1) * 180 / Math.PI - 90 - mapBearing)
}

function RunLabels({ runs, hovered, pinned, bearing, hiddenTiers }: { runs: RunGeo[]; hovered: string | null; pinned: string | null; bearing: number; hiddenTiers?: Set<string> }) {
  const map = useMap()
  const [zoom, setZoom] = useState(map.getZoom())
  useMapEvents({ zoom: () => setZoom(map.getZoom()) })
  if (zoom < RUN_LABEL_MIN_ZOOM) return null
  return (
    <>
      {runs.map(run => {
        if (run.is_area) return null
        if (run.name === hovered || run.name === pinned) return null
        if (hiddenTiers?.has(tierFor(run.steepest).label)) return null
        const position = runMid(run.coordinates)
        const angle    = runAngleDeg(run.coordinates, bearing)
        return (
          <Marker
            key={`all-label-${run.name}`}
            position={position}
            icon={makeRunLabelIcon(run.name, angle)}
            interactive={false}
          />
        )
      })}
    </>
  )
}

function FocusRun({ runs, focusRun }: { runs: RunGeo[]; focusRun: string | null }) {
  const map = useMap()
  useEffect(() => {
    if (!focusRun) return
    const segments = runs.filter(r => r.name === focusRun)
    if (segments.length === 0) return
    const all = segments.flatMap(r => r.coordinates)
    const lats = all.map(([, lat]) => lat)
    const lons = all.map(([lon]) => lon)
    const bounds = L.latLngBounds(
      [Math.min(...lats), Math.min(...lons)],
      [Math.max(...lats), Math.max(...lons)]
    )
    if (map.getBounds().contains(bounds)) return
    map.fitBounds(bounds, { maxZoom: map.getZoom(), padding: [32, 32] })
  }, [focusRun, runs, map])
  return null
}


function SetBearing({ bearing }: { bearing: number }) {
  const map = useMap()
  useEffect(() => { map.setBearing(bearing) }, [bearing, map])
  return null
}

function FitBounds({ runs }: { runs: RunGeo[] }) {
  const map = useMap()
  useEffect(() => {
    const all = runs.flatMap(r => r.coordinates)
    if (all.length === 0) return
    const lats = all.map(([, lat]) => lat)
    const lons = all.map(([lon]) => lon)
    const bounds: LatLngBoundsExpression = [
      [Math.min(...lats), Math.min(...lons)],
      [Math.max(...lats), Math.max(...lons)],
    ]
    map.fitBounds(bounds, { padding: [24, 24] })
  }, [runs, map])
  return null
}

const DIM_COLOR = "#d1d5db"

export default function MapView({ runs, lifts, hovered, pinned, onHover, onRunClick, focusRun, hiddenTiers, dimmedRuns, useFace = true, chartHoverCoord }: Props) {
  // Deduplicate by name for label placement (pick longest segment)
  const labelRuns = Array.from(
    runs.reduce((map, r) => {
      const existing = map.get(r.name)
      if (!existing || r.coordinates.length > existing.coordinates.length) map.set(r.name, r)
      return map
    }, new Map<string, RunGeo>()).values()
  )

  const bearing = 180

  return (
    <MapContainer
      center={[39.25, -120.2]}
      zoom={13}
      style={{ width: "100%", height: "100%" }}
      rotate={true}
      bearing={bearing}
    >
      <TileLayer
        url="https://tile.opentopomap.org/{z}/{x}/{y}.png"
        attribution='Map data: &copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM | Style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)'
        maxZoom={17}
        opacity={0.25}
      />
      <SetBearing bearing={bearing} />
      <FitBounds runs={runs} />
      <FocusRun runs={runs} focusRun={focusRun} />

      {/* Lifts — dashed lines */}
      {lifts.map((lift, i) => (
        <Polyline
          key={`${lift.name}-${i}`}
          positions={lift.coordinates.map(([lon, lat]) => [lat, lon] as [number, number])}
          pathOptions={{ color: "#1e293b", weight: 2, opacity: 0.65, dashArray: "6 4" }}
        />
      ))}

      {/* Lift labels — only when zoomed in enough */}
      <LiftLabels lifts={lifts} bearing={bearing} />

      {/* Run labels for all runs — only when zoomed in enough */}
      <RunLabels runs={labelRuns} hovered={hovered} pinned={pinned} bearing={bearing} hiddenTiers={hiddenTiers} />

      {/* Area runs — rendered first so line runs appear on top */}
      {runs.filter(r => r.is_area).map(run => {
        const isPinned  = pinned  === run.name
        const isHovered = hovered === run.name
        const isDimmed  = hiddenTiers?.has(tierFor(run.steepest).label) ?? false
        const color = isDimmed ? DIM_COLOR : slopeColor(run.steepest)
        const positions: [number, number][] = run.coordinates.map(([lon, lat]) => [lat, lon])
        return (
          <Polygon
            key={run.name}
            positions={positions}
            pathOptions={{
              color,
              weight:      isPinned ? 2     : isHovered ? 1.5 : 1,
              opacity:     isDimmed ? 0.25  : isPinned ? 0.6  : isHovered ? 0.45 : 0.3,
              fillColor:   color,
              fillOpacity: isDimmed ? 0.1   : isPinned ? 0.4  : isHovered ? 0.3  : 0.2,
            }}
            eventHandlers={{
              mouseover: () => onHover(run.name),
              mouseout:  () => onHover(null),
              click:     () => onRunClick(run.name),
            }}
          />
        )
      })}

      {/* Dimmed line runs — tier-hidden or delta-filtered, rendered before active */}
      {runs.filter(r => !r.is_area && (
        (hiddenTiers?.has(tierFor(r.steepest).label) ?? false) || (dimmedRuns?.has(r.name) ?? false)
      )).map((run, di) => {
        const positions: [number, number][] = run.coordinates.map(([lon, lat]) => [lat, lon])
        return (
          <Polyline
            key={`${run.name}-dim-${di}`}
            positions={positions}
            pathOptions={{ color: DIM_COLOR, weight: 3, opacity: 0.25 }}
            eventHandlers={{
              mouseover: () => onHover(run.name),
              mouseout:  () => onHover(null),
              click:     () => onRunClick(run.name),
            }}
          />
        )
      })}

      {/* Active line runs — rendered after dimmed so they appear on top */}
      {runs.filter(r => !r.is_area && !(hiddenTiers?.has(tierFor(r.steepest).label) ?? false) && !(dimmedRuns?.has(r.name) ?? false)).map(run => {
        const isPinned  = pinned  === run.name
        const isHovered = hovered === run.name
        const slopes    = useFace ? run.slopes : (run.line_slopes ?? run.slopes)
        return buildGroups(run.coordinates, slopes).map((g, gi) => (
          <Polyline
            key={`${run.name}-${gi}`}
            positions={g.positions}
            pathOptions={{
              color:   g.color,
              weight:  isPinned ? 8 : isHovered ? 6.5 : 4.5,
              opacity: isPinned ? 1 : isHovered ? 0.95 : 0.85,
            }}
            eventHandlers={{
              mouseover: () => onHover(run.name),
              mouseout:  () => onHover(null),
              click:     () => onRunClick(run.name),
            }}
          />
        ))
      })}

      {/* Run name label for hovered or pinned run (not dimmed) */}
      {labelRuns
        .filter(r => (r.name === hovered || r.name === pinned) && !(hiddenTiers?.has(tierFor(r.steepest).label)))
        .map(run => (
          <Marker
            key={`label-${run.name}`}
            position={run.is_area ? areaCentroid(run.coordinates) : runMid(run.coordinates)}
            icon={makeRunLabelIcon(run.name, run.is_area ? 0 : runAngleDeg(run.coordinates, bearing))}
            interactive={false}
          />
        ))
      }

      {/* Chart hover position marker */}
      {chartHoverCoord && (
        <CircleMarker
          center={[chartHoverCoord[1], chartHoverCoord[0]]}
          radius={6}
          pathOptions={{ color: "#fff", weight: 2, fillColor: "#1e293b", fillOpacity: 1 }}
          interactive={false}
        />
      )}
    </MapContainer>
  )
}
