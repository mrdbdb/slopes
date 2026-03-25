export interface RunProfile {
  name: string
  steepest: number
  face_steepest?: number
  face_delta?: number
  is_traverse?: boolean
  length_km: number
  profile: [number, number][] // [dist_km, slope_deg]
  osm_id?: number
  osm_difficulty?: string
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
  { label: "Beginner",     min: 10, color: "#16a34a" },
  { label: "Low Beginner", min: 0,  color: "#b5a0fb" },
] as const

export function tierFor(deg: number) {
  return TIERS.find(t => deg >= t.min) ?? TIERS[TIERS.length - 1]
}

const OSM_DIFFICULTY_FLOOR: Record<string, number> = {
  easy:         0,
  intermediate: 18,
  advanced:     27,
  expert:       36,
  freeride:     36,
}

export function effectiveSteepest(run: { steepest: number; face_steepest?: number; is_traverse?: boolean; osm_difficulty?: string }): number {
  // For traverses, face comes from terrain beside the skier — use line steepness only.
  // For non-traverses, face reflects terrain underfoot and may exceed line (e.g. short
  // steep rolls), so take the max.
  let measured = (run.face_steepest != null && !run.is_traverse)
    ? Math.max(run.steepest, run.face_steepest)
    : run.steepest
  // Floor: never show a run as easier than its OSM difficulty tag implies.
  const floor = OSM_DIFFICULTY_FLOOR[run.osm_difficulty ?? ''] ?? 0
  return Math.max(measured, floor)
}
