import type { Page } from "@playwright/test";

/** Open a workspace module from the landing page cards (the admin UI has no side navigation). */
export async function openModule(page: Page, name: string) {
  if (new URL(page.url()).pathname !== "/") await page.locator("a.brand").click();
  await page.locator(".module-card .title", { hasText: name }).click();
}
