# 3D body textures (World tab)

Real, self-hosted, same-origin equirectangular maps used by the three.js World tab.

| File | Body | Source | License |
|---|---|---|---|
| `moon.jpg` | Moon | NASA-derived lunar map | public domain |
| `mars.jpg` | Mars | Solar System Scope (`2k_mars`) — NASA/Viking-derived | **CC-BY 4.0** |
| `venus.jpg` | Venus | Solar System Scope (`2k_venus_surface`) — radar-colorized | **CC-BY 4.0** |
| `phobos.jpg` | Phobos | Viking Mosaic (DLR-controlled), Wikimedia Commons — NASA/Viking-derived, downscaled 7200×3600→512×256 | **public domain** |
| `deimos.jpg` | Deimos | "Deimos color map" (Askaniy), Wikimedia Commons, downscaled 1264×632→512×256 | **CC BY-SA 3.0** |

CC attribution shown in the World tab: *"Mars & Venus textures © Solar System Scope, CC-BY 4.0."* — Phobos: NASA/Viking (PD); Deimos color map by Askaniy, CC BY-SA 3.0 (Wikimedia Commons).
Served via the allowlisted `GET /tex/{body}.jpg` route (`server/app.py`); a missing file 404s and the World tab shows the tinted fallback sphere.
