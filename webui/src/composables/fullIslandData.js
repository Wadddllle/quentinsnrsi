// Memoized fetch of the full island's 3D buildings dataset -- lets App.vue
// kick this off eagerly in the background as soon as the app loads (while
// the user is still getting oriented in 2D, per direct user request: "maybe
// it can start loading earlier instead of waiting for me to click 3D...
// peacefully load in back"), without SbgViewer3D re-fetching the same 168MB
// payload when it actually mounts on first "3D" click. The first caller
// starts the real fetch; every caller (whether or not it's resolved yet)
// gets the same promise.
//
// Deliberately NOT wrapped in a dedicated Worker (tried it, reverted it):
// moving fetch+JSON.parse off the main thread sounds like a free win, but
// the parsed object still has to reach the main thread eventually (Vue/
// ThreeJsViewer need it there), and that hand-off is itself a full
// structuredClone -- for an object graph this size, roughly as expensive as
// the JSON.parse it was meant to replace. Measured in real browser use: it
// added a new ~5s main-thread freeze on top of the pipeline's existing
// costs rather than removing one. Plain fetch().then(res => res.json()) is
// simpler and, going by real timing, no worse.
export const FULL_ISLAND_BBOX = { xmin: 2000, ymin: 22000, xmax: 50500, ymax: 50500 };

let prefetchPromise = null;

export function getFullIslandCitymodel() {
	if (!prefetchPromise) {
		const { xmin, ymin, xmax, ymax } = FULL_ISLAND_BBOX;
		prefetchPromise = fetch(`/api/dataset/buildings?bbox=${xmin},${ymin},${xmax},${ymax}`).then((res) => {
			if (!res.ok) throw new Error(`dataset fetch failed: ${res.status}`);
			return res.json();
		});
	}
	return prefetchPromise;
}
