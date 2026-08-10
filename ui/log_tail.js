// log_tail.js - JSONL tailing, no Electron dependency so it can be unit-tested
// with plain `node` (PROMPTS.md A12). Every service in this project already
// hand-rolls the same append-only-JSONL-with-a-timestamp pattern (agent.jsonl,
// loop.jsonl, brain.jsonl, ears.jsonl, voice.jsonl, daemon.jsonl) - this is the
// one place that reads it back, rather than a new transport (no WebSocket/HTTP
// server on the Python side, matching how services/voice/playback_state.py
// already does cross-process coordination via a plain file, not a socket).

const fs = require("fs");

// Reads up to the last maxLines JSON records from a JSONL file. Used once at
// startup per log file so the UI opens with real recent history instead of an
// empty panel - a fresh JsonlTailer only sees appends from its own creation
// point forward.
function readLastLines(filePath, maxLines) {
  if (!fs.existsSync(filePath)) return [];
  const content = fs.readFileSync(filePath, "utf8");
  const lines = content.split("\n").filter((l) => l.trim().length > 0);
  const tail = lines.slice(-maxLines);
  const records = [];
  for (const line of tail) {
    try {
      records.push(JSON.parse(line));
    } catch (e) {
      // malformed/partial line - skip rather than crash the whole read
    }
  }
  return records;
}

// Polls one JSONL file for new appended lines since the last poll(). Seeds its
// offset at the current file size on construction (or 0 if the file doesn't
// exist yet) - callers that want history first should call readLastLines()
// before constructing the tailer, not after.
class JsonlTailer {
  constructor(filePath, onRecord) {
    this.filePath = filePath;
    this.onRecord = onRecord;
    this.offset = fs.existsSync(filePath) ? fs.statSync(filePath).size : 0;
    this.buffer = "";
  }

  poll() {
    if (!fs.existsSync(this.filePath)) return;
    const stat = fs.statSync(this.filePath);
    if (stat.size < this.offset) {
      // File shrank - rotated/truncated out from under us. Restart clean
      // rather than trying to compute a diff against data that's gone.
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
    this.buffer = lines.pop(); // last element may be an incomplete line - hold it for next poll()
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        this.onRecord(JSON.parse(line));
      } catch (e) {
        // one malformed line (e.g. read landed mid-write) shouldn't kill the tailer
      }
    }
  }
}

module.exports = { readLastLines, JsonlTailer };
