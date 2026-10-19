import type { NextConfig } from "next";

// The approver UI talks to the FastAPI backend (uvicorn sdra.api.app:app --port 8000).
const api = process.env.SDRA_API_URL ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${api}/api/:path*` }];
  },
};

export default config;
