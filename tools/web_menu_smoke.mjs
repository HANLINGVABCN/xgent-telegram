// Real menu routing, durable SQLite history, and SSE/polling in headless Chromium.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const storage = await fs.mkdtemp(path.join(os.tmpdir(), "xgent-menu-browser-"));
const output = path.join(root, "workspace", "web-menu-qa");
await fs.mkdir(output, { recursive: true });
let child, browser, fixture;
const errors = [];

async function start(port = 0) {
  child = spawn(process.env.PYTHON || "python", ["-u", "tools/web_menu_fixture.py", "--root", storage, "--port", String(port)], {
    cwd: root, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  let stderr = "";
  child.stderr.on("data", (data) => { stderr += data; });
  fixture = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(new Error("Fixture startup timed out\n" + stderr)), 25000);
    child.once("error", reject);
    child.once("exit", (code) => { clearTimeout(timer); reject(new Error(`Fixture exited: ${code}\n${stderr}`)); });
    child.stdout.on("data", (data) => {
      text += data;
      if (!text.includes("\n")) return;
      clearTimeout(timer);
      resolve(JSON.parse(text.split("\n")[0]));
    });
  });
}

async function stop() {
  if (!child || child.exitCode !== null) return;
  const active = child;
  await new Promise((resolve) => { active.once("exit", resolve); active.stdin.end("\n"); });
}

async function login(context) {
  const response = await context.request.post(fixture.url + "/api/login", {
    data: { password: fixture.password }, headers: { Origin: fixture.url },
  });
  assert.equal(response.status(), 200);
}

async function command(context, value) {
  const response = await context.request.post(fixture.url + "/api/command", {
    data: { command: value }, headers: { Origin: fixture.url },
  });
  assert.equal(response.status(), 200);
}

async function ready(page) {
  await page.waitForFunction(() => !document.getElementById("btn-send").disabled);
}

function menu(page, id) { return page.locator(`[data-ui-message-id="${id}"]`); }
async function saved(page) {
  return page.locator("[data-ui-message-id]").evaluateAll((rows) => rows.map((row) => ({
    id: row.dataset.uiMessageId, revision: Number(row.dataset.uiRevision),
    text: row.querySelector(".body").textContent,
    buttons: [...row.querySelectorAll(".ik-btn")].map((item) => item.textContent),
  })));
}
async function click(page, id, label) {
  const before = Number(await menu(page, id).getAttribute("data-ui-revision"));
  await menu(page, id).getByRole("button", { name: label }).click();
  await page.waitForFunction(({id, before}) => {
    const row = document.querySelector(`[data-ui-message-id="${id}"]`);
    return row && Number(row.dataset.uiRevision) > before;
  }, {id, before});
}

try {
  await start();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await login(context);
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(fixture.url);
  await ready(page);
  await command(context, "/start");
  await page.locator("[data-ui-message-id]").waitFor();
  const id = (await saved(page))[0].id;
  await click(page, id, /更多/);
  await click(page, id, /Skill/);
  await click(page, id, /中文技能/);
  const first = await saved(page);
  assert.equal(first.length, 1);
  await page.reload();
  await ready(page);
  assert.deepEqual(await saved(page), first);
  assert.doesNotMatch(await page.locator("#log").innerText(), /点击按钮:|Skill 状态已更新/);
  await command(context, "/start");
  await page.waitForFunction(() => document.querySelectorAll("[data-ui-message-id]").length === 2);
  const second = await context.newPage();
  second.on("pageerror", (error) => errors.push(error.message));
  await second.goto(fixture.url);
  await ready(second);
  await click(second, id, /中文技能/);
  const revision = Number(await menu(second, id).getAttribute("data-ui-revision"));
  await page.waitForFunction(({id, revision}) => Number(document.querySelector(`[data-ui-message-id="${id}"]`).dataset.uiRevision) === revision,
    {id, revision});
  assert.deepEqual(await saved(page), await saved(second));
  await page.screenshot({ path: path.join(output, "desktop-menus.png") });

  let release, received;
  const captured = new Promise((resolve) => { received = resolve; });
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/history?limit=0", async (route) => {
    const response = await route.fetch();
    received();
    await gate;
    await route.fulfill({ response });
  }, { times: 1 });
  await page.reload({ waitUntil: "domcontentloaded" });
  await captured;
  await click(second, id, /中文技能/);
  release();
  await ready(page);
  assert.deepEqual(await saved(page), await saved(second));
  console.log("ok menu navigation, multiple menus, refresh/edit race, multiple windows");

  const beforeRestart = await saved(page);
  const port = Number(new URL(fixture.url).port);
  await stop();
  await start(port);
  await login(context);
  await page.reload();
  await ready(page);
  assert.deepEqual(await saved(page), beforeRestart);
  await click(page, id, /中文技能/);
  await click(page, id, /返回/);
  await click(page, id, /返回/);
  assert.equal((await saved(page)).length, 2);
  await click(page, id, /更多/);
  await click(page, id, /Skill/);
  console.log("ok actual backend restart, canonical long callback, back navigation");

  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await mobile.addInitScript(() => { window.EventSource = undefined; });
  await login(mobile);
  const phone = await mobile.newPage();
  phone.on("pageerror", (error) => errors.push(error.message));
  await phone.goto(fixture.url);
  await ready(phone);
  await click(phone, id, /中文技能/);
  await phone.reload();
  await ready(phone);
  assert.equal((await saved(phone)).length, 2);
  assert.ok(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  const dimensions = await phone.locator(".ik-btn").evaluateAll((items) => items.map((item) => ({
    width: item.clientWidth, scroll: item.scrollWidth,
  })));
  assert.ok(dimensions.every((item) => item.scroll <= item.width + 1), JSON.stringify(dimensions));
  await menu(phone, id).scrollIntoViewIfNeeded();
  await phone.screenshot({ path: path.join(output, "mobile-polling-menu.png") });
  await command(mobile, "/fixture/delete");
  await phone.waitForFunction(() => document.querySelectorAll("[data-ui-message-id]").length === 1);
  await phone.reload();
  await ready(phone);
  await command(mobile, "/fixture/replay");
  await phone.waitForTimeout(1600);
  assert.equal((await saved(phone)).length, 1);
  await command(mobile, "/fixture/clear");
  await phone.waitForFunction(() => document.querySelectorAll("[data-ui-message-id]").length === 0);
  await command(mobile, "/fixture/replay");
  await phone.waitForTimeout(1600);
  assert.equal((await saved(phone)).length, 0);
  console.log("ok mobile polling, tombstone replay after refresh, clear-generation replay");
  assert.deepEqual(errors, []);
} finally {
  if (browser) await browser.close();
  await stop();
  // Remove only the isolated directory that mkdtemp created above.
  assert.ok(path.dirname(storage) === os.tmpdir() && path.basename(storage).startsWith("xgent-menu-browser-"));
  await fs.rm(storage, { recursive: true, force: true });
}
