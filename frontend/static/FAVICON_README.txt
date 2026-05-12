Place your favicon file here:

  favicon.ico   — used by most browsers (recommended, works everywhere)
  favicon.png   — fallback for browsers that prefer PNG

The browser tries favicon.ico first, then favicon.png.
Both are referenced in index.html already — just drop your file in and rebuild:

  docker compose up -d --build frontend

To swap back to the original, just replace the file and rebuild again.
No code changes needed.
