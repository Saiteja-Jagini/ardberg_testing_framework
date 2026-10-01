import Dashboard from "@/components/Dashboard";

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Dashboard initialRunId={id} />;
}
