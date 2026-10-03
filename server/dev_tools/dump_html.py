import asyncio
import sqlite3
import uuid
from playwright.async_api import async_playwright

DB_PATH = "orb.db"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("http://localhost:5173/")
        await page.get_by_role("button", name="Clinician").click()
        await page.get_by_role("button", name="Dr. Meera Sharma").click()
        await page.locator("text=Messages").first.click()
        await page.locator(".relay-channel-item", has_text="Manoj Tiwari").first.click()
        await page.wait_for_timeout(2000)
        html = await page.content()
        with open("sidebar_dump.html", "w") as f:
            f.write(html)
        await browser.close()

asyncio.run(main())
