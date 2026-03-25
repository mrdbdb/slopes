import MapApp from "@/components/MapApp"

export default async function ResortPage({ params }: { params: Promise<{ resort: string }> }) {
  const { resort } = await params
  return <MapApp initialSlug={resort} />
}
