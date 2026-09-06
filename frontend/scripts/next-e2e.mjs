import { spawn } from "node:child_process";

const [command, ...args] = process.argv.slice(2);
const child = spawn(process.execPath, ["node_modules/next/dist/bin/next", command, ...args], {
  stdio: "inherit",
  env: {
    ...process.env,
    NEXT_DIST_DIR: ".next-e2e",
    BACKEND_ORIGIN: "http://127.0.0.1:8765",
  },
});

child.on("exit", (code) => process.exit(code ?? 1));
