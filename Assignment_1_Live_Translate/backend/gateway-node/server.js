/*
 * FDE · Assignment 1 · Node Gateway  (the "software backend")
 * ==========================================================
 * This is the ONLY server the browser widget talks to. Its jobs:
 *   - serve the widget file at /widget.js
 *   - accept translation requests from the widget (CORS, validation)
 *   - forward them to the Python AI service
 *   - expose /health and /stats
 *   - log every request
 *
 * It is ~90% done. Find the two `TODO (YOU)` blocks and implement them.
 * Everything else works out of the box.
 *
 * Run:  npm install && npm start      (needs Node 18+ for global fetch)
 */
const express = require("express");
const cors = require("cors");
const path = require("path");
const fs = require("fs");
const { randomUUID } = require("crypto");
require("dotenv").config();

const PORT = process.env.PORT || 8787;
const AI_SERVICE_URL = process.env.AI_SERVICE_URL || "http://localhost:8000";
const WIDGET_PATH = path.join(__dirname, "..", "..", "widget", "translation-widget.js");
// Generous by default: /translate/batch translates sequentially upstream, so a
// large uncached batch legitimately takes tens of seconds. This only exists to
// catch a genuinely hung AI service, not to police slow-but-working requests.
const AI_TIMEOUT_MS = Number(process.env.AI_TIMEOUT_MS || 40000);

// --- structured logging ---------------------------------------------------
// Mirrors the Python service's lib/logger.py: one JSON object per line, written
// to BOTH stdout and a log file. The file matters — the trace check greps
// gateway.log on disk, and console.log alone would never create it (you'd have
// to remember `npm start > gateway.log`). __dirname keeps it independent of cwd.
const LOG_FILE = path.join(__dirname, "gateway.log");
const logStream = fs.createWriteStream(LOG_FILE, { flags: "a" });

function log(event, fields = {}) {
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    level: "INFO",
    service: "gateway",
    event,
    ...fields,
  });
  console.log(line);
  logStream.write(line + "\n");
}

const app = express();
const startedAt = Date.now();

// --- middleware ----------------------------------------------------------
app.use(cors()); // dev: allow every origin so the widget works on any page
app.use(express.json({ limit: "1mb" }));

// One structured line per request, emitted AFTER it finishes so we can read the
// final status code and the true elapsed time.
//
// This is also where the trace begins: every request gets a request ID — the
// inbound X-Request-Id if the caller sent one (so an upstream trace continues
// unbroken), otherwise a fresh UUID. We stash it on `req` for the proxy call to
// forward, and echo it back on the response so the browser can correlate too.
app.use((req, res, next) => {
  const t0 = Date.now();
  const requestId = req.get("X-Request-Id") || randomUUID();
  req.requestId = requestId;
  res.setHeader("X-Request-Id", requestId);

  res.on("finish", () => {
    log("request", {
      requestId,
      method: req.method,
      url: req.originalUrl,
      status: res.statusCode,
      durationMs: Date.now() - t0,
    });
  });

  next();
});

// --- serve the widget to the console loader ------------------------------
app.get("/widget.js", (req, res) => {
  res.type("application/javascript");
  res.sendFile(WIDGET_PATH);
});

// --- helper: forward a request to the Python AI service ------------------
// POST `body` as JSON to the AI service and return the parsed JSON.
// Throws on anything that isn't a 2xx so the routes can turn it into a 502 —
// never swallow the failure, or the widget would show English and call it a win.
// The error message carries the upstream detail, so a 502 is diagnosable
// ("AI service error: 500 ...provider 529...") instead of just "AI service 500".
async function callAiService(path, body, requestId) {
  let res;
  try {
    res = await fetch(AI_SERVICE_URL + path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // continue the trace into the Python service
        "X-Request-Id": requestId || randomUUID(),
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(AI_TIMEOUT_MS),
    });
  } catch (err) {
    // fetch rejects for transport failures (service down, DNS) and aborts
    if (err.name === "TimeoutError" || err.name === "AbortError") {
      throw new Error(`timeout after ${AI_TIMEOUT_MS}ms`);
    }
    throw new Error(`unreachable (${err.message})`);
  }

  if (!res.ok) {
    // The AI service reports failures as JSON ({error, requestId}); unwrap it so
    // the 502 reads "AI service error: 502 RuntimeError: provider 529 overloaded"
    // rather than a JSON blob stringified inside another error message.
    const body = await res.text().catch(() => "");
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      detail = parsed.error || parsed.detail || body;
    } catch (_) {
      /* not JSON — use the raw body */
    }
    throw new Error(`${res.status}${detail ? " " + String(detail).slice(0, 300) : ""}`);
  }
  return res.json();
}

// --- routes the widget calls ---------------------------------------------
app.post("/translate", async (req, res) => {
  const { text, target } = req.body || {};
  if (typeof text !== "string") return res.status(400).json({ error: "`text` (string) is required" });
  try {
    const data = await callAiService("/translate", { text, target: target || "es-MX" }, req.requestId);
    res.json(data);
  } catch (err) {
    res.status(502).json({ error: "AI service error: " + err.message });
  }
});

app.post("/translate/batch", async (req, res) => {
  const { texts, target } = req.body || {};
  if (!Array.isArray(texts)) return res.status(400).json({ error: "`texts` (array) is required" });
  try {
    const data = await callAiService("/translate/batch", { texts, target: target || "es-MX" }, req.requestId);
    res.json(data);
  } catch (err) {
    res.status(502).json({ error: "AI service error: " + err.message });
  }
});

app.get("/health", async (req, res) => {
  const uptimeSec = Math.round((Date.now() - startedAt) / 1000);
  let ai = "unreachable";
  try {
    const r = await fetch(AI_SERVICE_URL + "/health", { headers: { "X-Request-Id": req.requestId } });
    ai = r.ok ? await r.json() : "error";
  } catch (_) {}
  res.json({ status: "ok", gatewayUptimeSec: uptimeSec, aiService: ai });
});

app.get("/stats", async (req, res) => {
  try {
    const r = await fetch(AI_SERVICE_URL + "/stats", { headers: { "X-Request-Id": req.requestId } });
    res.json(await r.json());
  } catch (err) {
    res.status(502).json({ error: "AI service error: " + err.message });
  }
});

app.listen(PORT, () => {
  console.log(`FDE gateway on http://localhost:${PORT}  →  AI service ${AI_SERVICE_URL}`);
  console.log(`Widget served at http://localhost:${PORT}/widget.js`);
  console.log(`Structured logs → ${LOG_FILE}`);
});
