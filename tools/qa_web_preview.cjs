const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require(process.env.SCENE_FACTORY_PLAYWRIGHT);

const url = process.env.SCENE_FACTORY_QA_URL || "http://127.0.0.1:8765/";
const output = path.resolve(process.env.SCENE_FACTORY_QA_OUTPUT || "outputs/web-qa");
const executablePath = process.env.SCENE_FACTORY_BROWSER;
fs.mkdirSync(output, { recursive: true });

async function generate(page) {
  await page.goto(url, { waitUntil: "networkidle" });
  await page.fill(
    "#prompt",
    "刚做完饭，厨房台面上有砧板、YCB 刀、碗、盘子和杯子，锅盖放在岛台边。",
  );
  const started = Date.now();
  await page.click("#generate");
  await page.waitForSelector("#result-content:not([hidden])", { timeout: 30000 });
  await page.waitForFunction(
    () => document.querySelector("#viewer-status")?.dataset.state !== "loading",
    { timeout: 30000 },
  );
  return Date.now() - started;
}

async function canvasPixelStats(page, canvas) {
  const screenshot = await canvas.screenshot();
  const source = `data:image/png;base64,${screenshot.toString("base64")}`;
  return page.evaluate(async (imageSource) => {
    const image = new Image();
    image.src = imageSource;
    await image.decode();
    const element = document.createElement("canvas");
    element.width = image.naturalWidth;
    element.height = image.naturalHeight;
    const context = element.getContext("2d", { willReadFrequently: true });
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, element.width, element.height).data;
    let opaque = 0;
    let colored = 0;
    let minimumLuminance = 255;
    let maximumLuminance = 0;
    for (let index = 0; index < pixels.length; index += 4) {
      if (pixels[index + 3] > 0) opaque += 1;
      const maximum = Math.max(pixels[index], pixels[index + 1], pixels[index + 2]);
      const minimum = Math.min(pixels[index], pixels[index + 1], pixels[index + 2]);
      if (maximum - minimum > 5) colored += 1;
      const luminance = (pixels[index] + pixels[index + 1] + pixels[index + 2]) / 3;
      minimumLuminance = Math.min(minimumLuminance, luminance);
      maximumLuminance = Math.max(maximumLuminance, luminance);
    }
    return {
      width: element.width,
      height: element.height,
      opaque,
      colored,
      luminanceRange: maximumLuminance - minimumLuminance,
    };
  }, source);
}

