// Finite-event fallback through a proxy that deliberately buffers SSE bodies.
// Uses synthetic data only, never the application DB or model providers.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const output = path.join(root, "workspace", "web-history-qa");
await fs.mkdir(output, { recursive: true });
const child = spawn(process.env.PYTHON || "python", ["-u", "tools/web_history_fixture.py"], {
  cwd: root, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
  env: { ...process.env, PYTHONIOENCODING: "utf-8" },
});
child.stderr.on("data", (data) => process.stderr.write(data));
let browser, proxy;
const sockets = new Set();
let dropNextBatch = false;
try {
  const fixture = await new Promise((resolve, reject) => {
    let data = "";
    const timer = setTimeout(() => reject(new Error("Fixture startup timed out")), 20000);
    child.on("error", reject);
    child.on("exit", (code) => reject(new Error(`Fixture exited: ${code}`)));
    child.stdout.on("data", (chunk) => {
      data += chunk;
      if (!data.includes("\n")) return;
      clearTimeout(timer);
      resolve(JSON.parse(data.split("\n")[0]));
    });
  });
  proxy = http.createServer((request, response) => {
    const upstream = http.request(new URL(request.url, fixture.url), {
      method: request.method, headers: request.headers,
    }, (source) => {
      if (request.url.startsWith("/api/events") && dropNextBatch) {
        dropNextBatch = false;
        source.resume();
        response.destroy();
        return;
      }
      response.writeHead(source.statusCode, source.headers);
      if (request.url === "/api/stream") {
        response.flushHeaders();
        let first = "";
        let readySent = false;
        source.on("data", (chunk) => {
          if (request.headers["x-fixture-stream"] === "blocked" || readySent) return;
          first += chunk.toString();
          const end = first.indexOf("\n\n");
          if (end >= 0) {
            response.write(first.slice(0, end + 2));
            readySent = true;
            first = "";
          }
        });
        source.on("end", () => response.end());
      } else {
        source.pipe(response);
      }
      source.on("error", () => response.destroy());
    });
    upstream.on("error", () => response.destroy());
    response.on("close", () => upstream.destroy());
    request.pipe(upstream);
  });
  proxy.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise((resolve) => proxy.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${proxy.address().port}`;
  browser = await chromium.launch({ headless: true });
  const errors = [];
  async function context(mode, viewport) {
    const ctx = await browser.newContext({ viewport, extraHTTPHeaders: { "x-fixture-stream": mode } });
    const login = await ctx.request.post(url + "/api/login", {
      data: { password: fixture.password }, headers: { Origin: url },
    });
    assert.equal(login.status(), 200);
    return ctx;
  }
  async function pageIn(ctx) {
    const page = await ctx.newPage();
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto(url);
    await ready(page);
    return page;
  }
  async function ready(page) {
    await page.waitForFunction(() => !document.getElementById("btn-send").disabled, null, { timeout: 35000 });
  }
  async function command(page, value) {
    await page.locator("#input").fill(value);
    await page.locator("#btn-send").click();
  }
  async function clear(ctx, page) {
    const response = await ctx.request.post(url + "/api/command", {
      data: { command: "/fixture/clear" }, headers: { Origin: url },
    });
    assert.equal(response.status(), 200);
    await page.waitForFunction(() => document.querySelectorAll("#log .msg-row").length === 0);
    await ready(page);
  }
  async function verifyArchive(page) {
    const card = page.locator(".file-card").filter({ hasText: "progress.zip" });
    await card.waitFor();
    const link = await card.locator(".fc-dl").getAttribute("href");
    assert.equal((await page.request.get(url + link)).status(), 200);
  }

  const blocked = await context("blocked", { width: 390, height: 844 });
  const [mobile, second] = await Promise.all([pageIn(blocked), pageIn(blocked)]);
  await clear(blocked, mobile);
  let polls = 0, historyReads = 0;
  mobile.on("request", (request) => {
    if (request.url().includes("/api/events")) polls++;
    if (request.url().includes("/api/history?")) historyReads++;
  });
  await command(mobile, "/fixture/progress");
  await Promise.all([mobile, second].map((page) => page.getByText("COMMAND STEP 3", { exact: true }).waitFor()));
  assert.ok(polls > 0);
  assert.equal(historyReads, 0, "live progress must come from event batches, not repeated history loads");
  await mobile.screenshot({ path: path.join(output, "tunnel-mobile-live.png") });
  await mobile.reload();
  await mobile.locator("#btn-stop").waitFor({ state: "visible" });
  await mobile.getByText(/^STREAM DRAFT (?:8|9|10)$/).waitFor();
  await mobile.getByText("PROGRESS COMPLETE", { exact: true }).waitFor();
  await ready(mobile);
  await verifyArchive(mobile);
  for (let step = 1; step <= 10; step++) {
    assert.equal(await mobile.getByText(`COMMAND STEP ${step}`, { exact: true }).count(), 1);
    assert.equal(await second.getByText(`COMMAND STEP ${step}`, { exact: true }).count(), 1);
  }
  await second.close();
  console.log("ok blocked SSE: automatic fallback, live edits, two tabs, refresh during execution and downloads");

  await clear(blocked, mobile);
  await command(mobile, '/fixture/generated');
  let generated = mobile.locator('#log .msg-row').filter({ has: mobile.getByRole('heading', { name: 'GENERATED REPLY', exact: true }) });
  await generated.waitFor();
  assert.equal(await generated.locator('.media-img').count(), 2);
  assert.equal(await generated.locator('.bubble').count(), 1);
  assert.equal(await mobile.getByText('Generating fixture media...', { exact: true }).count(), 0);
  await mobile.reload();
  await ready(mobile);
  generated = mobile.locator('#log .msg-row').filter({ has: mobile.getByRole('heading', { name: 'GENERATED REPLY', exact: true }) });
  assert.equal(await generated.count(), 1);
  assert.equal(await generated.locator('.media-img').count(), 2);
  assert.equal(await generated.locator('.bubble-header').count(), 1);
  assert.equal(await generated.locator('.media-dl').count(), 2);
  await generated.scrollIntoViewIfNeeded();
  await mobile.screenshot({ path: path.join(output, 'tunnel-generated-group.png') });
  await clear(blocked, mobile);
  console.log('ok generated images, text and paths stay in one bubble through polling and refresh');

  await command(mobile, "/fixture/progress");
  await mobile.getByText("COMMAND STEP 1", { exact: true }).waitFor();
  dropNextBatch = true;
  await mobile.getByText("COMMAND STEP 4", { exact: true }).waitFor();
  assert.equal(dropNextBatch, false);
  await mobile.locator("#btn-stop").click();
  await mobile.getByText("PROGRESS STOPPED", { exact: true }).waitFor();
  await ready(mobile);
  assert.equal(await mobile.getByText("COMMAND STEP 1", { exact: true }).count(), 1);
  await verifyArchive(mobile);
  await clear(blocked, mobile);
  let uploadRequests = 0;
  mobile.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/upload" && request.method() === "POST") uploadRequests++;
  });
  await mobile.locator("#file-input").setInputFiles([
    { name: "slow-clear.txt", mimeType: "text/plain", buffer: Buffer.from("accepted") },
    { name: "after-clear.txt", mimeType: "text/plain", buffer: Buffer.from("not submitted") },
  ]);
  const accepted = mobile.waitForResponse((response) => response.url().endsWith("/api/upload"));
  await mobile.locator("#btn-send").click();
  assert.equal((await accepted).status(), 200);
  assert.equal((await blocked.request.post(url + "/api/command", {
    data: { command: "/fixture/clear" }, headers: { Origin: url },
  })).status(), 200);
  await mobile.locator(".pending-file").waitFor();
  await ready(mobile);
  assert.equal(uploadRequests, 1, "an epoch reset must cancel queued uploads");
  assert.match(await mobile.locator(".pending-file").innerText(), /after-clear\.txt/);
  await clear(blocked, mobile);
  await blocked.close();
  console.log("ok lost event response retries by cursor; stop, deduplication and clear cancel pending uploads");

  const stalled = await context("stall-after-ready", { width: 1440, height: 1000 });
  const desktop = await pageIn(stalled);
  let fallbackPolls = 0;
  desktop.on("request", (request) => { if (request.url().includes("/api/events")) fallbackPolls++; });
  await command(desktop, "/fixture/progress");
  await desktop.getByText("PROGRESS COMPLETE", { exact: true }).waitFor({ timeout: 35000 });
  await ready(desktop);
  assert.ok(fallbackPolls > 0, "silent open SSE must trigger the heartbeat watchdog");
  assert.equal(await desktop.getByText("COMMAND STEP 10", { exact: true }).count(), 1);
  assert.equal(await desktop.locator("#typing").isVisible(), false);
  await verifyArchive(desktop);
  await desktop.screenshot({ path: path.join(output, "tunnel-desktop-recovered.png") });
  await stalled.close();
  assert.deepEqual(errors, []);
  console.log(`ok SSE silently stalls after readiness: replay catches up and clears loading; screenshots: ${output}`);
} finally {
  if (browser) await browser.close();
  if (proxy) {
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => proxy.close(resolve));
  }
  const exited = new Promise((resolve) => child.once("exit", resolve));
  child.stdin.end("stop\n");
  const timer = setTimeout(() => child.kill(), 10000);
  await exited;
  clearTimeout(timer);
}
