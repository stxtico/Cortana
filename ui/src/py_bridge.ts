// py_bridge.ts - the one place that shells out to a Python script and parses
// its --json output (PROMPTS.md A12 point 3/4: reuse latency_report.py's
// corrected math and memory.py's real store operations rather than
// reimplementing either in TypeScript). Every caller gets the same
// error-shape contract instead of hand-rolling execFile parsing per call site.
import { execFile } from "child_process";

export interface PyResult<T> {
  ok: boolean;
  data?: T;
  error?: string;
}

export function runPython<T = unknown>(
  root: string,
  scriptRelPath: string,
  args: string[],
  timeoutMs = 10000
): Promise<PyResult<T>> {
  return new Promise((resolve) => {
    execFile("uv", ["run", scriptRelPath, ...args], { cwd: root, timeout: timeoutMs }, (err, stdout, stderr) => {
      if (err) {
        resolve({ ok: false, error: (stderr || err.message).trim() });
        return;
      }
      try {
        resolve({ ok: true, data: JSON.parse(stdout) as T });
      } catch (e) {
        resolve({ ok: false, error: `bad JSON from ${scriptRelPath}: ${(e as Error).message}` });
      }
    });
  });
}
