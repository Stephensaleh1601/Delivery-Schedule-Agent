import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/chat", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => sessionStorage.clear());
  await page.reload({ waitUntil: "domcontentloaded" });
});

test("the live demo starts above the fold", async ({ page }) => {
  await expect(page.getByRole("heading", { name: "Open one customer conversation" })).toBeVisible();
  await expect(page.getByLabel("Name")).toBeVisible();
  await expect(page.getByLabel("Postal")).toBeVisible();
  await expect(page.getByLabel("Order")).toBeVisible();

  const open = page.getByRole("button", { name: "Open the chat" });
  await expect(open).toBeVisible();
  const box = await open.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y + box!.height).toBeLessThanOrEqual(674);
  await expect(page.getByText("Nothing decided yet")).toHaveCount(0);
});

test("mobile shows the setup before the inactive phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload({ waitUntil: "domcontentloaded" });
  const open = page.getByRole("button", { name: "Open the chat" });
  await expect(open).toBeVisible();
  const box = await open.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y + box!.height).toBeLessThanOrEqual(844);
});

test("the happy path confirms one feasible window directly", async ({ page }) => {
  const messagePayloads: Record<string, unknown>[] = [];
  const responsePayloads: Record<string, unknown>[] = [];
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    if (/\/api\/orders\/[^/]+\/messages$/.test(request.url())) {
      messagePayloads.push(request.postDataJSON());
    }
    if (/\/api\/offers\/[^/]+\/respond$/.test(request.url())) {
      responsePayloads.push(request.postDataJSON());
    }
  });

  await page.getByLabel("Name").fill("Mrs Chua");
  await page.getByLabel("Postal").fill("408564");
  await page.getByRole("button", { name: "Open the chat" }).click();
  await page.getByLabel("Message").fill("Friday morning works for me.");
  await page.getByRole("button", { name: "Send" }).click();

  const confirm = page.getByRole("button", { name: /^Confirm / }).first();
  await expect(confirm).toBeVisible({ timeout: 30_000 });
  await confirm.click();
  await expect(page.getByText("Confirmed & locked")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/plan v2/)).toBeVisible();
  await expect(page.getByText("Every other customer on this day kept the window they were promised.")).toBeVisible();

  expect(messagePayloads).toHaveLength(1);
  expect(messagePayloads[0].client_message_id).toMatch(/^[0-9a-f-]{36}$/i);
  expect(responsePayloads).toHaveLength(1);
  expect(responsePayloads[0].event_id).toMatch(/^[0-9a-f-]{36}$/i);
});

test("a rejected slot becomes three choices and a locked route", async ({ page }) => {
  await page.getByLabel("Name").fill("Mr Rajan");
  await page.getByLabel("Postal").fill("318993");
  await page.getByRole("button", { name: "Open the chat" }).click();

  const composer = page.getByLabel("Message");
  await expect(composer).toBeEnabled();
  await composer.fill("Saturday morning works for me.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("button", { name: "Suggest another time" })).toBeVisible({
    timeout: 30_000,
  });

  await page.getByRole("button", { name: "Suggest another time" }).click();
  await expect(page.getByText("Route-friendly options (3)")).toBeVisible({ timeout: 30_000 });

  const trace = page.getByRole("button", { name: /function calls & results/ }).last();
  await trace.click();
  await expect(page.getByText("find_fallback_options", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();

  const choices = page.getByRole("button", { name: /^Confirm / });
  await expect(choices).toHaveCount(3);
  await choices.nth(1).click();

  await expect(page.getByText("Confirmed & locked")).toBeVisible({ timeout: 30_000 });
  await expect(
    page.getByRole("heading", { name: "Appointment locked. The route has been republished." }),
  ).toBeVisible();
  await expect(page.getByText(/plan v2/)).toBeVisible();
  await expect(page.getByText("Every other customer on this day kept the window they were promised.")).toBeVisible();
  await expect(page.getByText("Route-friendly options (3)")).toHaveCount(0);

  await page.reload();
  await expect(page.getByText("Confirmed & locked")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/plan v2/)).toBeVisible();
  await expect(page.getByText("before", { exact: true })).toBeVisible();
  await expect(page.getByText("Route-friendly options (3)")).toHaveCount(0);
});
