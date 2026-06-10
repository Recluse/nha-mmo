"""Generate a seamless, tileable ground-detail texture for the NHA 3D terrain.

Two seamless ingredients, both periodic so the result tiles with no visible seams:
  * spectral-synthesis fractal bands (1/f^beta via FFT) for large/mid/fine relief, and
  * tileable Worley/cellular noise (toroidal feature points) for pebble highlights and a
    crack network between cells — what gives it a rocky, Moon-like read rather than smoke.
Combined, contrast-punched and unsharp-masked for crisp micro-detail.

Output: server/ground.jpg (grayscale). The terrain shader tiles it across the map, tints it
by the biome vertex color, and uses its gradient for cheap bump relief.
Re-run with: python scripts/gen-ground-texture.py
"""
import numpy as np
from PIL import Image, ImageFilter

N = 512
rng = np.random.default_rng(7)
fy = np.fft.fftfreq(N)[:, None]
fx = np.fft.fftfreq(N)[None, :]
freq = np.sqrt(fx * fx + fy * fy)
freq[0, 0] = 1e-6


def band(beta):
    """One normalized 1/f^beta fractal-noise band (periodic -> seamless)."""
    f = np.fft.fft2(rng.standard_normal((N, N))) * (freq ** (-beta))
    a = np.fft.ifft2(f).real
    return (a - a.min()) / (a.max() - a.min() + 1e-9)


def worley(gn, seed):
    """Tileable Worley noise: distance to the nearest jittered cell point, wrapped
    toroidally (feature lookup mod gn while position stays unwrapped) so it tiles."""
    r = np.random.default_rng(seed)
    feat = r.random((gn, gn, 2))
    ys, xs = np.mgrid[0:N, 0:N].astype(float)
    gx, gy = xs / N * gn, ys / N * gn
    cx, cy = np.floor(gx).astype(int), np.floor(gy).astype(int)
    best = np.full((N, N), 1e9)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            ncx, ncy = cx + dx, cy + dy
            fxp = feat[ncy % gn, ncx % gn, 0] + ncx
            fyp = feat[ncy % gn, ncx % gn, 1] + ncy
            best = np.minimum(best, (gx - fxp) ** 2 + (gy - fyp) ** 2)
    w = np.sqrt(best)
    return (w - w.min()) / (w.max() - w.min() + 1e-9)


mid, fine = band(1.7), band(0.9)
w1, w2 = worley(13, 11), worley(27, 12)
rock = (1.0 - w1) * 0.5 + (1.0 - w2) * 0.5        # bright pebble cores + dark crack network

# NO low-frequency band: a tiled detail map must stay roughly uniform, or the big blobs
# repeat visibly. Mid/fine relief + Worley rocks only, then center the mean so it modulates evenly.
img = mid * 0.16 + fine * 0.18 + rock * 0.5
img = (img - img.min()) / (img.max() - img.min())
img = img - img.mean() + 0.5
img = np.clip((img - 0.5) * 1.25 + 0.5, 0, 1)     # gentle contrast — a detail map, not extreme black/white
arr = (img * 255).astype(np.uint8)

im = Image.fromarray(arr, "L").filter(
    ImageFilter.UnsharpMask(radius=2, percent=130, threshold=2)
).convert("RGB")
im.save("server/ground.jpg", quality=80, optimize=True)
print(f"wrote server/ground.jpg  {N}x{N}")
