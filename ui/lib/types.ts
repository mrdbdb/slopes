export interface RunProfile {
  name: string
  steepest: number
  face_steepest?: number
  face_delta?: number
  is_traverse?: boolean
  length_km: number
  profile: [number, number][] // [dist_km, slope_deg]
  osm_id?: number
}

export type RunRow = RunProfile | null // null = tier separator

export interface ResortData {
  name: string
  color: string
  runs: RunRow[]
}

export const TIERS = [
  { label: "Expert",       min: 36, color: "#ef4444" },
  { label: "Advanced",     min: 27, color: "#1f2937" },
  { label: "Intermediate", min: 18, color: "#3b82f6" },
  { label: "Beginner",     min: 10, color: "#22c55e" },
  { label: "Low Beginner", min: 0,  color: "#b5a0fb" },
] as const

export function tierFor(deg: number) {
  return TIERS.find(t => deg >= t.min) ?? TIERS[TIERS.length - 1]
}
