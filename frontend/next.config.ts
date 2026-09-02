import type { NextConfig } from "next";

/**
 * The browser only ever talks to one origin. Proxying /api to FastAPI in dev means no CORS
 * middleware on the backend and no environment-specific base URL in the client -- the same
 * relative fetch works in dev and behind a single reverse proxy in production.
 */
const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000"}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
