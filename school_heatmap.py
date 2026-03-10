"""
Myanmar Formal Sector School Locations – 2019
Merges Lower and Upper Myanmar CSV datasets, then produces:
  1. merged_schools_myanmar_2019.csv          – combined data file
  2. heatmap_static_full.png                  – full-country KDE heatmap
  3. heatmap_static_by_region.png             – per-state/region KDE facets
  4. heatmap_static_urbanrural.png            – urban vs rural side-by-side
  5. heatmap_interactive.html                 – interactive Folium heatmap

Optimisations vs first draft:
  - Web Mercator projection done in pure NumPy (no per-row GeoDataFrame)
  - KDE grids capped at 250 × 250
  - KDE fitted on a random subsample (≤ 15 000 pts) then evaluated on grid
  - Basemap tile fetch wrapped so failures are non-fatal
"""

import warnings

warnings.filterwarnings("ignore")

import math
import os

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import contextily as ctx
import folium
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from folium.plugins import HeatMap
from matplotlib.gridspec import GridSpec
from scipy.stats import gaussian_kde

# ─────────────────────────────────────────────────────────────────────────────
# 0.  Paths
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOWER_CSV = os.path.join(
    BASE_DIR, "formal_sector_school_location_lowermyanmar_2019.csv"
)
UPPER_CSV = os.path.join(
    BASE_DIR, "formal_sector_school_location_uppermyanmar_2019.csv"
)
MERGED_CSV = os.path.join(BASE_DIR, "merged_schools_myanmar_2019.csv")
OUT_FULL = os.path.join(BASE_DIR, "heatmap_static_full.png")
OUT_REGION = os.path.join(BASE_DIR, "heatmap_static_by_region.png")
OUT_UR = os.path.join(BASE_DIR, "heatmap_static_urbanrural.png")
OUT_HTML = os.path.join(BASE_DIR, "heatmap_interactive.html")

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Fast coordinate helpers  (pure NumPy – no GeoDataFrame overhead)
# ─────────────────────────────────────────────────────────────────────────────
_R = 6_378_137.0  # WGS-84 semi-major axis (metres)


def lonlat_to_webmercator(lon, lat):
    """Vectorised WGS-84 → Web Mercator (EPSG:3857)."""
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    lat = np.clip(lat, -85.051129, 85.051129)
    x = lon * math.pi / 180.0 * _R
    y = np.log(np.tan(math.pi / 4.0 + lat * math.pi / 360.0)) * _R
    return x, y


def wm_extent(lon_min, lon_max, lat_min, lat_max):
    """Return Web Mercator bounding box [xmin, xmax, ymin, ymax]."""
    xs, ys = lonlat_to_webmercator([lon_min, lon_max], [lat_min, lat_max])
    return xs[0], xs[1], ys[0], ys[1]


# ─────────────────────────────────────────────────────────────────────────────
# 2.  KDE helper
# ─────────────────────────────────────────────────────────────────────────────
KDE_SAMPLE = 15_000  # max points used to *fit* the KDE
KDE_GRID = 250  # grid resolution per axis


def compute_kde_wm(lons, lats, grid_pts=KDE_GRID, pad=0.3, sample=KDE_SAMPLE, rng=None):
    """
    Fit a 2-D Gaussian KDE in *lon/lat* space on a subsample,
    evaluate on a regular grid, then return everything in Web Mercator
    so it can be plotted directly alongside contextily tiles.

    Returns
    -------
    xx_wm, yy_wm : 2-D arrays  (Web Mercator grid)
    zz           : 2-D density array  (same shape)
    extent_ll    : (lon_min, lon_max, lat_min, lat_max) in lon/lat
    """
    rng = rng or np.random.default_rng(42)
    lons = np.asarray(lons, dtype=np.float64)
    lats = np.asarray(lats, dtype=np.float64)

    # subsample for fitting
    if len(lons) > sample:
        idx = rng.choice(len(lons), size=sample, replace=False)
        fit_lons, fit_lats = lons[idx], lats[idx]
    else:
        fit_lons, fit_lats = lons, lats

    # bounding box
    lon_min, lon_max = lons.min() - pad, lons.max() + pad
    lat_min, lat_max = lats.min() - pad, lats.max() + pad

    # build grid in lon/lat, evaluate KDE
    xi = np.linspace(lon_min, lon_max, grid_pts)
    yi = np.linspace(lat_min, lat_max, grid_pts)
    xx, yy = np.meshgrid(xi, yi)

    kernel = gaussian_kde(np.vstack([fit_lons, fit_lats]), bw_method="scott")
    zz = kernel(np.vstack([xx.ravel(), yy.ravel()])).reshape(xx.shape)

    # project grid to Web Mercator
    xx_wm, yy_wm = lonlat_to_webmercator(xx, yy)
    return xx_wm, yy_wm, zz, (lon_min, lon_max, lat_min, lat_max)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Transparent colourmap helper
