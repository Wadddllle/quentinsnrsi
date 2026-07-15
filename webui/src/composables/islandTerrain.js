// Memoized fetch of the prototype coarse whole-island terrain heightfield --
// see sbg/topo/island_terrain.py for why this is a raw binary payload
// instead of CityJSON (avoids the ~540MB/~70s cost a triangulated JSON
// terrain was measured at). Mirrors fullIslandData.js's memoization pattern.
//
// Wire format (see island_terrain.py's pack_terrain_binary):
//   header (32 bytes): ncols:i32, nrows:i32, xmin:f64, ymax:f64, step:f64
//   body: nrows*ncols float32 heights, row-major, row 0 = north edge, NaN
//         for masked/no-data cells (passes through as real IEEE754 NaN --
//         Float32Array reads it back as NaN natively).

let prefetchPromise = null;

export function getIslandTerrain() {
	if (!prefetchPromise) {
		prefetchPromise = fetch('/api/dataset/terrain').then((res) => {
			if (res.status === 404) return null; // dtm.tif not built server-side, skip cleanly
			if (!res.ok) throw new Error(`terrain fetch failed: ${res.status}`);
			return res.arrayBuffer();
		}).then((buf) => {
			if (!buf) return null;
			const view = new DataView(buf);
			const ncols = view.getInt32(0, true);
			const nrows = view.getInt32(4, true);
			const xmin = view.getFloat64(8, true);
			const ymax = view.getFloat64(16, true);
			const step = view.getFloat64(24, true);
			const heights = new Float32Array(buf, 32);
			return { ncols, nrows, xmin, ymax, step, heights };
		});
	}
	return prefetchPromise;
}
