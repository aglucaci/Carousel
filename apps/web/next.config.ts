import path from "node:path";
import type { NextConfig } from "next";

const config: NextConfig = {
  // The browser only talks to this app; app/api/v1/[...path] forwards to the CAROUSEL API.
  poweredByHeader: false,
  turbopack: { root: path.join(__dirname) },
};

export default config;
