import type { ResortData, RunProfile } from "./types"
import { effectiveSteepest } from "./types"
import { fetchData } from "./dataFetch"

interface ResortMeta { name: string; slug: string; color: string; region?: string }

interface GeoFeature {
  geometry: { type: string; coordinates: [number, number][] | [number, number][][] }
  properties: {
    name: string
    steepest: number
    face_steepest?: number
    is_traverse?: boolean
    is_area?: boolean
    osm_id?: number
    osm_difficulty?: string
    slopes: number[]
    line_slopes?: number[]
  }
}

interface GeoJSON { resort?: string; color?: string; features: GeoFeature[] }

function haversineKm([lon1, lat1]: [number, number], [lon2, lat2]: [number, number]): number {
  const R = 6371
  const dLat = (lat2 - lat1) * Math.PI / 180
  const dLon = (lon2 - lon1) * Math.PI / 180
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2
  return R * 2 * Math.asin(Math.sqrt(a))
}

export function featureToProfile(feature: GeoFeature, useFace = true): RunProfile {
  const coords = feature.geometry.type === "Polygon"
    ? (feature.geometry.coordinates as [number, number][][])[0]
    : feature.geometry.coordinates as [number, number][]
  const rawSlopes = (useFace ? feature.properties.slopes : (feature.properties.line_slopes ?? feature.properties.slopes))
  // Mirror the map's scaling: ensure the profile peak matches effectiveSteepest when
  // raw per-segment max under-reports (over-smoothed geo segments).
  const eff = effectiveSteepest(feature.properties)
  const maxSeg = rawSlopes.length > 0 ? Math.max(...rawSlopes) : 0
  const scale = maxSeg > 0 && eff > maxSeg ? eff / maxSeg : 1
  const profile: [number, number][] = []
  let cumDist = 0
  for (let i = 0; i < rawSlopes.length; i++) {
    profile.push([parseFloat(cumDist.toFixed(3)), Math.max(0, rawSlopes[i] * scale)])
    cumDist += haversineKm(coords[i], coords[i + 1])
  }
  profile.push([parseFloat(cumDist.toFixed(3)), 0])
  return {
    name: feature.properties.name,
    steepest: feature.properties.steepest,
    face_steepest: feature.properties.face_steepest,
    is_traverse: feature.properties.is_traverse,
    is_area: feature.properties.is_area,
    osm_id: feature.properties.osm_id,
    osm_difficulty: feature.properties.osm_difficulty,
    length_km: parseFloat(cumDist.toFixed(3)),
    profile,
  }
}

export async function loadResortFromGeo(slug: string): Promise<ResortData> {
  const [geo, index] = await Promise.all([
    fetchData(`/data/${slug}_geo.json`).then(r => r.json() as Promise<GeoJSON>),
    fetchData("/data/index.json").then(r => r.json() as Promise<ResortMeta[]>).catch(() => [] as ResortMeta[]),
  ])
  const meta = index.find(m => m.slug === slug)
  return {
    name: meta?.name ?? geo.resort ?? slug,
    color: meta?.color ?? geo.color ?? "#64748b",
    runs: geo.features.map(f => featureToProfile(f)),
  }
}
