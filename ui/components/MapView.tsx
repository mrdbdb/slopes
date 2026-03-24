"use client"

import { useEffect, useState } from "react"
import { MapContainer, TileLayer, Polyline, Marker, useMap, useMapEvents } from "react-leaflet"
import type { LatLngBoundsExpression } from "leaflet"
import L from "leaflet"
import "leaflet/dist/leaflet.css"
import { TIERS } from "@/lib/types"

export interface RunGeo {
  name: string
  steepest: number
  osm_id?: number
  slopes: number[]
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
  onHover: (name: string | null) => void
}

function slopeColor(deg: number): string {
  if (deg < 10) return "#c4b5fd"
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

// CSS rotation angle so text aligns with the lift line direction
function liftAngleDeg(coords: [number, number][]): number {
  const n = coords.length
  const [lon1, lat1] = coords[Math.max(0, Math.floor(n * 0.25))]
  const [lon2, lat2] = coords[Math.min(n - 1, Math.floor(n * 0.75))]
  // geographic bearing from north → CSS rotation (0° = east/horizontal)
  let css = Math.atan2(lon2 - lon1, lat2 - lat1) * 180 / Math.PI - 90
  // keep text upright
  if (css > 90)  css -= 180
  if (css < -90) css += 180
  return css
}

function liftMid(coords: [number, number][]): [number, number] {
  const [lon, lat] = coords[Math.floor(coords.length / 2)]
  return [lat, lon]
}

function makeLiftIcon(name: string, angle: number) {
  return L.divIcon({
    className: "",
    html: `<div style="transform:translate(-50%,-50%) rotate(${angle}deg);white-space:nowrap;font-size:9px;font-weight:700;color:#1e293b;text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;pointer-events:none;letter-spacing:0.02em">${name}</div>`,
    iconSize:   [0, 0],
    iconAnchor: [0, 0],
  })
}

const LABEL_MIN_ZOOM = 13

function LiftLabels({ lifts }: { lifts: LiftGeo[] }) {
  const map = useMap()
  const [zoom, setZoom] = useState(map.getZoom())
  useMapEvents({ zoom: () => setZoom(map.getZoom()) })
  if (zoom < LABEL_MIN_ZOOM) return null
  return (
    <>
      {lifts.map(lift => (
        <Marker
          key={lift.name}
          position={liftMid(lift.coordinates)}
          icon={makeLiftIcon(lift.name, liftAngleDeg(lift.coordinates))}
          interactive={false}
        />
      ))}
    </>
  )
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

export default function MapView({ runs, lifts, hovered, onHover }: Props) {
  return (
    <MapContainer
      center={[39.25, -120.2]}
      zoom={13}
      style={{ width: "100%", height: "100%" }}
    >
      <TileLayer
        url="https://tile.opentopomap.org/{z}/{x}/{y}.png"
        attribution='Map data: &copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM | Style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)'
        maxZoom={17}
        opacity={0.25}
      />
      <FitBounds runs={runs} />

      {/* Lifts — dashed lines */}
      {lifts.map(lift => (
        <Polyline
          key={lift.name}
          positions={lift.coordinates.map(([lon, lat]) => [lat, lon] as [number, number])}
          pathOptions={{ color: "#1e293b", weight: 2, opacity: 0.65, dashArray: "6 4" }}
        />
      ))}

      {/* Lift labels — only when zoomed in enough */}
      <LiftLabels lifts={lifts} />

      {/* Runs */}
      {runs.map(run => {
        const isHovered = hovered === run.name
        return buildGroups(run.coordinates, run.slopes).map((g, gi) => (
          <Polyline
            key={`${run.name}-${gi}`}
            positions={g.positions}
            pathOptions={{
              color:   g.color,
              weight:  isHovered ? 8 : 4.5,
              opacity: isHovered ? 1 : 0.85,
            }}
            eventHandlers={{
              mouseover: () => onHover(run.name),
              mouseout:  () => onHover(null),
            }}
          />
        ))
      })}
    </MapContainer>
  )
}