# ─────────────────────────────────────────────────────────────────────────────
def transparent_cmap(name, fade=40):
    """Return a copy of *name* with the low-density end made transparent."""
    base = matplotlib.colormaps.get_cmap(name)
    colors = base(np.arange(base.N))
    colors[:fade, -1] = np.linspace(0, 1, fade)
    return mcolors.ListedColormap(colors)


CMAP_FULL = transparent_cmap("inferno", fade=35)
CMAP_URBAN = transparent_cmap("plasma", fade=35)
CMAP_RURAL = transparent_cmap("viridis", fade=35)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Basemap helper
# ─────────────────────────────────────────────────────────────────────────────
def add_basemap(ax, source=None, zoom="auto"):
    try:
        src = source or ctx.providers.CartoDB.DarkMatter
        ctx.add_basemap(ax, crs="EPSG:3857", source=src, attribution=False, zoom=zoom)
    except Exception as exc:
        print(f"    [warn] basemap skipped: {exc}")
        ax.set_facecolor("#111111")


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Shared plot utility
# ─────────────────────────────────────────────────────────────────────────────
def plot_kde_on_ax(
    ax,
    xx_wm,
    yy_wm,
    zz,
    cmap,
    pts_lon=None,
    pts_lat=None,
    dot_size=0.4,
    dot_alpha=0.08,
    add_map=True,
    zoom="auto",
):
    """Render KDE surface (and optional scatter dots) onto *ax* in Web Mercator."""
    if add_map:
        add_basemap(ax, zoom=zoom)

    pcm = ax.pcolormesh(xx_wm, yy_wm, zz, cmap=cmap, shading="gouraud", zorder=2)

    if pts_lon is not None and pts_lat is not None:
        px, py = lonlat_to_webmercator(pts_lon, pts_lat)
        ax.scatter(
            px, py, s=dot_size, c="white", alpha=dot_alpha, linewidths=0, zorder=3
        )

    ax.set_xlim(xx_wm.min(), xx_wm.max())
    ax.set_ylim(yy_wm.min(), yy_wm.max())
    ax.set_axis_off()
    return pcm


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 – Load & merge
# ─────────────────────────────────────────────────────────────────────────────
print("=" * 60)
print("Step 1 – Loading & merging CSV files …")

lower = pd.read_csv(LOWER_CSV, low_memory=False)
upper = pd.read_csv(UPPER_CSV, low_memory=False)

lower["source_region"] = "Lower Myanmar"
upper["source_region"] = "Upper Myanmar"

merged = pd.concat([lower, upper], ignore_index=True)
merged = merged.rename(columns={"longx": "longitude", "laty": "latitude"})

# Drop rows with missing or out-of-bounds coordinates
merged = merged.dropna(subset=["longitude", "latitude"])
merged = merged[
    merged["longitude"].between(92.0, 102.0) & merged["latitude"].between(9.5, 29.0)
].reset_index(drop=True)

merged.to_csv(MERGED_CSV, index=False, encoding="utf-8-sig")

