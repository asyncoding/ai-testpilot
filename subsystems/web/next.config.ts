import type { NextConfig } from "next";

// 纯静态导出：产物在 out/，由 FastAPI StaticFiles 同端口托管（单进程演示，无 CORS）。
// rewrites 只在 `next dev` 开发态生效，导出产物不含服务端逻辑。
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8765/api/:path*",
      },
    ];
  },
};

export default nextConfig;
