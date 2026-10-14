/** @type {import('next').NextConfig} */
const demo = process.env.NEXT_PUBLIC_DEMO_MODE === "true";
const repositoryName = "coop-video-planning-dashboard";

const nextConfig = {
  output: demo ? "export" : undefined,
  basePath: demo ? `/${repositoryName}` : "",
  assetPrefix: demo ? `/${repositoryName}/` : undefined,
  trailingSlash: demo,
  images: { unoptimized: true },
};

export default nextConfig;
