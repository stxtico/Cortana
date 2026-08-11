// log_tail.ts - JSONL tailing with a real push signal (PROMPTS.md A12).
// fs.watch() fires on the native OS filesystem-change notification, so a new
// line appended by any Python service (they all open(...).write() the same
// way - see agent.jsonl/loop.jsonl/ears.jsonl/brain.jsonl/voice.jsonl/
// daemon.jsonl) is noticed the moment it lands, not on the next poll tick.
// fs.watch is known to occasionally miss events (particularly on Windows,
// depending on the writer), so a slow backstop poll runs alongside it -
// belt and suspenders, not a substitute for either half.
import * as fs from "fs";
import * as path from "path";

export type JsonRecord = Record<string, unknown>;

// Reads up to the last maxLines JSON records from a JSONL file. Used once at
// startup per log file so the UI opens with real recent history instead of
// an empty panel - a fresh JsonlTailer only sees appends from its own start()
// call forward.
export function readLastLines(filePath: string, maxLines: number): JsonRecord[] {
  if (!fs.existsSync(filePath)) return [];
  const content = fs.readFileSync(filePath, "utf8");
  const lines = content.split("\n").filter((l) => l.trim().length > 0);
  const tail = lines.slice(-maxLines);
  const records: JsonRecord[] = [];
  for (const line of tail) {
    try {
      records.push(JSON.parse(line));
    } catch {
      // malformed/partial line - skip rather than crash the whole read
    }
  }
  return records;
}

export class JsonlTailer {
  private filePath: string;
  private onRecord: (record: JsonRecord) => void;
  private offset: number;
  private buffer = "";
  private watcher: fs.FSWatcher | null = null;
  private pollTimer: NodeJS.Timeout | null = null;

  constructor(filePath: string, onRecord: (record: JsonRecord) => void) {
    this.filePath = filePath;
    this.onRecord = onRecord;
    this.offset = fs.existsSync(filePath) ? fs.statSync(filePath).size : 0;
  }

  poll(): void {
    if (!fs.existsSync(this.filePath)) return;
    const stat = fs.statSync(this.filePath);
    if (stat.size < this.offset) {
      // File shrank - rotated/truncated out from under us. Restart clean
      // rather than diffing against data that's gone.
      this.offset = 0;
      this.buffer = "";
    }
    if (stat.size === this.offset) return;

    const fd = fs.openSync(this.filePath, "r");
    const length = stat.size - this.offset;
    const buf = Buffer.alloc(length);
    fs.readSync(fd, buf, 0, length, this.offset);
    fs.closeSync(fd);
    this.offset = stat.size;

    this.buffer += buf.toString("utf8");
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() ?? ""; // last element may be incomplete - hold for next poll
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        this.onRecord(JSON.parse(line));
      } catch {
        // one malformed line (e.g. a read landing mid-write) shouldn't kill the tailer
      }
    }
  }

  // Real push (fs.watch on the containing directory - watching a file that
  // doesn't exist yet throws, and every one of these logs is created lazily
  // by its first write) plus a slow backstop poll for whatever fs.watch
  // misses.
  start(backstopIntervalMs: number): void {
    const dir = path.dirname(this.filePath);
    const base = path.basename(this.filePath);
    try {
      this.watcher = fs.watch(dir, (_eventType, filename) => {
        if (filename === base) this.poll();
      });
    } catch {
      // directory doesn't exist yet - the backstop poll still covers it once it does
    }
    this.pollTimer = setInterval(() => this.poll(), backstopIntervalMs);
  }

  stop(): void {
    this.watcher?.close();
    this.watcher = null;
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = null;
  }
}
