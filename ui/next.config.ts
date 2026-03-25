import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async headers() {
    return [
      {
        source: "/data/:path*",
        headers: [
          { key: "Cache-Control", value: "public, max-age=300, stale-while-revalidate=60" },
        ],
      },
    ]
  },
};

export default nextConfig;
