// npm install --no-save playwright
// node tools/web_history_smoke.mjs
// The Python child uses only temporary fixture data, never the application DB.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
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
let browser;
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
  browser = await chromium.launch({ headless: true });
  const errors = [];
  async function createPage(viewport) {
    const context = await browser.newContext({ viewport, acceptDownloads: true });
    const login = await context.request.post(fixture.url + "/api/login", {
      data: { password: fixture.password }, headers: { Origin: fixture.url },
    });
    assert.equal(login.status(), 200);
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(fixture.url);
    await ready(page);
    return { context, page };
  }
  async function ready(page) {
    await page.waitForFunction(() => {
      const button = document.getElementById("btn-send");
      return button && !button.disabled;
    });
  }
  async function command(context, value) {
    const response = await context.request.post(fixture.url + "/api/command", {
      data: { command: value }, headers: { Origin: fixture.url },
    });
    assert.equal(response.status(), 200);
  }
  async function verifyHistory(page) {
    assert.equal(await page.locator("#log table").count(), 1);
    assert.equal(await page.locator("#log .task-checkbox").count(), 2);
    assert.equal(await page.locator("#log blockquote").count(), 1);
    assert.equal(await page.locator("#log .media-img").count(), 3);
    await page.waitForFunction(() => [...document.querySelectorAll("#log .media-img")]
      .every((image) => image.complete && image.naturalWidth === 480 && image.naturalHeight === 280));
    const pixels = await page.evaluate(() => [...document.querySelectorAll("#log .media-img")].map((image) => {
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(image, 0, 0);
      return [...ctx.getImageData(80, 80, 1, 1).data];
    }));
    assert.deepEqual(pixels, [[204, 68, 85, 255], [32, 141, 119, 255], [32, 141, 119, 255]]);
    assert.match(await page.locator("#log").innerText(), /complete caption stays visible/);
    assert.match(await page.locator("#log .media-error").innerText(), /missing.txt/);
    const shell = page.locator("#log .msg-row.cmd");
    assert.equal(await shell.locator("pre code").innerText(), "first\n  second\nthird");
    assert.equal(await page.locator("#log a[href^='/api/history/media/']").count(), 6);
    const history = await page.request.get(fixture.url + "/api/history");
    for (const record of (await history.json()).messages) {
      for (const media of record.media) {
        if (!media.download_url) continue;
        const response = await page.request.get(fixture.url + media.download_url);
        assert.equal(response.status(), 200, media.filename);
        assert.equal((await response.body()).length, media.size);
      }
    }
    const dimensions = await page.evaluate(() => ({
      width: innerWidth, body: document.documentElement.scrollWidth,
      cards: [...document.querySelectorAll(".file-card")].map((el) => ({
        width: el.clientWidth, scroll: el.scrollWidth, x: el.getBoundingClientRect().x,
        right: el.getBoundingClientRect().right, textWidth: el.querySelector(".fc-body").clientWidth,
      })),
      headers: [...document.querySelectorAll(".code-header")].map((el) => {
        const label = el.querySelector(".code-lang").getBoundingClientRect();
        const actions = el.querySelector(".code-actions").getBoundingClientRect();
        return { overlaps: label.right > actions.left && label.bottom > actions.top };
      }),
      downloadColors: [...document.querySelectorAll(".fc-dl")].map((el) => getComputedStyle(el).color),
    }));
    assert.ok(dimensions.body <= dimensions.width + 1, JSON.stringify(dimensions));
    assert.ok(dimensions.cards.every((c) => c.scroll <= c.width + 1 && c.x >= 0 && c.right <= dimensions.width),
      JSON.stringify(dimensions));
    assert.ok(dimensions.cards.every((c) => c.textWidth >= Math.min(140, c.width / 2)),
      JSON.stringify(dimensions));
    assert.ok(dimensions.headers.every((h) => !h.overlaps), JSON.stringify(dimensions));
    assert.ok(dimensions.downloadColors.every((color) => color === "rgb(255, 255, 255)"),
      JSON.stringify(dimensions));
  }

  const { page, context } = await createPage({ width: 1440, height: 1000 });
  await verifyHistory(page);
  const before = await page.locator("#log").innerText();
  await page.reload();
  await ready(page);
  await verifyHistory(page);
  assert.equal(await page.locator("#log").innerText(), before);
  await page.evaluate(() => { document.getElementById("log").scrollTop = 0; });
  await page.screenshot({ path: path.join(output, "desktop-top.png") });
  await page.evaluate(() => { document.getElementById("log").scrollTop = 900; });
  await page.screenshot({ path: path.join(output, "desktop-media.png") });
  await page.evaluate(() => { document.getElementById("log").scrollTop = 100000; });
  await page.screenshot({ path: path.join(output, "desktop-files.png") });
  const downloadPromise = page.waitForEvent("download");
  await page.locator(".file-card").filter({ hasText: "\u7cfb\u7edf\u8bb0\u5fc6.zip" }).click();
  const download = await downloadPromise;
  assert.equal(download.suggestedFilename(), "\u7cfb\u7edf\u8bb0\u5fc6.zip");
  assert.equal(await download.failure(), null);
  await page.locator("#btn-search").click();
  await page.locator("#search-input").fill("<img src=x onerror=alert(1)>");
  assert.equal(await page.locator("#log mark.highlight-match").count(), 1);
  assert.equal(await page.locator("#log img[src='x']").count(), 0);
  await page.locator("#search-input").press("Escape");
  await context.setOffline(true);
  await page.waitForFunction(() => document.getElementById("btn-send").disabled);
  await context.setOffline(false);
  await ready(page);
  await verifyHistory(page);
  console.log("ok desktop refresh, formatting, originals, downloads, search, reconnect");

  async function openSettings(target) {
    await target.locator("#btn-settings").click();
    await target.locator(".skill-item").last().waitFor();
  }
  async function toggleSkill(target) {
    const response = target.waitForResponse((res) => res.url().endsWith("/api/config")
      && res.request().method() === "POST");
    await target.locator(".skill-item[data-path='daily.md'] .skill-state").click();
    assert.equal((await response).status(), 200);
    await target.waitForFunction(() => !document.querySelector(
      ".skill-item[data-path='daily.md'] .skill-state").disabled);
  }
  await openSettings(page);
  const state = page.locator(".skill-item[data-path='daily.md'] .skill-state");
  assert.equal(await page.locator(".skill-item[data-path='daily.md'] button").count(), 1);
  assert.equal(await state.getAttribute("data-state"), "enabled");
  const colors = [await state.evaluate((button) => getComputedStyle(button).backgroundColor)];
  await toggleSkill(page);
  assert.equal(await state.getAttribute("data-state"), "disabled");
  colors.push(await state.evaluate((button) => getComputedStyle(button).backgroundColor));
  await toggleSkill(page);
  assert.equal(await state.getAttribute("data-state"), "hidden");
  colors.push(await state.evaluate((button) => getComputedStyle(button).backgroundColor));
  assert.equal(new Set(colors).size, 3);
  await page.reload();
  await ready(page);
  await openSettings(page);
  assert.equal(await state.getAttribute("data-state"), "hidden");
  await toggleSkill(page);
  assert.equal(await state.getAttribute("data-state"), "enabled");
  await page.route('**/api/config', (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'Fixture save failed' }) })
    : route.continue());
  await state.click();
  await page.getByText(/Fixture save failed/).waitFor();
  assert.equal(await state.getAttribute('data-state'), 'enabled');
  await page.unroute('**/api/config');
  await page.locator(".skill-list").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, "desktop-skills.png") });
  await page.locator("#btn-close-settings").click();
  console.log("ok one skill button, three colors, cycle, refresh persistence and failed saves");

  const mobile = await createPage({ width: 390, height: 844 });
  await verifyHistory(mobile.page);
  await mobile.page.reload();
  await ready(mobile.page);
  await verifyHistory(mobile.page);
  await mobile.page.evaluate(() => { document.getElementById("log").scrollTop = 0; });
  await mobile.page.screenshot({ path: path.join(output, "mobile-top.png") });
  await mobile.page.evaluate(() => { document.getElementById("log").scrollTop = 100000; });
  await mobile.page.screenshot({ path: path.join(output, "mobile-files.png") });
  await mobile.page.setViewportSize({ width: 320, height: 568 });
  await verifyHistory(mobile.page);
  await mobile.page.screenshot({ path: path.join(output, "mobile-narrow.png") });
  await openSettings(mobile.page);
  await mobile.page.locator(".skill-list").scrollIntoViewIfNeeded();
  const skillDimensions = await mobile.page.locator(".skill-item").evaluateAll((items) => items.map((item) => {
    const parent = item.getBoundingClientRect();
    const name = item.querySelector(".skill-name").getBoundingClientRect();
    const controls = item.querySelector(".skill-state").getBoundingClientRect();
    return {
      contained: parent.left >= 0 && parent.right <= innerWidth
        && name.left >= parent.left && name.right <= parent.right
        && controls.left >= parent.left && controls.right <= parent.right,
      overlaps: name.right > controls.left && name.left < controls.right
        && name.bottom > controls.top && name.top < controls.bottom,
      overflow: item.scrollWidth > item.clientWidth + 1,
    };
  }));
  assert.ok(skillDimensions.every((item) => item.contained && !item.overlaps && !item.overflow),
    JSON.stringify(skillDimensions));
  await mobile.page.screenshot({ path: path.join(output, "mobile-skills.png") });
  await mobile.context.close();
  console.log("ok mobile refresh and file layout");

  await page.locator("#input").fill("live test");
  await page.locator("#btn-send").click();
  await page.getByRole("heading", { name: "LIVE FINAL", exact: true }).waitFor();
  await ready(page);
  const richBefore = await page.locator(".msg-row").filter({ has: page.getByRole("heading", {
    name: "LIVE FINAL", exact: true,
  }) }).locator(".body").innerHTML();
  await page.reload();
  await ready(page);
  const richAfter = await page.locator(".msg-row").filter({ has: page.getByRole("heading", {
    name: "LIVE FINAL", exact: true,
  }) }).locator(".body").innerHTML();
  assert.equal(richAfter, richBefore);
  await command(context, "/fixture/frames");
  await page.getByText("UPDATED FINAL", { exact: true }).waitFor();
  assert.equal(await page.getByText("UPDATED FINAL", { exact: true }).count(), 1);
  assert.equal(await page.getByText("DELETE ME", { exact: true }).count(), 0);
  assert.equal(await page.getByRole("link", { name: "Example", exact: true }).getAttribute("href"),
    "https://example.com");
  console.log("ok live vs restored markup, edit upserts, deletion and URL buttons");

  await command(context, '/fixture/generated');
  const generated = page.locator('#log .msg-row').filter({ has: page.getByRole('heading', { name: 'GENERATED REPLY', exact: true }) });
  await generated.waitFor();
  const generatedBody = await generated.locator('.body').innerHTML();
  async function verifyGenerated() {
    assert.equal(await generated.count(), 1);
    assert.equal(await generated.locator('.bubble').count(), 1);
    assert.equal(await generated.locator('.bubble-header').count(), 1);
    assert.match(await generated.locator('.bubble-header').innerText(), /XGent/);
    assert.equal(await generated.locator('.media-img').count(), 2);
    assert.equal(await generated.locator('.body').innerHTML(), generatedBody);
    assert.equal(await generated.locator('.media-paths').count(), 0, 'paths already in the notice must not repeat');
    await page.waitForFunction(() => [...document.querySelectorAll('img[alt^="generated-"]')]
      .every((image) => image.complete && image.naturalWidth === 480));
    const pixels = await generated.locator('.media-img').evaluateAll((images) => images.map((image) => {
      const canvas = document.createElement('canvas');
      canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(image, 0, 0);
      return [...ctx.getImageData(20, 20, 1, 1).data];
    }));
    assert.deepEqual(pixels, [[200, 75, 86, 255], [22, 139, 116, 255]]);
    for (const href of await generated.locator('.media-dl').evaluateAll((links) => links.map((link) => link.getAttribute('href')))) {
      assert.equal((await page.request.get(fixture.url + href)).status(), 200);
    }
    const layout = await generated.evaluate((row) => {
      const image = row.querySelector('.message-media').getBoundingClientRect();
      const body = row.querySelector('.body').getBoundingClientRect();
      return { imageFirst: image.bottom <= body.top, overflow: row.scrollWidth > row.clientWidth + 1 };
    });
    assert.equal(layout.imageFirst, true);
    assert.equal(layout.overflow, false);
  }
  await verifyGenerated();
  await page.reload();
  await ready(page);
  await verifyGenerated();
  await generated.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'desktop-generated-group.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await verifyGenerated();
  await generated.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'mobile-generated-group.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });
  console.log('ok generated media: two originals, full text, paths and downloads in one XGent bubble, including refresh during delivery');

  await command(context, "/fixture/busy");
  await page.reload();
  await page.locator("#btn-stop").waitFor({ state: "visible" });
  await ready(page);
  assert.equal(await page.getByText("BUSY FINAL", { exact: true }).count(), 1);
  await command(context, "/fixture/error");
  await page.reload();
  await page.locator("#btn-stop").waitFor({ state: "visible" });
  await ready(page);
  await page.getByText(/Fixture interrupted error/).waitFor();
  console.log("ok refresh during generation and error recovery");

  await page.locator("#file-input").setInputFiles([
    { name: "queue-one.txt", mimeType: "text/plain", buffer: Buffer.from("one") },
    { name: "queue-two.txt", mimeType: "text/plain", buffer: Buffer.from("two") },
    { name: "queue-three.txt", mimeType: "text/plain", buffer: Buffer.from("three") },
  ]);
  await page.locator("#btn-send").click();
  await page.getByText("Received queue-three.txt", { exact: true }).waitFor();
  await ready(page);
  assert.equal(await page.getByText(/上传失败/).count(), 0);
  await page.reload();
  await ready(page);
  for (const name of ["queue-one.txt", "queue-two.txt", "queue-three.txt"]) {
    assert.equal(await page.locator(".file-card").filter({ hasText: name }).count(), 1);
  }
  console.log("ok multiple uploads and restored download cards");

  let uploadRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/upload" && request.method() === "POST") uploadRequests++;
  });
  async function selectFiles(names) {
    await page.locator("#file-input").setInputFiles(names.map((name) => ({
      name, mimeType: "text/plain", buffer: Buffer.from(name),
    })));
  }
  async function pendingNames() {
    return page.locator(".pending-file > span").allTextContents();
  }
  async function clearPending() {
    while (await page.locator(".pf-del").count()) await page.locator(".pf-del").first().click();
    await page.locator("#input").fill("");
  }

  await page.route("**/api/upload", (route) => route.fulfill({
    status: 413, contentType: "application/json", body: JSON.stringify({ error: "Fixture HTTP rejection" }),
  }));
  await selectFiles(["rejected-one.txt", "rejected-two.txt"]);
  await page.locator("#input").fill("Keep this unsent caption");
  await page.locator("#btn-send").click();
  await page.getByText(/Fixture HTTP rejection/).waitFor();
  await ready(page);
  assert.equal(uploadRequests, 1);
  assert.equal((await pendingNames()).length, 2);
  assert.equal(await page.locator("#input").inputValue(), "Keep this unsent caption");
  await page.unroute("**/api/upload");
  await clearPending();

  await selectFiles(["queue-error.txt", "after-error.txt"]);
  await page.locator("#btn-send").click();
  await page.getByText(/Fixture upload turn failed/).waitFor();
  await ready(page);
  assert.equal(uploadRequests, 2);
  assert.deepEqual(await pendingNames(), ["\ud83d\udcce after-error.txt"]);
  await clearPending();
  console.log("ok HTTP and generation failures preserve unsent files, caption and error");

  await selectFiles(["slow-stop.txt", "after-stop.txt"]);
  let accepted = page.waitForResponse((response) => response.url().endsWith("/api/upload"));
  await page.locator("#btn-send").click();
  assert.equal((await accepted).status(), 200);
  await page.locator("#btn-stop").click();
  await ready(page);
  assert.equal(uploadRequests, 3);
  assert.deepEqual(await pendingNames(), ["\ud83d\udcce after-stop.txt"]);
  assert.equal(await page.locator(".file-card").filter({ hasText: "slow-stop.txt" }).count(), 1);
  await clearPending();

  await selectFiles(["slow-disconnect.txt", "after-disconnect.txt"]);
  accepted = page.waitForResponse((response) => response.url().endsWith("/api/upload"));
  await page.locator("#btn-send").click();
  assert.equal((await accepted).status(), 200);
  await context.setOffline(true);
  await page.waitForFunction(() => document.querySelectorAll(".pending-file").length === 1);
  await context.setOffline(false);
  await ready(page);
  assert.equal(uploadRequests, 4);
  assert.deepEqual(await pendingNames(), ["\ud83d\udcce after-disconnect.txt"]);
  assert.equal(await page.locator(".file-card").filter({ hasText: "slow-disconnect.txt" }).count(), 1);
  await clearPending();
  console.log("ok stop and disconnect cancel future uploads without automatic retries");

  await command(context, "/fixture/compression-error");
  await page.locator("#btn-stop").waitFor({ state: "visible" });
  await page.locator("#bot-avatar.pulsing").waitFor();
  await page.getByText("RESTORING FIXTURE", { exact: true }).first().waitFor();
  await page.reload();
  await page.locator("#btn-stop").waitFor({ state: "visible" });
  await page.locator("#bot-avatar.pulsing").waitFor();
  await ready(page);
  await page.getByText("COMPRESSION FAILED: archive retained; retry available", { exact: true }).waitFor();
  assert.equal(await page.locator(".file-card").count(), 1);
  await page.reload();
  await ready(page);
  const retryButton = page.getByRole("button", { name: "重试恢复", exact: true });
  await retryButton.waitFor();
  await page.screenshot({ path: path.join(output, "desktop-restore-retry.png") });
  const retryRequest = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/callback");
  await retryButton.click();
  const retryPayload = (await retryRequest).postDataJSON();
  assert.equal(retryPayload.callback_data, "retry_compress:" + "a".repeat(32));
  assert.ok(Number.isInteger(retryPayload.message_id) && retryPayload.message_id > 0);
  await page.locator("#bot-avatar.pulsing").waitFor();
  await ready(page);
  assert.equal(await page.getByText("COMPRESSION SUMMARY", { exact: true }).count(), 1);
  assert.equal(await page.locator("#bot-avatar.pulsing").count(), 0);
  assert.equal(await retryButton.count(), 0);
  console.log("ok failed restore persists after refresh; retry callbacks and busy avatar recover");

  const oldDownload = await page.locator(".file-card .fc-dl").first().getAttribute("href");
  await command(context, "/fixture/clear");
  await page.waitForFunction(() => document.querySelectorAll("#log .msg-row").length === 0);
  assert.equal((await context.request.get(fixture.url + oldDownload)).status(), 404);
  await command(context, "/fixture/compression");
  await page.locator("#btn-stop").waitFor({ state: "visible" });
  await page.locator("#bot-avatar.pulsing").waitFor();
  await page.getByText("COMPRESSED FIXTURE", { exact: true }).waitFor();
  await ready(page);
  assert.equal(await page.locator(".file-card").count(), 1);
  await page.reload();
  await ready(page);
  assert.equal(await page.getByText("COMPRESSED FIXTURE", { exact: true }).count(), 1);
  assert.equal(await page.getByText("COMPRESSION SUMMARY", { exact: true }).count(), 1);
  assert.equal(await page.locator("#bot-avatar.pulsing").count(), 0);
  const archiveLink = await page.locator(".file-card .fc-dl").getAttribute("href");
  assert.equal((await context.request.get(fixture.url + archiveLink)).status(), 200);
  await page.screenshot({ path: path.join(output, "desktop-compressed.png") });
  assert.equal(errors.length, 0, errors.join("\n"));
  console.log(`ok clear revokes links; compression unlocks input and preserves downloads; screenshots: ${output}`);
} finally {
  if (browser) await browser.close();
  const exited = new Promise((resolve) => child.once("exit", resolve));
  child.stdin.end("stop\n");
  const timer = setTimeout(() => child.kill(), 10000);
  await exited;
  clearTimeout(timer);
}