print(f"  Lower Myanmar : {len(lower):>7,} schools")
print(f"  Upper Myanmar : {len(upper):>7,} schools")
print(f"  Merged total  : {len(merged):>7,} schools")
print(f"  Saved  →  {MERGED_CSV}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 – Static heatmap: full country
# ─────────────────────────────────────────────────────────────────────────────
print("\nStep 2 – Full-country static heatmap …")

lons_all = merged["longitude"].values
lats_all = merged["latitude"].values

xx_wm, yy_wm, zz, ext_ll = compute_kde_wm(
    lons_all, lats_all, grid_pts=KDE_GRID, pad=0.5
)

fig, ax = plt.subplots(figsize=(11, 16))
fig.patch.set_facecolor("#0d1117")
ax.set_facecolor("#0d1117")

pcm = plot_kde_on_ax(
    ax,
    xx_wm,
    yy_wm,
    zz,
    CMAP_FULL,
    pts_lon=lons_all,
    pts_lat=lats_all,
    dot_size=0.3,
    dot_alpha=0.06,
    zoom=6,
)

cbar = fig.colorbar(pcm, ax=ax, fraction=0.028, pad=0.02)
cbar.set_label("School Density (KDE)", color="white", fontsize=11)
cbar.ax.yaxis.set_tick_params(color="white")
plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")
cbar.outline.set_edgecolor("#555")

fig.suptitle(
    "Myanmar – Formal Sector School Density  (2019)\n"
    f"{len(merged):,} schools  ·  Lower + Upper Myanmar combined",
    color="white",
    fontsize=14,
    fontweight="bold",
    y=0.97,
)
fig.tight_layout()
fig.savefig(OUT_FULL, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(f"  Saved  →  {OUT_FULL}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 – Static heatmap: per State / Region facets
# ─────────────────────────────────────────────────────────────────────────────
print("\nStep 3 – Per-region facet heatmap …")

region_counts = merged["mm_srname"].value_counts()
top_regions = sorted(region_counts[region_counts >= 300].index.tolist())

N_COLS = 4
N_ROWS = math.ceil(len(top_regions) / N_COLS)

fig, axes = plt.subplots(N_ROWS, N_COLS, figsize=(5.5 * N_COLS, 5.5 * N_ROWS))
fig.patch.set_facecolor("#0d1117")

for ax in axes.ravel():
    ax.set_facecolor("#0d1117")
    ax.set_axis_off()

for idx, region in enumerate(top_regions):
    ax = axes.ravel()[idx]
    sub = merged[merged["mm_srname"] == region]

    ax.set_axis_on()
    ax.set_facecolor("#0d1117")
    for sp in ax.spines.values():
        sp.set_edgecolor("#333")

    try:
        xx_r, yy_r, zz_r, _ = compute_kde_wm(
            sub["longitude"].values,
            sub["latitude"].values,
            grid_pts=180,
            pad=0.25,
            sample=10_000,
        )
        plot_kde_on_ax(
            ax,
            xx_r,
            yy_r,
            zz_r,
            CMAP_FULL,
            pts_lon=sub["longitude"].values,
            pts_lat=sub["latitude"].values,
            dot_size=1.0,
            dot_alpha=0.15,
            zoom=7,
        )
    except Exception as exc:
        ax.text(
            0.5,
            0.5,
            f"KDE failed\n{exc}",
            ha="center",
            va="center",
            color="#888",
            transform=ax.transAxes,
            fontsize=8,
        )

    ax.set_title(f"{region}\n({len(sub):,} schools)", color="white", fontsize=9, pad=4)
    ax.set_axis_off()

fig.suptitle(
    "Myanmar – School Density by State / Region  (2019)",
    color="white",
    fontsize=16,
    fontweight="bold",
    y=1.005,
)
fig.tight_layout()
fig.savefig(OUT_REGION, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(f"  Saved  →  {OUT_REGION}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 – Static heatmap: Urban vs Rural
# ─────────────────────────────────────────────────────────────────────────────
print("\nStep 4 – Urban vs Rural heatmap …")

groups = {
    "Urban": (merged[merged["urbanrural"] == "Urban"], CMAP_URBAN),
    "Rural": (merged[merged["urbanrural"] == "Rural"], CMAP_RURAL),
}

fig, axes = plt.subplots(1, 2, figsize=(22, 16))
fig.patch.set_facecolor("#0d1117")

for ax, (label, (sub, cmap)) in zip(axes, groups.items()):
    ax.set_facecolor("#0d1117")

    xx_u, yy_u, zz_u, _ = compute_kde_wm(
        sub["longitude"].values,
        sub["latitude"].values,
        grid_pts=KDE_GRID,
        pad=0.4,
    )
    pcm = plot_kde_on_ax(
        ax,
        xx_u,
        yy_u,
        zz_u,
        cmap,
        pts_lon=sub["longitude"].values,
        pts_lat=sub["latitude"].values,
        dot_size=0.3,
        dot_alpha=0.07,
        zoom=6,
    )

    cbar = fig.colorbar(pcm, ax=ax, fraction=0.028, pad=0.02)
    cbar.set_label("School Density (KDE)", color="white", fontsize=11)
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")
    cbar.outline.set_edgecolor("#555")

    ax.set_title(
        f"{label} Schools\n({len(sub):,} schools)",
        color="white",
        fontsize=14,
        fontweight="bold",
        pad=10,
    )

fig.suptitle(
    "Myanmar – School Density: Urban vs Rural  (2019)",
    color="white",
    fontsize=16,
    fontweight="bold",
    y=1.01,
)
fig.tight_layout()
fig.savefig(OUT_UR, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(f"  Saved  →  {OUT_UR}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 – Interactive Folium heatmap
# ─────────────────────────────────────────────────────────────────────────────
print("\nStep 5 – Interactive Folium heatmap …")

center_lat = float(merged["latitude"].mean())
center_lon = float(merged["longitude"].mean())

m = folium.Map(location=[center_lat, center_lon], zoom_start=6, tiles=None)

# ── Basemap tiles ─────────────────────────────────────────────────────────────
folium.TileLayer("CartoDB dark_matter", name="Dark (default)", control=True).add_to(m)
folium.TileLayer("CartoDB positron", name="Light", control=True).add_to(m)
folium.TileLayer("OpenStreetMap", name="OpenStreetMap", control=True).add_to(m)

# ── Colour gradients ──────────────────────────────────────────────────────────
GRAD_ALL = {
    "0.2": "#000080",
    "0.4": "#0000ff",
    "0.55": "#00ffff",
    "0.75": "#ffff00",
    "1.0": "#ff0000",
}
GRAD_URBAN = {
    "0.2": "#2d0057",
    "0.45": "#7b00d4",
    "0.7": "#ff00ff",
    "0.9": "#ffaaff",
    "1.0": "#ffffff",
}
GRAD_RURAL = {
    "0.2": "#001a00",
    "0.45": "#006600",
    "0.7": "#00ff00",
    "0.9": "#aaff00",
    "1.0": "#ffff00",
}

# ── All schools layer ─────────────────────────────────────────────────────────
HeatMap(
    merged[["latitude", "longitude"]].values.tolist(),
    name="All Schools",
    radius=10,
    blur=14,
    max_zoom=13,
    min_opacity=0.3,
    gradient=GRAD_ALL,
).add_to(m)

# ── Urban layer ───────────────────────────────────────────────────────────────
urban_grp = folium.FeatureGroup(name="Urban Schools Only", show=False)
HeatMap(
    merged[merged["urbanrural"] == "Urban"][["latitude", "longitude"]].values.tolist(),
    radius=12,
    blur=16,
    max_zoom=13,
    min_opacity=0.35,
    gradient=GRAD_URBAN,
).add_to(urban_grp)
urban_grp.add_to(m)

# ── Rural layer ───────────────────────────────────────────────────────────────
rural_grp = folium.FeatureGroup(name="Rural Schools Only", show=False)
HeatMap(
    merged[merged["urbanrural"] == "Rural"][["latitude", "longitude"]].values.tolist(),
    radius=9,
    blur=12,
    max_zoom=13,
    min_opacity=0.3,
    gradient=GRAD_RURAL,
).add_to(rural_grp)
rural_grp.add_to(m)

# ── Per-region layers ─────────────────────────────────────────────────────────
PALETTE = [
    "#ff4444",
    "#ff8800",
    "#ffdd00",
    "#88ff00",
    "#00ffcc",
    "#00ccff",
    "#4488ff",
    "#aa44ff",
    "#ff44aa",
    "#ff6666",
    "#ffaa44",
    "#aaff44",
    "#44ffee",
    "#44aaff",
    "#8844ff",
]
for i, region in enumerate(top_regions):
    sub_r = merged[merged["mm_srname"] == region]
    colour = PALETTE[i % len(PALETTE)]
    rg = folium.FeatureGroup(name=f"Region: {region} ({len(sub_r):,})", show=False)
    HeatMap(
        sub_r[["latitude", "longitude"]].values.tolist(),
        radius=13,
        blur=18,
        max_zoom=14,
        min_opacity=0.4,
        gradient={"0.3": "#000000", "0.65": colour, "1.0": "#ffffff"},
    ).add_to(rg)
    rg.add_to(m)

# ── Individual school markers (sampled) ───────────────────────────────────────
SAMPLE_N = min(2_000, len(merged))
sample_df = merged.sample(n=SAMPLE_N, random_state=42)

marker_grp = folium.FeatureGroup(
    name=f"School Markers (sample {SAMPLE_N:,})", show=False
)
for _, row in sample_df.iterrows():
    colour = "#ff6600" if row["urbanrural"] == "Urban" else "#22aa55"
    folium.CircleMarker(
        location=[row["latitude"], row["longitude"]],
        radius=4,
        color=colour,
        fill=True,
        fill_color=colour,
        fill_opacity=0.8,
        weight=0.5,
        tooltip=folium.Tooltip(
            f"<b>{row['schoolname']}</b><br>"
            f"Type: {row['urbanrural']}<br>"
            f"State/Region: {row['mm_srname']}<br>"
            f"District: {row['mm_dtname']}<br>"
            f"Township: {row['mm_tsname']}<br>"
            f"Coords: {row['latitude']:.4f}, {row['longitude']:.4f}"
        ),
    ).add_to(marker_grp)
marker_grp.add_to(m)

# ── Legend ────────────────────────────────────────────────────────────────────
legend_html = """
<div style="
    position:fixed; bottom:30px; left:30px; z-index:1000;
    background:rgba(13,17,23,0.88); border:1px solid #444;
    border-radius:8px; padding:14px 18px;
    font-family:Arial,sans-serif; font-size:13px; color:#eee;
    min-width:230px; box-shadow:0 2px 12px rgba(0,0,0,.6);">
  <b style="font-size:15px;">Myanmar School Locations</b>
  <hr style="border-color:#444;margin:8px 0">
  <div><span style="color:#ff6600;">&#9679;</span>&nbsp; Urban school</div>
  <div><span style="color:#22aa55;">&#9679;</span>&nbsp; Rural school</div>
  <hr style="border-color:#444;margin:8px 0">
  <div style="font-size:12px;color:#aaa;">
    Heatmap colour scale (all schools)<br>
    <span style="color:#0000ff;">&#9632;</span> Low &nbsp;&#8594;&nbsp;
    <span style="color:#ff0000;">&#9632;</span> High density
  </div>
  <hr style="border-color:#444;margin:8px 0">
  <div style="font-size:11px;color:#888;">
    Total&nbsp;schools:&nbsp;{total:,}<br>
    Lower&nbsp;Myanmar:&nbsp;{lower_n:,}<br>
    Upper&nbsp;Myanmar:&nbsp;{upper_n:,}
  </div>
</div>
""".format(total=len(merged), lower_n=len(lower), upper_n=len(upper))

m.get_root().html.add_child(folium.Element(legend_html))

# ── Title banner ──────────────────────────────────────────────────────────────
title_html = """
<div style="
    position:fixed; top:12px; left:50%; transform:translateX(-50%);
    z-index:1000; background:rgba(13,17,23,0.85);
    border:1px solid #555; border-radius:6px;
    padding:8px 24px; font-family:Arial,sans-serif;
    font-size:16px; font-weight:bold; color:#f0f0f0;
    pointer-events:none;">
  Myanmar Formal Sector School Density &mdash; 2019
</div>
"""
m.get_root().html.add_child(folium.Element(title_html))

folium.LayerControl(collapsed=False, position="topright").add_to(m)
m.save(OUT_HTML)
print(f"  Saved  →  {OUT_HTML}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 – Summary statistics
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"Total schools        : {len(merged):,}")
print(f"Lower Myanmar        : {len(lower):,}")
print(f"Upper Myanmar        : {len(upper):,}")

print("\nUrban / Rural split:")
for label, cnt in merged["urbanrural"].value_counts().items():
    print(f"  {label:<10}: {cnt:>7,}  ({cnt / len(merged) * 100:.1f}%)")

print("\nTop 10 states / regions by school count:")
for region, cnt in merged["mm_srname"].value_counts().head(10).items():
    print(f"  {region:<22}: {cnt:>6,}")

print("\nTop 10 districts by school count:")
for district, cnt in merged["mm_dtname"].value_counts().head(10).items():
    print(f"  {district:<27}: {cnt:>6,}")

print("\nOutput files:")
for f in [MERGED_CSV, OUT_FULL, OUT_REGION, OUT_UR, OUT_HTML]:
    kb = os.path.getsize(f) / 1024 if os.path.exists(f) else 0
    print(f"  {os.path.basename(f):<47}  {kb:>8.1f} KB")

print("=" * 60)
print("Done.")
