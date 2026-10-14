"use client";

import { CoopWorkspace } from "@/features/coops/coop-workspace";
import { demoRepository } from "@/features/coops/demo-data";

export function DemoWorkspace() {
  return <CoopWorkspace repository={demoRepository} demoMode />;
}
