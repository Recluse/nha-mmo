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

## Season 7 — the outer system

| File | Body | Source | License |
|---|---|---|---|
| `saturn.jpg` | Saturn | Solar System Scope (`2k_saturn`) | **CC-BY 4.0** |
| `saturn_ring.png` | Saturn's rings | Solar System Scope (`2k_saturn_ring_alpha`) — a RADIAL strip with real alpha | **CC-BY 4.0** |
| `neptune.jpg` | Neptune | Solar System Scope (`2k_neptune`) | **CC-BY 4.0** |
| `enceladus.jpg` | Enceladus | "Enceladus Color Map" (Cassini ISS, Dr. Paul Schenk / LPI), Wikimedia Commons | **public domain (NASA)** |
| `titan.jpg` | Titan | "Map of Titan cropped" (Cassini ISS mosaic), Wikimedia Commons | **public domain (NASA)** |
| `triton.jpg` | Triton | "Triton map no grid" (Voyager 2, Dr. Paul Schenk / LPI), Wikimedia Commons | **public domain (NASA)** |

Notes on two of them, because both are edited and that should not be a surprise to the next reader:

- **Titan is greyscale.** Cassini could only see the surface through the haze in the near-infrared, so the map has
  no colour. The dashboard therefore does NOT reset the material to white for Titan (`keepTint`) — the amber tint
  multiplies the greyscale map, which is how the one orange world in the solar system avoids looking like a dirty
  snowball.
- **Triton's map has a hole.** Voyager 2 photographed a single hemisphere in 1989 and nothing has been back, so
  38% of the source image is literally black. Ours fills that region by nearest-column sampling and then blurs the
  fill heavily, feathered at the seam, so the unimaged cap reads as a smooth pale region instead of a hole punched
  through the globe. It is a plausible fill, not data.

All six are downscaled to 512x256 (Saturn 1024x512, the ring strip 512x24) and re-encoded — together they add ~104 KB,
which matters: `server/static` ships as a ConfigMap and the deploy aborts above 1,000,000 bytes of base64.
