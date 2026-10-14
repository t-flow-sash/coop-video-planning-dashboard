import { DemoWorkspace } from "@/features/coops/demo-workspace";
import { ApiWorkspace } from "@/features/coops/api-workspace";

export default function CoopsPage() {
  return process.env.NEXT_PUBLIC_DEMO_MODE === "true" ? (
    <DemoWorkspace />
  ) : (
    <ApiWorkspace />
  );
}
