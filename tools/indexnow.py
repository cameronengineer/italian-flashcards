"""Tell Bing and the other IndexNow search engines about every page of the website.

Run it by hand after a site change has been deployed (Render redeploys on push):

    python3 tools/indexnow.py

It reads the live sitemap and submits all its pages. The key file
``site/<KEY>.txt`` proves the site is ours, so it must be live first.
IndexNow covers Bing (and through it DuckDuckGo and Yahoo), Yandex, Seznam
and Naver; Google uses Search Console instead.
"""

import json
import re
import urllib.request

ORIGIN = "https://italianflashcards.cameronengineer.com"
KEY = "5cf6212b1f7cd971d492a98c9c55d050"
ENDPOINT = "https://api.indexnow.org/indexnow"


def pages() -> list[str]:
    with urllib.request.urlopen(ORIGIN + "/sitemap.xml", timeout=30) as response:
        return re.findall(r"<loc>([^<]+)</loc>", response.read().decode())


def main() -> int:
    with urllib.request.urlopen(f"{ORIGIN}/{KEY}.txt", timeout=30) as response:
        if response.read().decode().strip() != KEY:
            raise SystemExit(f"{ORIGIN}/{KEY}.txt is not live yet: deploy the site first")
    urls = pages()
    body = {
        "host": ORIGIN.removeprefix("https://"),
        "key": KEY,
        "keyLocation": f"{ORIGIN}/{KEY}.txt",
        "urlList": urls,
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(f"IndexNow: {response.status} · {len(urls)} pages submitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