async function main() {
  const browser = await chromium.launch({
    executablePath,
    headless: true,
    args: ["--enable-webgl", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
  });
  const errors = [];
  const warnings = [];
  const failedRequests = [];
  const badResponses = [];
  try {
    const desktop = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const desktopCdp = await desktop.context().newCDPSession(desktop);
    await desktopCdp.send("Network.enable");
    await desktopCdp.send("Network.setCacheDisabled", { cacheDisabled: true });
    desktop.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
      if (message.type() === "warning") warnings.push(message.text());
    });
    desktop.on("pageerror", (error) => errors.push(error.message));
    desktop.on("requestfailed", (request) => {
      failedRequests.push(`${request.url()}: ${request.failure()?.errorText || "failed"}`);
    });
    desktop.on("response", (response) => {
      if (response.status() >= 400) badResponses.push(`${response.status()} ${response.url()}`);
    });
    const desktopGenerationMs = await generate(desktop);
    const viewerState = await desktop.locator("#viewer-status").getAttribute("data-state");
    const viewerText = await desktop.locator("#viewer-status").textContent();
    const canvas = desktop.locator("#three-preview canvas");
    const desktopPixels = await canvasPixelStats(desktop, canvas);
    const bundleLink = desktop.locator(".scene-bundle-link");
    const bundleHref = await bundleLink.getAttribute("href");
    const bundleDownloadName = await bundleLink.getAttribute("download");
    const bundleResponse = await desktop.context().request.get(new URL(bundleHref, url).href);
    const bundleBody = await bundleResponse.body();
    fs.writeFileSync(path.join(output, "downloaded.scene.zip"), bundleBody);
    await canvas.screenshot({ path: path.join(output, "three-desktop-before.png") });
    const box = await canvas.boundingBox();
    if (!box) throw new Error("Three.js canvas has no visible bounds");
    await desktop.mouse.move(box.x + box.width * 0.68, box.y + box.height * 0.48);
    await desktop.mouse.down();
    await desktop.mouse.move(box.x + box.width * 0.42, box.y + box.height * 0.36, { steps: 12 });
    await desktop.mouse.up();
    await desktop.waitForTimeout(700);
    await canvas.screenshot({ path: path.join(output, "three-desktop-after.png") });
    await desktop.click('[data-preview-mode="2d"]');
    const topDownVisible = await desktop.locator("#preview").isVisible();
    await desktop.click('[data-preview-mode="3d"]');
    await desktop.screenshot({ path: path.join(output, "desktop-page.png"), fullPage: true });

    const before = fs.readFileSync(path.join(output, "three-desktop-before.png"));
    const after = fs.readFileSync(path.join(output, "three-desktop-after.png"));
    const beforeHash = crypto.createHash("sha256").update(before).digest("hex");
    const afterHash = crypto.createHash("sha256").update(after).digest("hex");

    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true });
    const mobileCdp = await mobile.context().newCDPSession(mobile);
    await mobileCdp.send("Network.enable");
    await mobileCdp.send("Network.setCacheDisabled", { cacheDisabled: true });
    mobile.on("console", (message) => {
      if (message.type() === "error") errors.push(`mobile: ${message.text()}`);
    });
    mobile.on("pageerror", (error) => errors.push(`mobile: ${error.message}`));
    const mobileGenerationMs = await generate(mobile);
    await mobile.waitForTimeout(800);
    const mobilePixels = await canvasPixelStats(
      mobile,
      mobile.locator("#three-preview canvas"),
    );
    const mobileBundleVisible = await mobile.locator(".scene-bundle-link").isVisible();
    await mobile.screenshot({ path: path.join(output, "mobile-page.png"), fullPage: true });
    const mobileLayout = await mobile.evaluate(() => ({
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      canvas: (() => {
        const rect = document.querySelector("#three-preview canvas").getBoundingClientRect();
        return { width: rect.width, height: rect.height };
      })(),
      summaryOverflow: [...document.querySelectorAll(".scene-summary strong")].some(
        (element) => element.scrollWidth > element.clientWidth,
      ),
    }));

    console.log(JSON.stringify({
      desktopGenerationMs,
      mobileGenerationMs,
      viewerState,
      viewerText: viewerText.trim(),
      desktopPixels,
      mobilePixels,
      bundleDownload: {
        visible: await bundleLink.isVisible(),
        status: bundleResponse.status(),
        bytes: bundleBody.length,
        name: bundleDownloadName,
        mobileVisible: mobileBundleVisible,
      },
      cameraInteractionChangedPixels: beforeHash !== afterHash,
      topDownTabVisible: topDownVisible,
      desktopCanvasBytes: before.length,
      mobileLayout,
      errors,
      warnings,
      failedRequests,
      badResponses,
      output,
    }, null, 2));
    if (
      errors.length
      || failedRequests.some((item) => !item.endsWith("net::ERR_ABORTED"))
      || viewerState !== "ready"
      || desktopPixels.opaque < 1000
      || desktopPixels.luminanceRange < 40
      || mobilePixels.opaque < 1000
      || mobilePixels.luminanceRange < 40
      || !bundleResponse.ok()
      || bundleBody.length < 1000
      || !bundleDownloadName?.endsWith(".scene.zip")
      || !mobileBundleVisible
      || beforeHash === afterHash
      || !topDownVisible
      || mobileLayout.scrollWidth > mobileLayout.innerWidth
      || mobileLayout.canvas.width < 300
      || mobileLayout.canvas.height < 240
    ) {
      process.exitCode = 1;
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
