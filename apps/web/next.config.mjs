/** @type {import('next').NextConfig} */
const demo = process.env.NEXT_PUBLIC_DEMO_MODE === "true";
const repositoryName =
  process.env.PAGES_REPOSITORY_NAME ??
  process.env.GITHUB_REPOSITORY?.split("/").pop() ??
  "coop-video-planning-dashboard";

const nextConfig = {
  output: demo ? "export" : "standalone",
  basePath: demo ? `/${repositoryName}` : "",
  assetPrefix: demo ? `/${repositoryName}/` : undefined,
  trailingSlash: demo,
  images: { unoptimized: true },
};

export default nextConfig;
