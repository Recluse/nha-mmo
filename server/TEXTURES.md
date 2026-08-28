# 3D body textures (World tab)

Real, self-hosted, same-origin equirectangular maps used by the three.js World tab.

| File | Body | Source | License |
|---|---|---|---|
| `moon.jpg` | Moon | NASA-derived lunar map | public domain |
| `mars.jpg` | Mars | Solar System Scope (`2k_mars`) — NASA/Viking-derived | **CC-BY 4.0** |
| `venus.jpg` | Venus | Solar System Scope (`2k_venus_surface`) — radar-colorized | **CC-BY 4.0** |
| `phobos.jpg` | Phobos | *(not yet installed — tinted-sphere fallback; USGS Astropedia mosaic, public domain, when added)* | public domain |
| `deimos.jpg` | Deimos | *(not yet installed — tinted-sphere fallback; P. Stooke / USGS, public domain, when added)* | public domain |

CC-BY attribution shown in the World tab: *"Mars & Venus textures © Solar System Scope, CC-BY 4.0."*
Served via the allowlisted `GET /tex/{body}.jpg` route (`server/app.py`); a missing file 404s and the World tab shows the tinted fallback sphere.
