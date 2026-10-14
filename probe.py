"""Connectivity check: can this machine reach x.com at all? Uses NO cookies or secrets."""
import asyncio

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
URL = "https://x.com/tesla"


def show(name, status, text):
    extra = "" if status == 200 else " | " + " ".join(text[:200].split())
    print(f"  {name}: HTTP {status}{extra}")


async def main() -> None:
    print("Connectivity check to x.com (logged out, no cookies):")
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome") as s:
            r = await s.get(URL, timeout=20)
            show("curl_cffi", r.status_code, r.text)
    except Exception as e:
        print(f"  curl_cffi: {type(e).__name__}: {e}")
    try:
        async with httpx.AsyncClient(headers={"user-agent": UA}, timeout=20, follow_redirects=True) as c:
            r = await c.get(URL)
            show("httpx", r.status_code, r.text)
    except Exception as e:
        print(f"  httpx: {type(e).__name__}: {e}")


asyncio.run(main())
