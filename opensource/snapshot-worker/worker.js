#!/usr/bin/env node
import crypto from "node:crypto";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { mkdir } from "node:fs/promises";

import puppeteer from "puppeteer";

const HOST = process.env.SNAPSHOT_WORKER_HOST || "0.0.0.0";
const PORT = parseInt(process.env.SNAPSHOT_WORKER_PORT || "9230", 10);
const CACHE_ROOT = path.resolve(process.env.HEATMAP_CACHE_DIR || "./heatmap_cache");
const NAVIGATION_TIMEOUT = parseInt(process.env.SNAPSHOT_NAVIGATION_TIMEOUT || "45000", 10);
const POST_NAVIGATION_WAIT = parseInt(process.env.SNAPSHOT_AFTER_WAIT || "1200", 10);
const WEBP_QUALITY = clamp(parseInt(process.env.SNAPSHOT_WEBP_QUALITY || "85", 10), 60, 100);

let browser;

async function getBrowser() {
  if (!browser) {
    browser = await puppeteer.launch({
      headless: "new",
      args: ["--no-sandbox", "--disable-setuid-sandbox"],
    });
  }
  return browser;
}

function clamp(value, min, max) {
  if (Number.isNaN(value)) return min;
  return Math.min(Math.max(value, min), max);
}

async function readJson(req) {
  const chunks = [];
  let total = 0;
  const limit = 256 * 1024;
  for await (const chunk of req) {
    total += chunk.length;
    if (total > limit) {
      throw new Error("Payload too large");
    }
    chunks.push(chunk);
  }
  if (!chunks.length) {
    return {};
  }
  const raw = Buffer.concat(chunks).toString("utf-8");
  return JSON.parse(raw);
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function validateJob(payload) {
  if (!payload || typeof payload !== "object") {
    throw createHttpError(400, "Invalid request payload");
  }
  const url = typeof payload.url === "string" ? payload.url.trim() : "";
  if (!url) {
    throw createHttpError(400, "Missing url");
  }
  if (!/^https?:\/\//i.test(url)) {
    throw createHttpError(400, "Only http(s) URLs are allowed");
  }
  const output = typeof payload.output === "string" ? payload.output.trim() : "";
  if (!output) {
    throw createHttpError(400, "Missing output path");
  }
  const resolved = path.resolve(CACHE_ROOT, output);
  if (!resolved.startsWith(CACHE_ROOT)) {
    throw createHttpError(400, "Output path escapes cache directory");
  }
  return {
    url,
    output,
    fullPage: payload.fullPage !== false,
    viewport: normalizeViewport(payload.viewport),
    resolvedPath: resolved,
  };
}

function normalizeViewport(input) {
  if (!input || typeof input !== "object") {
    return null;
  }
  const width = clamp(parseInt(input.width, 10), 240, 8192);
  const height = clamp(parseInt(input.height, 10), 240, 8192);
  const deviceScaleFactor = clamp(Number.parseFloat(input.deviceScaleFactor ?? input.dpr ?? 1), 0.1, 4);
  return { width, height, deviceScaleFactor };
}

function createHttpError(statusCode, message) {
  const err = new Error(message);
  err.statusCode = statusCode;
  return err;
}

function delay(ms) {
  if (!ms || ms <= 0) {
    return Promise.resolve();
  }
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function handleCapture(payload) {
  const job = validateJob(payload);
  await mkdir(path.dirname(job.resolvedPath), { recursive: true });
  const instance = await getBrowser();
  const page = await instance.newPage();
  let viewport = job.viewport;
  try {
    if (viewport) {
      await page.setViewport({
        width: viewport.width,
        height: viewport.height,
        deviceScaleFactor: viewport.deviceScaleFactor,
      });
    }
    const startedAt = Date.now();
    await page.goto(job.url, {
      waitUntil: ["load", "domcontentloaded", "networkidle2"],
      timeout: NAVIGATION_TIMEOUT,
    });
    if (POST_NAVIGATION_WAIT > 0) {
      await delay(POST_NAVIGATION_WAIT);
    }
    const screenshotBuffer = await page.screenshot({
      path: job.resolvedPath,
      type: "webp",
      quality: WEBP_QUALITY,
      fullPage: job.fullPage,
      captureBeyondViewport: true,
    });
    const duration = Date.now() - startedAt;
    viewport = page.viewport() ?? viewport ?? {};
    const hash = crypto.createHash("sha256").update(screenshotBuffer).digest("hex");
    return {
      ok: true,
      path: job.output,
      format: "webp",
      width: viewport.width ?? null,
      height: viewport.height ?? null,
      bytes: screenshotBuffer.length,
      duration_ms: duration,
      captured_at: Date.now(),
      sha256: hash,
    };
  } finally {
    await page.close();
  }
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === "GET" && req.url === "/healthz") {
      sendJson(res, 200, { ok: true });
      return;
    }
    if (req.method === "POST" && req.url === "/capture") {
      const payload = await readJson(req);
      const result = await handleCapture(payload);
      sendJson(res, 200, result);
      return;
    }
    sendJson(res, 404, { ok: false, error: "Not found" });
  } catch (err) {
    const statusCode = err.statusCode || 500;
    const message = err.message || "Unexpected error";
    console.error("Snapshot worker error:", err);
    try {
      sendJson(res, statusCode, { ok: false, error: message });
    } catch {
      res.writeHead(statusCode);
      res.end();
    }
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[snapshot-worker] listening on http://${HOST}:${PORT} -> ${CACHE_ROOT}`);
});

async function shutdown() {
  console.log("[snapshot-worker] shutting down");
  if (browser) {
    try {
      await browser.close();
    } catch (err) {
      console.warn("Failed to close browser", err);
    }
  }
  server.close(() => process.exit(0));
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
