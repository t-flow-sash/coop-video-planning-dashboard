"use client";

import { CoopWorkspace } from "@/features/coops/coop-workspace";
import { apiRepository } from "@/lib/api/coops";

export function ApiWorkspace() {
  return <CoopWorkspace repository={apiRepository} />;
}
