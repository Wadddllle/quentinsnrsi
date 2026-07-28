<template>
  <div
    id="viewer"
    class="col-12 px-0 h-100"
  ></div>
</template>

<script>
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls';
import { AttributeEvaluator, CityJSONLoader, CityJSONParser, CityJSONWorkerParser, CityObjectsMaterial, TextureManager } from 'cityjson-threejs-loader';
import { SRGBColorSpace } from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { GTAOPass, OutputPass, RenderPass } from 'three/examples/jsm/Addons.js';
import GUI from 'three/examples/jsm/libs/lil-gui.module.min.js';
// "Fat lines" for the boundary-ring/crossing-highlight overlays -- plain
// THREE.Line + LineBasicMaterial's `linewidth` is a documented WebGL no-op
// on most platforms/drivers (always renders 1px regardless of the value
// set), confirmed by direct visual testing here: the ring/highlights WERE
// in the scene (verified via direct scene-graph inspection) but were only
// visible as faint dotted traces. Line2/LineMaterial renders real
// screen-space-pixel-width lines instead.
import { Line2 } from 'three/examples/jsm/lines/Line2.js';
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js';
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js';
// Prototype: coarse whole-island terrain overlay, for visual orientation
// only -- see sbg/topo/island_terrain.py and composables/islandTerrain.js.
import { getIslandTerrain } from '../../composables/islandTerrain';

export default {
	name: 'ThreeJsViewer',
	props: {
		citymodel: {
			type: Object,
			default: function () {

				return {};

			}
		},
		selectedObjid: {
			type: String,
			default: null
		},
		selectedGeomIdx: {
			type: Number,
			default: - 1,
		},
		selectedBoundaryIdx: {
			type: Number,
			default: - 1
		},
		highlightSelectedSurface: {
			type: Boolean,
			default: false
		},
		selectionColor: {
			type: Number,
			default: 0xffc107
		},
		objectColors: {
			type: Object,
			default: function () {

				return {
					"Building": 0x7497df,
					"BuildingPart": 0x7497df,
					"BuildingInstallation": 0x7497df,
					"Bridge": 0x999999,
					"BridgePart": 0x999999,
					"BridgeInstallation": 0x999999,
					"BridgeConstructionElement": 0x999999,
					"CityObjectGroup": 0xffffb3,
					"CityFurniture": 0xcc0000,
					"GenericCityObject": 0xcc0000,
					"LandUse": 0xffffb3,
					"PlantCover": 0x39ac39,
					"Railway": 0x000000,
					"Road": 0x999999,
					"SolitaryVegetationObject": 0x39ac39,
					"TINRelief": 0xffdb99,
					"TransportSquare": 0x999999,
					"Tunnel": 0x999999,
					"TunnelPart": 0x999999,
					"TunnelInstallation": 0x999999,
					"WaterBody": 0x4da6ff
				};

			}
		},
		surfaceColors: {
			type: Object,
			default: function () {

				return {
					"GroundSurface": 0x999999,
					"WallSurface": 0xffffff,
					"RoofSurface": 0xff0000,
					"TrafficArea": 0x6e6e6e,
					"AuxiliaryTrafficArea": 0x2c8200,
					"Window": 0x0059ff,
					"Door": 0x640000
				};

			}
		},
		backgroundColor: {
			type: Number,
			default: 0xd9eefc
		},
		showSemantics: {
			type: Boolean,
			default: true
		},
		doubleSide: {
			type: Boolean,
			// FrontSide (backface culling on), not the original DoubleSide --
			// real GPU win (the renderer discards ~half the fragments of
			// every closed solid before shading) for LoD1 box extrusions,
			// which are exactly what the current dataset is: every building
			// in data/sbg.city.json is our own deterministic
			// sbg/extrude.py output, not raw messy external mesh geometry,
			// so winding consistency isn't a real risk here the way it might
			// be for arbitrary imported meshes -- verified directly, not
			// assumed, given this session's own history of winding-order
			// red herrings (see the lighting-bug writeup above): rendered a
			// real footprint with a courtyard hole (extrude.py reverses the
			// hole ring for the bottom face but not the wall-quad winding,
			// so this was a genuine case worth checking, not a formality)
			// from four different close-in camera angles including directly
			// into the hole, FrontSide vs DoubleSide, byte-for-byte
			// identical renders at every angle -- our extrusion code
			// produces consistent winding. Revisit if Phase 1.6's real
			// OneMap mesh geometry (arbitrary external meshes, not our own
			// extrusion) ever gets re-enabled -- that path has no such
			// guarantee.
			default: false
		},
		ambientOcclusion: {
			type: Boolean,
			default: true
		},
		activeLod: {
			type: Number,
			default: - 1
		},
		cameraSpotlight: {
			type: Boolean,
			default: true
		},
		conditionalFormatting: {
			type: Boolean,
			default: false
		},
		conditionalAttribute: {
			type: String,
			default: null
		},
		attributeColors: {
			type: Object,
			default: () => {}
		},
		activeMaterialTheme: {
			type: String,
			default: "undefined"
		},
		textureManager: {
			type: TextureManager,
			default: undefined
		},
		activeTextureTheme: {
			type: String,
			default: "undefined"
		},
		// SbgViewer3D (Phase 1) reloads `citymodel` in the background as the
		// user pans, without the user asking to jump anywhere -- re-fitting
		// the camera on every one of those loads would fight the user's own
		// orbit/pan. Only the initial load and explicit "go to location"
		// loads should recenter the camera.
		autoFitOnLoad: {
			type: Boolean,
			default: true
		},
		// UX redesign (Phase 6, post-Phase-1): the domain boundary drawn in 2D
		// mode is read-only in 3D -- shown as a ground-level line loop, not an
		// editable/interactive object here. World [x, y] points, same shape the
		// 2D canvas's `ring` prop already uses.
		boundaryRing: {
			type: Array,
			default: () => []
		},
		// Buildings crossing the boundary, shown as a "warning" per the UX
		// redesign -- deliberately implemented as separate ground-level outline
		// loops (same low-risk pattern as boundaryRing) rather than recoloring
		// CityObjectsMesh's own materials, which would mean depending on
		// cityjson-threejs-loader's attribute-driven coloring/shader internals
		// under real time pressure. Array of ring-point-arrays (each already a
		// closed or open [x, y] loop), not building ids -- the caller (which
		// already has full footprint data loaded for the 2D pane) resolves ids
		// to rings before passing them in.
		highlightFootprintRings: {
			type: Array,
			default: () => []
		},
		// Phase 3 (remove-building): near-exact copy of highlightFootprintRings
		// above -- a removed building's real mesh is NOT hidden (see
		// SbgViewer3D.vue's own comment on why: cityjson-threejs-loader builds
		// one merged mesh per ~2000-object chunk, not one per building, so
		// there's no cheap per-building visibility toggle without new
		// shader/attribute work against undocumented library internals, the
		// same risk this project already avoided once for the crossing
		// highlight above). This just flags it with a distinct-colored outline.
		removedFootprintRings: {
			type: Array,
			default: () => []
		},
		// Phase 4b (add-building): a small self-contained mini-CityJSON
		// ({CityObjects, vertices, transform}, same shape subset_cityjson()
		// produces) covering just the buildings added THIS session -- lets a
		// just-added building show up immediately with its REAL geometry,
		// without waiting for the whole-island reload the main citymodel
		// would otherwise need (see project plan: cityjson-threejs-loader
		// builds one merged mesh per ~2000-object chunk, not one per
		// building, so there's no cheap way to patch the real mesh
		// in-place). Rendered through a second, small, synchronous
		// CityJSONLoader instance (see updateAddedBuildings below) instead
		// of approximating it as a box extrusion -- an earlier version did
		// that, which was exactly right for Path 1 (hypothetical footprint+
		// height IS a box) but visibly wrong for Path 2/3 (a real imported
		// building or uploaded mesh has actual shape a box can't represent),
		// per direct user report.
		addedCitymodel: {
			type: Object,
			default: null
		},
		// Phase 4c (OneMap review-gate): the footprint ring(s) of the
		// existing SBG building(s) currently under review for replacement --
		// near-exact copy of removedFootprintRings above (same reasoning:
		// don't touch the real chunk mesh's color/visibility, just flag it
		// with a distinct outline), different color so "being reviewed"
		// reads as visually distinct from "already removed."
		reviewOldFootprintRings: {
			type: Array,
			default: () => []
		},
		// Phase 4c: the CURRENT (post-nudge) proposed OneMap mesh(es) for
		// whatever's under review -- same mini-CityJSON shape and same
		// scoped-CityJSONLoader rendering mechanism as addedCitymodel above,
		// kept as a SEPARATE prop/group rather than reusing addedCitymodel
		// directly so a proposal-in-progress (not yet approved, may still be
		// rejected) can never be confused with a real, already-committed
		// addition. This is deliberately real, full-fidelity rendering (not
		// a translucent "ghost") -- per the plan, the point of the
		// contextual preview is seeing exactly what the new building would
		// really look like next to its real neighbors, not a placeholder;
		// the outlined old footprint alongside it is what supplies the
		// "old vs new" visual contrast.
		reviewCitymodel: {
			type: Object,
			default: null
		},
		// Real user report after the outline-only treatment shipped: a
		// removed/under-review building's real, still-fully-opaque geometry
		// visually occludes what's behind/underneath it (the removed
		// building's own outline, or -- worse -- the OneMap review overlay's
		// proposed mesh, "if it stays there it's overlapping you cant see
		// shit"). Building ids (not rings) -- resolved to each mesh's own
		// per-vertex objectid index in updateDimState() below (same
		// per-mesh-citymodel lookup the click-highlight fix needed) and
		// applied via a real shader mechanism (CityObjectsMaterial's
		// setDimState(), a project-local patch -- see webui/patches/ and the
		// project plan's own risk writeup for why this needed real,
		// isolated-verified shader work rather than a CSS/material trick).
		// removedIds fully hides (Remove, can accumulate all session);
		// reviewOldBuildingIds renders translucent instead (OneMap review,
		// usually one but can be several once pooled).
		removedIds: {
			type: Array,
			default: () => []
		},
		reviewOldBuildingIds: {
			type: Array,
			default: () => []
		},
		showIslandTerrain: {
			type: Boolean,
			default: true
		},
		// Height-edit (direct user request: fix "absurdly wrong" LoD1
		// heights, e.g. a OneMap-backfill height assigned to the wrong
		// tower). Deliberately a SEPARATE array from removedIds, not
		// reused -- removedIds also drives the 2D view's gray "removed"
		// recolor (see OrthoWebGLView.vue), which would be actively wrong
		// here (the building isn't removed, just height-corrected). This
		// only ever affects the 3D hide-shader below.
		editedIds: {
			type: Array,
			default: () => []
		},
	},
	data() {

		return {

			camera_init: true,
			lods: [],
			previousPos: {
				x: - 1,
				y: - 1
			},

		};

	},
	watch: {
		backgroundColor: function ( newVal ) {

			this.renderer.setClearColor( newVal );

			this.updateScene();

		},
		objectColors: {
			handler: function () {

				this.refreshColors();
				this.updateScene();

			},
			deep: true
		},
		surfaceColors: {
			handler: function () {

				this.refreshColors();
				this.updateScene();

			},
			deep: true
		},
		selectionColor: function () {

			this.refreshColors();
			this.updateScene();

		},
		// NOT deep: `citymodel` is always replaced wholesale (a shallowRef
		// reassignment in SbgViewer3D.vue, `citymodel.value = data`), never
		// mutated in place -- a shallow watch already fires correctly on
		// every reference reassignment, which is the only way this prop ever
		// changes here. `deep: true` would force Vue's traverse() to
		// recursively walk the ENTIRE CityJSON object graph (Solid -> shells
		// -> faces -> rings -> vertex-index arrays, several levels deep,
		// potentially tens of millions of individual elements for the full
		// 118,780-building island) before every reactivity flush -- a real,
		// confirmed, severe cost (see project plan: measured full-island 3D
		// load at 3-5 minutes with the tab genuinely unresponsive, not just
		// slow). This was ported unquestioned from ninja's original code in
		// Phase 0 and never revisited until directly diagnosed.
		citymodel: function ( newCitymodel ) {

			this.clearScene();

			this.loadCitymodel( newCitymodel );

			// Re-run these too, not just updateScene(): both read
			// this.citymodel.transform.translate to convert the ring's true-world
			// coordinates into this scene's local frame (see updateBoundaryRing's
			// comment). If a boundary was drawn before citymodel finished loading
			// (transform unavailable, so a 0,0 offset was used), this citymodel
			// change is the only thing that re-triggers them with the real
			// offset now available -- without this they'd stay stuck at the
			// wrong position until the ring itself happened to change again.
			this.updateBoundaryRing();
			this.updateHighlightFootprints();
			this.updateRemovedFootprints();
			this.updateAddedBuildings();
			this.updateReviewOldFootprints();
			this.updateReviewOverlay();
			// Re-shift too: same true-world -> local-frame conversion as the
			// boundary/highlight overlays, and this citymodel change is the
			// only thing that re-triggers it if the terrain finished loading
			// before citymodel.transform was available.
			this.updateIslandTerrain();
			// Re-apply too -- a fresh citymodel load means fresh materials
			// (on the main scene at initial load, or via
			// updateAddedBuildings/updateReviewOverlay above rebuilding
			// their own overlay materials from scratch every time), and a
			// brand new CityObjectsMaterial always starts with DIM_OBJECTS
			// off until setDimState() is called on it explicitly.
			this.updateDimState();

			this.updateScene();

		},
		boundaryRing: {
			handler: function () {

				this.updateBoundaryRing();
				this.updateScene();

			},
			deep: true
		},
		highlightFootprintRings: {
			handler: function () {

				this.updateHighlightFootprints();
				this.updateScene();

			},
			deep: true
		},
		removedFootprintRings: {
			handler: function () {

				this.updateRemovedFootprints();
				this.updateScene();

			},
			deep: true
		},
		// Not deep -- SbgViewer3D always reassigns this wholesale on a fresh
		// fetch (never mutates it in place), same reasoning as citymodel's
		// own non-deep watch above.
		addedCitymodel: function () {

			this.updateAddedBuildings();
			this.updateDimState(); // fresh overlay material -- see citymodel watcher's own comment on why
			this.updateScene();

		},
		reviewOldFootprintRings: {
			handler: function () {

				this.updateReviewOldFootprints();
				this.updateScene();

			},
			deep: true
		},
		reviewCitymodel: function () {

			this.updateReviewOverlay();
			this.updateDimState(); // fresh overlay material -- see citymodel watcher's own comment on why
			this.updateScene();

		},
		removedIds: {
			handler: function () {

				this.updateDimState();
				this.updateScene();

			},
			deep: true
		},
		reviewOldBuildingIds: {
			handler: function () {

				this.updateDimState();
				this.updateScene();

			},
			deep: true
		},
		showIslandTerrain: function () {

			this.updateIslandTerrain();

		},
		editedIds: {

			handler: function () {

				this.updateDimState();

			},
			deep: true

		},
		selectedObjid: function () {

			this.updateScene();

		},
		selectedGeomIdx: function () {

			this.updateScene();

		},
		selectedBoundaryIdx: function () {

			this.updateScene();

		},
		doubleSide: function () {

			this.scene.traverse( c => {

				if ( c.material && c.material.isCityObjectsMaterial ) {

					c.material.side = this.doubleSide ? THREE.DoubleSide : THREE.FrontSide;

				}

			} );

			this.updateScene();

		},
		ambientOcclusion: function () {

			if ( this.ambientOcclusion ) {

				this.composer.addPass( this.gtaoPass );

			} else {

				this.composer.removePass( this.gtaoPass );

			}

			this.updateScene();

		},
		highlightSelectedSurface: function () {

			this.scene.traverse( c => {

				if ( c.material && c.material.isCityObjectsMaterial ) {

					c.material.selectSurface = this.highlightSelectedSurface;

				}

			} );

			this.updateScene();

		},
		showSemantics: function ( value ) {

			this.scene.traverse( c => {

				if ( c.material && c.material.isCityObjectsMaterial ) {

					c.material.showSemantics = value;

				}

			} );

			this.updateScene();

		},
		activeLod: function ( lodIdx ) {

			this.scene.traverse( c => {

				if ( c.material && c.material.isCityObjectsMaterial ) {

					c.material.showLod = lodIdx;

				}

			} );

			this.updateScene();

		},
		cameraSpotlight: function () {

			this.updateScene();

		},
		conditionalFormatting: function ( value ) {

			if ( this.conditionalAttribute == '' || this.conditionalAttribute === null ) {

				return;

			}

			this.scene.traverse( c => {

				if ( c.isCityObject ) {

					c.material.conditionalFormatting = value;

				}

			} );

			this.updateScene();

		},
		conditionalAttribute: function ( value ) {

			this.updateConditionalInfo();

			this.updateScene();

		},
		activeMaterialTheme: function ( theme ) {

			this.scene.traverse( c => {

				if ( c.supportsMaterials ) {

					c.material.materialTheme = theme;

				}

			} );

			this.updateScene();

		},
		activeTextureTheme: function ( theme ) {

			const scope = this;

			this.scene.traverse( c => {

				if ( c.supportsMaterials ) {

					c.setTextureTheme( theme, scope.textureManager );

				}

			} );

			this.updateScene();

		},
		attributeColors: {
			handler: function () {

				this.scene.traverse( c => {

					if ( c.isCityObject ) {

						c.material.attributeColors = this.attributeColors;

					}

				} );

				this.updateScene();

			},
			deep: true
		}
	},
	beforeCreate() {

		this.scene = null;
		this.camera = null;
		this.renderer = null;
		this.controls = null;
		this.raycaster = null;
		this.mouse = null;
		this.spotLight = null;
		this.gtaoPass = null;
		this.composer = null;
		// Not in data(): Vue 3 Options API makes everything returned from
		// data() deeply reactive with no opt-out, and the real
		// CityJSONWorkerParser instance assigned here gets postMessage()'d
		// wholesale to a Web Worker for parsing -- a reactive Proxy can't be
		// structured-cloned. Confirmed via direct instrumentation: citymodel
		// (never touching data()) cloned fine, this.parser.objectColors and
		// this.parser.lods (reactive-wrapped via data()) both failed with
		// DataCloneError. Same reasoning as every other object above.
		this.parser = null;
		this.boundaryLine = null;
		this.highlightLines = [];
		this.removedLines = [];
		// Phase 4b (add-building): the THREE.Group a scoped CityJSONLoader
		// puts its real-geometry mesh(es) into for buildings added this
		// session -- kept out of data() like everything else here, same
		// reasoning (real THREE.js objects, not something Vue's reactivity
		// should wrap).
		this.addedBuildingsGroup = null;
		// Phase 4c (OneMap review-gate): reviewLines mirrors highlightLines/
		// removedLines (an outline per old-footprint-under-review);
		// reviewBuildingsGroup mirrors addedBuildingsGroup (the scoped
		// CityJSONLoader's group for the current proposed mesh(es)) -- kept
		// deliberately separate from the add-building versions of both, see
		// reviewCitymodel's own prop comment.
		this.reviewLines = [];
		this.reviewBuildingsGroup = null;
		// Real perf regression found via direct user report: updateScene()
		// (fires on every camera-drag frame AND on every chunk-loaded
		// callback during a full-island load) used to call
		// `Object.keys(citymodel.CityObjects).indexOf(id)` fresh every time
		// it needed to resolve the selected id to an integer index -- for
		// the real 118,780-building citymodel that's a ~118K-key array
		// build PLUS a linear scan, repeated per material per call. A
		// WeakMap cache keyed by the citymodel object itself (never
		// mutated in place, only ever replaced wholesale -- same invariant
		// citymodel's own non-deep watcher already relies on) means the
		// expensive id->index Map is built at most ONCE per citymodel
		// object, not once per updateScene() call.
		this._objectIndexCache = new WeakMap();
		// islandTerrainMesh: the built THREE.Mesh (kept out of data() like
		// everything else here). terrainRaw: the raw fetched heightfield (a
		// Float32Array wrapped in a plain object) -- also kept out of
		// data(), same reasoning as `parser`: Vue 3's deep reactivity would
		// wrap this in a Proxy, and re-reading millions of elements through
		// a Proxy would be needless overhead even though this one is never
		// postMessage'd.
		this.islandTerrainMesh = null;
		this.terrainRaw = null;
		// The last-loaded citymodel's bounding box (real THREE.Box3, kept out
		// of data() like everything else here) -- lets resetView() snap back
		// to a known-good extent regardless of where the user's orbit/pan/zoom
		// left the camera, rather than needing to reload/recompute anything.
		this.loadedBoundingBox = null;

	},
	mounted() {

		this.initScene();
		this.updateBoundaryRing();
		this.updateHighlightFootprints();
		this.updateRemovedFootprints();
		this.updateAddedBuildings();
		this.updateReviewOldFootprints();
		this.updateReviewOverlay();
		this.updateDimState();

		this.loadCitymodel( this.citymodel );

		this.updateScene();

		this.renderer.domElement.addEventListener( 'pointerdown', this.pointerDown, false );
		this.renderer.domElement.addEventListener( 'pointermove', this.pointerMove, false );
		this.renderer.domElement.addEventListener( 'pointerup', this.pointerUp, false );

		const scope = this;
		getIslandTerrain().then( data => {

			scope.terrainRaw = data;
			scope.updateIslandTerrain();
			scope.updateScene();

		} );

	},
	methods: {
		loadCitymodel( citymodel ) {

			this.$emit( 'rendering', true );

			if ( Object.keys( citymodel ).length > 0 ) {

				this.parser = new CityJSONWorkerParser();
				this.parser.chunkSize = 2000;

				const scope = this;
				this.parser.onChunkLoad = () => {

					scope.lods = scope.parser.lods;

					this.$emit( 'objectColorsChanged', scope.parser.objectColors );
					this.$emit( 'surfaceColorsChanged', scope.parser.surfaceColors );

					scope.refreshColors();

					scope.updateScene();

					scope.$emit( 'chunkLoaded' );

				};

				this.parser.onComplete = () => {

					scope.$emit( 'rendering', false );
					scope.$emit( 'loadCompleted' );

					scope.scene.traverse( c => {

						if ( c.isCityObject ) {

							c.material.side = this.doubleSide ? THREE.DoubleSide : THREE.FrontSide;
							// Forces a shader recompile, same as what toggling the LoD
							// filter does in ninja (CityObjectsBaseMaterial's showLod
							// setter flips needsUpdate=true on a SHOW_LOD define change) --
							// confirmed that's the actual mechanism behind "toggling LoD1
							// fixes the lighting": materials compiled on the very first
							// render don't pick up this scene's lights correctly, and a
							// recompile after the real geometry (and lights) are in place
							// fixes it. Forcing it directly here instead of relying on the
							// user finding a LoD button to click.
							c.material.needsUpdate = true;

							// These objects never move once loaded (confirmed:
							// cityjson-threejs-loader bakes real-world vertex
							// positions directly into the geometry, one merged
							// mesh per ~2000-object chunk, not one per building --
							// there are only tens to a couple hundred of these
							// objects total even for the full island, not
							// thousands). Freezing matrix auto-update skips a
							// needless per-frame recomputation for each of them.
							c.updateMatrix();
							c.matrixAutoUpdate = false;

						}

					} );

				};

				const loader = new CityJSONLoader( this.parser );
				loader.load( citymodel );

				// Stored regardless of autoFitOnLoad (cheap -- just a matrix
				// transform on an already-computed box) so resetView() always
				// has a real extent to snap back to, not just whatever camera
				// position the user's orbit/pan/zoom ended up at.
				const bbox = loader.boundingBox.clone();
				bbox.applyMatrix4( loader.matrix );
				this.loadedBoundingBox = bbox;

				if ( this.autoFitOnLoad ) {

					this.fitCameraToSelection( this.camera, this.controls, bbox );

				}

				this.scene.add( loader.scene );

			}

		},
		// Distance a perspective camera needs from a box's center to fit the
		// box's width and height in view (before any fitOffset padding).
		// Pulled out of fitCameraToSelection so the same math can size the
		// zoom-out cap (controls.maxDistance) against a DIFFERENT, wider
		// reference box than whatever's actually being framed right now --
		// see fitCameraToSelection's maxDistanceBox parameter for why that
		// split matters.
		//
		// Rederived from first principles (found while chasing a "16 scroll
		// clicks to match" zoom-gap report): a box of world-width sizeX /
		// world-height sizeY needs distance >= sizeY / (2*tan(halfFovY)) to
		// fit its height AND distance >= sizeX / (2*tan(halfFovY)*aspect) to
		// fit its width (dividing by aspect converts a vertical-FOV distance
		// into a horizontal-equivalent one) -- take the max of both real
		// constraints. The previous formula collapsed size.x/size.y into one
		// number BEFORE any distance math ran, then treated it as purely
		// vertical -- correct only when height happened to be the larger
		// dimension, silently wrong (overshooting by roughly the aspect
		// ratio) for any wider-than-tall box, which is the common case since
		// viewports are landscape. size.z folded into the height side (not
		// rigorous, matches the original code's own rough intent of not
		// letting a tall Z-extent clip either) since it isn't meaningfully
		// affected by the width/height aspect split. Also fixes a second,
		// compounding bug: the original used Math.atan where the standard
		// perspective fit-to-size math calls for Math.tan -- atan(x)
		// computes an angle from a ratio, the wrong operation entirely for
		// "take the tangent of an angle we already have."
		_fitDistanceForBox( camera, box ) {

			const size = new THREE.Vector3();
			box.getSize( size );

			const halfFovY = Math.PI * camera.fov / 360;
			const fitHeightDistance = Math.max( size.y, size.z ) / ( 2 * Math.tan( halfFovY ) );
			const fitWidthDistance = size.x / ( 2 * Math.tan( halfFovY ) * camera.aspect );
			return Math.max( fitHeightDistance, fitWidthDistance );

		},
		// maxDistanceBox: the box used to size controls.maxDistance (how far
		// the user can zoom OUT), separate from `box` (what's actually being
		// framed right now). Defaults to `box` itself when not given, which
		// is correct for the initial full-extent load and resetView() (the
		// box already IS the full extent there). Real bug found via direct
		// user report ("snap to 2D view... cant zoom out th see the whole
		// island anymore"): every fit call used to set maxDistance from
		// whatever box it was CURRENTLY framing, so snapping to a small
		// zoomed-in 2D area would shrink the zoom-out cap down to ~10x that
		// tiny area, permanently (until a manual Reset View) losing the
		// ability to zoom back out to island scale. Callers that fit a small
		// target while wanting to preserve the full-scene zoom range (goTo,
		// used by both search navigation and snapTo2dView) pass the
		// viewer's own loadedBoundingBox here instead.
		fitCameraToSelection( camera, controls, box, fitOffset = 1.2, topDown = false, maxDistanceBox = null ) {

			// From https://discourse.threejs.org/t/camera-zoom-to-fit-object/936/24

			// const box.makeEmpty();
			// for ( const object of selection ) {

			//   box.expandByObject( object );

			// }
			const size = new THREE.Vector3();
			const center = new THREE.Vector3();

			box.getSize( size );
			box.getCenter( center );

			// Still used below for the spotlight's offset scale (a rough "how
			// big is this scene" reference, unrelated to the camera-distance
			// fit math) -- kept as its own variable rather than folded into
			// _fitDistanceForBox, which deliberately no longer uses a single
			// pre-mixed maxSize.
			const maxSize = Math.max( size.x, size.y, size.z );

			const distance = fitOffset * this._fitDistanceForBox( camera, box );

			controls.maxDistance = this._fitDistanceForBox( camera, maxDistanceBox || box ) * 10;
			controls.target.copy( center );

			camera.near = distance / 100;
			camera.far = distance * 100;
			camera.updateProjectionMatrix();

			if ( topDown ) {

				// "Reset View"/"Snap to 2D view" both want a clean, deterministic
				// top-down orientation, not whatever angle the camera happened to
				// be at before -- straight reuse of the code below (which derives
				// direction from the CURRENT camera position) would just preserve
				// that old angle instead of resetting it.
				//
				// Can't get there by positioning the camera straight up and
				// calling controls.update() alone: OrbitControls.update() always
				// ends with object.lookAt(target) using camera.up (fixed at
				// world +Z here, see initScene()) -- confirmed by reading
				// OrbitControls.js directly -- and that lookAt is geometrically
				// degenerate exactly when the view direction is parallel to
				// camera.up, which is exactly true for a perfectly vertical
				// camera. The resulting on-screen "roll" (which way is up) would
				// be arbitrary/unstable rather than matching anything.
				//
				// Fixed by positioning+updating first (so OrbitControls' own
				// internal spherical/clamped state resyncs correctly for
				// whatever the user does next -- update() reads camera.position
				// fresh every call, confirmed in source, so this is safe even
				// though its own lookAt result gets thrown away immediately
				// after), then overwriting the final orientation with an
				// explicitly-constructed matrix using world +Y as the up
				// reference instead of camera.up -- non-degenerate (Y is
				// perpendicular to a straight-down view, not parallel to it) and
				// matches OrthoWebGLView's 2D camera exactly (it also uses
				// up=(0,1,0)), which is the whole point: same world direction
				// reads as "up on screen" in both views, so switching between
				// them doesn't feel like the content silently rotated.
				camera.position.set( center.x, center.y, center.z + distance );
				controls.update();

				const m = new THREE.Matrix4().lookAt( camera.position, center, new THREE.Vector3( 0, 1, 0 ) );
				camera.quaternion.setFromRotationMatrix( m );

			} else {

				const direction = controls.target.clone()
					.sub( camera.position )
					.normalize()
					.multiplyScalar( distance );

				camera.position.copy( controls.target ).sub( direction );

				controls.update();

			}

			// Overhead "sun" light, fixed to the actual loaded content's bounding
			// box (not world origin -- our real EPSG:3414 data sits ~30,000 units
			// from origin, so the light's default target of (0,0,0) gave it an
			// effectively arbitrary direction relative to the buildings, which is
			// what was making some faces look implausibly dark). Positioned once
			// per model load here, not re-aimed every frame like the
			// cameraSpotlight path below -- a fixed overhead source reads more
			// like natural sunlight than a headlamp that swings with the camera.
			if ( this.spotLight ) {

				// Offset in X/Y too, not just straight up -- a perfectly vertical
				// "noon sun" lights roofs brightly but leaves walls almost
				// unlit (near-zero angle of incidence), which read as a flat dark
				// blob from the oblique viewing angles this is actually used from.
				// ~40 degrees elevation from the northwest is a standard
				// architectural-rendering sun angle precisely because it shows
				// both roofs and walls with real shading definition.
				this.spotLight.position.set(
					center.x - maxSize * 1.2,
					center.y - maxSize * 1.2,
					center.z + maxSize * 1.4
				);
				this.spotLight.target.position.copy( center );
				this.spotLight.target.updateMatrixWorld();

			}

		},
		updateScene() {

			if ( this.cameraSpotlight ) {

				this.spotLight.position.copy( this.camera.position );

			}

			this.scene.traverse( c => {

				if ( c.material ) {

					const mats = Array.isArray( c.material ) ? c.material : [ c.material ];

					// Real bug found via user report: idx used to be computed
					// ONCE from this.citymodel (the main whole-island
					// citymodel) and applied to every material found by this
					// traverse -- including the added/review overlay groups'
					// own, separate CityObjectsMaterial instances, each built
					// by its own small, independently-indexed CityJSONLoader/
					// CityJSONParser (see updateAddedBuildings/
					// updateReviewOverlay). The uniform genuinely reached
					// those materials (they're real children of this.scene),
					// it just meant the wrong thing there -- objectIndex 5 in
					// the main citymodel is a different building (or
					// nothing) than objectIndex 5 in a 3-building overlay.
					// Resolve idx per-mesh from THAT mesh's own citymodel
					// (already stored on every CityObjectsMesh at
					// construction -- the same property
					// resolveIntersectionInfo() already relies on for the
					// click-to-select direction) instead of one hoisted
					// outer value shared by every material -- via the
					// cached lookup (see _objectIndexCache's own comment),
					// not a fresh Object.keys()/indexOf() scan every call.
					const meshCitymodel = c.citymodel || this.citymodel;
					const idx = this.resolveObjectIndex( meshCitymodel, this.selectedObjid );

					for ( const mat of mats ) {

						if ( mat.isCityObjectsMaterial ) {

							mat.selectSurface = this.highlightSelectedSurface;

							mat.highlightedObject = {

								objectIndex: idx,
								geometryIndex: this.selectedGeomIdx,
								boundaryIndex: this.selectedBoundaryIdx

							};

						}

					}

				}

			} );

			// this.renderer.render( this.scene, this.camera );
			this.composer.render();

		},
		refreshColors() {

			const scope = this;

			this.scene.traverse( mesh => {

				if ( mesh.material && mesh.material.isCityObjectsMaterial ) {

					mesh.material.objectColors = this.objectColors;
					mesh.material.surfaceColors = this.surfaceColors;

					mesh.material.highlightColor = scope.selectionColor;

				}

			} );

		},
		updateConditionalInfo() {

			if ( this.conditionalAttribute ) {

				const evaluator = new AttributeEvaluator( this.citymodel, this.conditionalAttribute );
				const colors = evaluator.createColors();

				this.$emit( 'attributeColorsChanged', colors );

				this.scene.traverse( c => {

					if ( c.isCityObject ) {

						c.addAttributeByProperty( evaluator );
						c.material.attributeColors = colors;

					}

				} );

			}

		},
		pointerDown( e ) {

			this.previousPos.x = e.clientX;
			this.previousPos.y = e.clientY;

		},
		pointerUp( e ) {

			if ( this.previousPos.x == e.clientX && this.previousPos.y == e.clientY ) {

				this.handleClick( e );

			}

		},
		pointerMove( e ) {

			if ( e.ctrlKey ) {

				this.handleClick( e );

			}

		},
		getActiveIntersection( results ) {

			// Filters through the results to find the first one for the active LoD

			if ( this.activeLod > - 1 ) {

				for ( let i = 0; i < results.length; i ++ ) {

					const lodIdx = results[ i ].object.resolveIntersectionInfo( results[ i ], this.citymodel ).lodIndex;

					if ( lodIdx == this.activeLod ) {

						return results[ i ];

					}

				}

			}

			return results[ 0 ];

		},
		handleClick( e ) {

			var rect = this.renderer.domElement.getBoundingClientRect();
			this.mouse.x = ( ( e.clientX - rect.left ) / this.renderer.domElement.clientWidth ) * 2 - 1;
			this.mouse.y = - ( ( e.clientY - rect.top ) / this.renderer.domElement.clientHeight ) * 2 + 1;

			//get cameraposition
			this.raycaster.setFromCamera( this.mouse, this.camera );

			//calculate intersects
			var intersects = this.raycaster.intersectObject( this.scene, true );

			//if clicked on nothing return
			if ( intersects.length == 0 ) {

				this.$emit( 'object_clicked', null );
				return;

			}

			const intersection = this.getActiveIntersection( intersects );

			if ( intersection.object.isCityObject ) {

				const info = intersection.object.resolveIntersectionInfo( intersection, this.citymodel );

				this.$emit( 'object_clicked', [ info.objectId, info.geometryIndex, info.boundaryIndex ] );

			}

		},
		getParams() {

			const hash = window.location.hash;

			if ( hash ) {

				const params = new URLSearchParams( hash.substring( 1 ) );
				return Object.fromEntries( params );

			} else {

				return {};

			}

		},
		initScene() {

			const viewer = document.getElementById( "viewer" );
			const ratio = viewer.clientWidth / viewer.clientHeight;

			this.scene = new THREE.Scene();
			this.camera = new THREE.PerspectiveCamera( 60, ratio, 0.0001, 4000 );
			this.camera.position.set( 0, - 1, 1 );
			this.camera.up.set( 0, 0, 1 );

			this.renderer = new THREE.WebGLRenderer( {
				antialias: window.devicePixelRatio > 1 ? false : true,
				powerPreference: "high-performance"
			} );
			this.renderer.outputEncoding = SRGBColorSpace;
			viewer.appendChild( this.renderer.domElement );
			this.renderer.setSize( viewer.clientWidth, viewer.clientHeight );
			this.renderer.setClearColor( this.backgroundColor );
			this.renderer.setPixelRatio( window.devicePixelRatio );

			// Real, likely cause of the "3D view randomly goes black, not
			// reproducible, fixed by a refresh" report: confirmed via grep that
			// NOTHING in this codebase (or ninja's, which this was ported from)
			// ever listened for 'webglcontextlost'. By spec, if nothing calls
			// preventDefault() on that event, the browser does not even attempt
			// automatic context restoration -- the canvas just stays black
			// forever until the page reloads. Real-world triggers for a context
			// loss are exactly the kind of thing that wouldn't reproduce on
			// demand: GPU driver reset, laptop integrated/discrete GPU
			// switching, OS reclaiming GPU memory under pressure, or simply too
			// many WebGL contexts open across tabs (this app alone runs two,
			// one for 2D's OrthoWebGLView and one for this 3D view, both
			// mounted for the whole session once activated).
			this.renderer.domElement.addEventListener( 'webglcontextlost', ( event ) => {

				event.preventDefault();
				console.warn( '[ThreeJsViewer] WebGL context lost -- attempting recovery on restore.' );

			}, false );
			this.renderer.domElement.addEventListener( 'webglcontextrestored', () => {

				console.warn( '[ThreeJsViewer] WebGL context restored -- forcing a full re-render.' );
				// Materials compiled before the context loss reference GPU
				// resources that no longer exist -- same needsUpdate trick
				// already used in onComplete() to force a shader recompile
				// after real geometry/lights are in place, reused here for the
				// same underlying reason (stale compiled state, different
				// cause).
				this.scene.traverse( c => {

					if ( c.material ) c.material.needsUpdate = true;

				} );
				this.updateScene();

			}, false );

			const composer = new EffectComposer( this.renderer );
			this.composer = composer;

			const renderPass = new RenderPass( this.scene, this.camera );
			composer.addPass( renderPass );

			this.gtaoPass = new GTAOPass( this.scene, this.camera, viewer.clientWidth, viewer.clientHeight );

			const aoParameters = {
				radius: 2.4,
				distanceExponent: 1.,
				thickness: 10.,
				scale: 1.3,
				// 8, not the original 16 -- GTAO cost scales with sample
				// count across every pixel every frame; halving it is a
				// straightforward win with negligible visual difference at
				// the zoom levels this app actually uses (island-wide to
				// street-level, never close enough for AO softness to be
				// the limiting visual factor).
				samples: 8,
				distanceFallOff: 1.,
				screenSpaceRadius: false,
			};

			this.gtaoPass.updateGtaoMaterial( aoParameters );

			if ( this.ambientOcclusion ) {

				composer.addPass( this.gtaoPass );

			}

			const outputPass = new OutputPass();
			composer.addPass( outputPass );

			// Real pre-existing bug, found chasing an unrelated crash while
			// testing with ?debug for the first time in this project: both
			// references below used the bare local `gtaoPass` instead of
			// `this.gtaoPass` -- a plain ReferenceError the instant this
			// block ran, since no local of that name is ever declared here.
			// Never caught before because nothing in this project had
			// actually exercised the debug GUI panel end-to-end.
			const updateGtaoMaterial = () => {

				this.gtaoPass.updateGtaoMaterial( aoParameters );
				this.updateScene();

			};

			if ( "debug" in this.getParams() ) {

				window.__threeJsViewerCamera = this.camera; // for console/automated inspection only
				window.__threeJsViewerScene = this.scene; // for console/automated inspection only
				window.__threeJsViewerUpdateScene = this.updateScene.bind( this ); // for console/automated inspection only

				const gui = new GUI();

				gui.add( this.gtaoPass, 'blendIntensity' ).min( 0 ).max( 1 ).step( 0.01 );
				gui.add( aoParameters, 'radius' ).min( 0.01 ).max( 10 ).step( 0.1 ).onChange( updateGtaoMaterial );
				gui.add( aoParameters, 'distanceExponent' ).min( 1 ).max( 4 ).step( 0.01 ).onChange( updateGtaoMaterial );
				gui.add( aoParameters, 'thickness' ).min( 0.01 ).max( 10 ).step( 0.1 ).onChange( updateGtaoMaterial );
				gui.add( aoParameters, 'distanceFallOff' ).min( 0 ).max( 1 ).step( 0.01 ).onChange( updateGtaoMaterial );
				gui.add( aoParameters, 'scale' ).min( 0.01 ).max( 2.0 ).step( 0.1 ).onChange( updateGtaoMaterial );
				gui.add( aoParameters, 'samples' ).min( 2 ).max( 32 ).step( 1 ).onChange( updateGtaoMaterial );
				gui.add( aoParameters, 'screenSpaceRadius' ).onChange( updateGtaoMaterial );

			}

			let self = this;

			this.raycaster = new THREE.Raycaster();
			this.mouse = new THREE.Vector2();

			// Bumped from 0.7 -- verified via direct instrumentation that neither
			// the directional light's position/target nor GTAO (confirmed absent
			// from composer.passes throughout) change between viewing angles, so
			// the "gets darker on rotation" complaint isn't a state bug at all --
			// it's real high contrast between brightly-lit roofs and dim walls
			// under one directional light with weak ambient fill, which just
			// reads differently depending on which faces dominate the current
			// view. More ambient fill softens that contrast at every angle.
			this.ambientLight = new THREE.AmbientLight( 0x999999, 1.1 * Math.PI ); // soft white light
			this.scene.add( this.ambientLight );

			this.spotLight = new THREE.DirectionalLight( 0xDDDDDD, Math.PI );
			this.spotLight.position.set( 1, 2, 3 );

			this.scene.add( this.spotLight );
			this.scene.add( this.spotLight.target ); // repositioned once the real content loads, see fitCameraToSelection

			this.controls = new OrbitControls( this.camera, this.renderer.domElement );
			if ( "debug" in this.getParams() ) window.__threeJsViewerControls = this.controls; // for console/automated inspection only
			// A real floor on how close the camera can dolly in -- without this,
			// OrbitControls' zoom is multiplicative (each scroll notch scales the
			// remaining distance by a fixed percentage), so it asymptotically
			// approaches distance=0 and never actually gets there: the closer you
			// get, the smaller each notch's absolute step becomes, which is
			// exactly the "zoom becomes exponentially slow" feeling (same root
			// cause as the well-known Blender orbit-zoom behavior near a pivot).
			// 1 meter is close enough to inspect a single building's facade.
			this.controls.minDistance = 1;
			this.controls.addEventListener( 'change', function () {

				// Recompute near/far from the CURRENT camera-to-target distance,
				// not once at load time (see fitCameraToSelection). Keeps the same
				// ratio (near = dist/100, far = dist*100) that's already proven
				// safe for depth-buffer precision at the initial whole-island fit,
				// just re-applied continuously -- so near shrinks as you zoom in
				// (letting you get much closer than a value fixed at island-wide
				// scale would ever allow) without ever blowing the near:far ratio
				// out to the point GTAO's depth reconstruction breaks down, which
				// is what happened when this was tried as a single static value
				// tuned for close-up zoom instead of tracking actual distance.
				const dist = self.camera.position.distanceTo( self.controls.target );
				self.camera.near = Math.max( dist / 100, 0.01 );
				self.camera.far = dist * 100;
				self.camera.updateProjectionMatrix();

				self.updateScene();

			} );
			this.controls.target.set( 0, 0, 0 );

			const scope = this;
			window.addEventListener( 'resize', _ => {

				scope.camera.aspect = viewer.clientWidth / viewer.clientHeight;
				scope.camera.updateProjectionMatrix();

				scope.renderer.setSize( viewer.clientWidth, viewer.clientHeight );

				const res = scope.lineResolution();
				if ( scope.boundaryLine ) scope.boundaryLine.material.resolution.copy( res );
				for ( const line of scope.highlightLines ) line.material.resolution.copy( res );
				for ( const line of scope.removedLines ) line.material.resolution.copy( res );

				scope.updateScene();

			}, false );

		},
		clearScene() {

			// Removes only the previously-loaded CityObject content, sparing
			// the persistent lights initScene() set up (and fitCameraToSelection
			// keeps aimed at real content) -- a bbox-panning reload used to wipe
			// them and recreate a second, inconsistent light rig from scratch
			// (different ambient intensity, a SpotLight hardcoded at a
			// coordinate unrelated to any real content), which never fired
			// before Phase 1 started reloading citymodel after mount. See the
			// project plan's lighting-bug writeup for how this was found.
			const keep = new Set( [ this.ambientLight, this.spotLight, this.spotLight.target, this.boundaryLine, this.islandTerrainMesh, this.addedBuildingsGroup, this.reviewBuildingsGroup, ...this.highlightLines, ...this.removedLines, ...this.reviewLines ] );
			for ( const child of [ ...this.scene.children ] ) {

				if ( ! keep.has( child ) ) this.scene.remove( child );

			}

		},
		lineResolution() {

			const viewer = document.getElementById( 'viewer' );
			return new THREE.Vector2( viewer.clientWidth, viewer.clientHeight );

		},
		makeFatLine( points, color, pixelWidth ) {

			const flat = [];
			for ( const p of points ) flat.push( p.x, p.y, p.z );

			const geometry = new LineGeometry();
			geometry.setPositions( flat );

			const material = new LineMaterial( {
				color,
				linewidth: pixelWidth, // screen-space pixels, unlike LineBasicMaterial's ignored linewidth
				resolution: this.lineResolution(),
				depthTest: false, // ground-level marker lines shouldn't get lost behind building walls at oblique angles
			} );

			const line = new Line2( geometry, material );
			line.computeLineDistances();
			line.renderOrder = 999;

			return line;

		},
		updateBoundaryRing() {

			if ( this.boundaryLine ) {

				this.scene.remove( this.boundaryLine );
				this.boundaryLine.geometry.dispose();
				this.boundaryLine.material.dispose();
				this.boundaryLine = null;

			}

			if ( ! this.boundaryRing || this.boundaryRing.length < 2 ) return;

			// Flat ground-level marker, not draped to terrain -- this dataset is
			// buildings-only (no terrain CityObject loaded here), so a flat loop
			// a couple meters up (avoids z-fighting with building bases sitting
			// at z=0) is a reasonable, honest representation: a boundary line,
			// not a claim about ground elevation.
			const z = 2;
			// boundaryRing is true EPSG:3414 world coordinates (that's what
			// App.vue/BoundaryDrawTool's 2D raycaster produces), but
			// cityjson-threejs-loader's CityJSONLoader zeroes out translation in
			// its coordinate matrix (see the Phase 1 plan writeup: "confirmed NOT
			// a bug... matrix and mesh are shifted by the SAME -translate offset
			// consistently") -- so actual building meshes here sit in a LOCAL
			// frame (true_world - transform.translate), not true world
			// coordinates. Subtract the same offset here or this ring renders
			// tens of kilometers away from the buildings it's supposed to outline.
			const [ tx, ty ] = this.citymodel?.transform?.translate || [ 0, 0 ];
			const points = this.boundaryRing.map( ( [ x, y ] ) => new THREE.Vector3( x - tx, y - ty, z ) );
			points.push( points[ 0 ] ); // closed loop

			this.boundaryLine = this.makeFatLine( points, 0xffd23f, 3 );
			this.scene.add( this.boundaryLine );

		},
		updateHighlightFootprints() {

			for ( const line of this.highlightLines ) {

				this.scene.remove( line );
				line.geometry.dispose();
				line.material.dispose();

			}
			this.highlightLines = [];

			if ( ! this.highlightFootprintRings || this.highlightFootprintRings.length === 0 ) return;

			const z = 2; // matches boundaryRing's ground-level offset, see updateBoundaryRing
			// Same true-world -> local-frame conversion as updateBoundaryRing --
			// see its comment for why this subtraction is needed.
			const [ tx, ty ] = this.citymodel?.transform?.translate || [ 0, 0 ];

			for ( const ring of this.highlightFootprintRings ) {

				if ( ! ring || ring.length < 2 ) continue;

				const points = ring.map( ( [ x, y ] ) => new THREE.Vector3( x - tx, y - ty, z ) );
				points.push( points[ 0 ] );

				const line = this.makeFatLine( points, 0xff3b1f, 3 );
				this.highlightLines.push( line );
				this.scene.add( line );

			}

		},
		// Phase 3 (remove-building): exact copy of updateHighlightFootprints
		// above -- see removedFootprintRings prop comment for why this is an
		// outline overlay, not an actual hide of the underlying mesh. Different
		// color (dark gray, not the crossing-warning's red) so the two overlay
		// types read as distinct at a glance.
		updateRemovedFootprints() {

			for ( const line of this.removedLines ) {

				this.scene.remove( line );
				line.geometry.dispose();
				line.material.dispose();

			}
			this.removedLines = [];

			if ( ! this.removedFootprintRings || this.removedFootprintRings.length === 0 ) return;

			const z = 2; // matches boundaryRing's ground-level offset, see updateBoundaryRing
			const [ tx, ty ] = this.citymodel?.transform?.translate || [ 0, 0 ];

			for ( const ring of this.removedFootprintRings ) {

				if ( ! ring || ring.length < 2 ) continue;

				const points = ring.map( ( [ x, y ] ) => new THREE.Vector3( x - tx, y - ty, z ) );
				points.push( points[ 0 ] );

				const line = this.makeFatLine( points, 0x333333, 3 );
				this.removedLines.push( line );
				this.scene.add( line );

			}

		},
		// Phase 4b (add-building): renders REAL geometry for buildings added
		// this session, via a second, small, synchronous CityJSONLoader
		// instance (CityJSONParser, not CityJSONWorkerParser -- no worker
		// postMessage/structuredClone overhead worth paying for a handful of
		// objects, and it means this can run synchronously instead of
		// juggling another async completion callback). addedCitymodel
		// carries the SAME transform (scale+translate) as the main
		// citymodel (see sbg.io_cityjson.subset_cityjson, which copies
		// cm["transform"] verbatim) -- CityJSONLoader.load() always zeroes
		// out translation from its matrix (see this file's own notes on
		// that elsewhere), so the resulting mesh lands in exactly the same
		// local frame as everything else in this scene, no extra shift
		// needed. An earlier version approximated every add as a flat-
		// topped box extruded from its footprint+height -- exactly right
		// for Path 1 (that box IS the real geometry) but visibly wrong for
		// Path 2/3 (a real imported CityJSON building or uploaded mesh has
		// actual wall/roof shape a box can't represent), per direct user
		// report.
		updateAddedBuildings() {

			if ( this.addedBuildingsGroup ) {

				this.scene.remove( this.addedBuildingsGroup );
				this.addedBuildingsGroup.traverse( ( child ) => {

					if ( child.geometry ) child.geometry.dispose();
					if ( child.material ) child.material.dispose();

				} );
				this.addedBuildingsGroup = null;

			}

			if ( ! this.addedCitymodel || Object.keys( this.addedCitymodel.CityObjects || {} ).length === 0 ) return;

			const loader = new CityJSONLoader( new CityJSONParser() );
			loader.load( this.addedCitymodel );
			this.addedBuildingsGroup = loader.scene;
			this.scene.add( this.addedBuildingsGroup );

		},
		// Phase 4c (OneMap review-gate): near-exact copy of
		// updateRemovedFootprints -- outlines the existing SBG building(s)
		// currently proposed for replacement, distinct orange color so it
		// doesn't read as either "removed" (dark gray) or "crossing warning"
		// (red).
		updateReviewOldFootprints() {

			for ( const line of this.reviewLines ) {

				this.scene.remove( line );
				line.geometry.dispose();
				line.material.dispose();

			}
			this.reviewLines = [];

			if ( ! this.reviewOldFootprintRings || this.reviewOldFootprintRings.length === 0 ) return;

			const z = 2;
			const [ tx, ty ] = this.citymodel?.transform?.translate || [ 0, 0 ];

			for ( const ring of this.reviewOldFootprintRings ) {

				if ( ! ring || ring.length < 2 ) continue;

				const points = ring.map( ( [ x, y ] ) => new THREE.Vector3( x - tx, y - ty, z ) );
				points.push( points[ 0 ] );

				const line = this.makeFatLine( points, 0xffa500, 3 );
				this.reviewLines.push( line );
				this.scene.add( line );

			}

		},
		// Phase 4c: near-exact copy of updateAddedBuildings -- renders the
		// CURRENT (post-nudge) proposed mesh(es) via the same scoped,
		// synchronous CityJSONLoader mechanism, kept in a separate group so
		// a still-under-review proposal is never confused with a real,
		// already-committed addition (see reviewCitymodel's own prop
		// comment for why this isn't just reused directly).
		updateReviewOverlay() {

			if ( this.reviewBuildingsGroup ) {

				this.scene.remove( this.reviewBuildingsGroup );
				this.reviewBuildingsGroup.traverse( ( child ) => {

					if ( child.geometry ) child.geometry.dispose();
					if ( child.material ) child.material.dispose();

				} );
				this.reviewBuildingsGroup = null;

			}

			if ( ! this.reviewCitymodel || Object.keys( this.reviewCitymodel.CityObjects || {} ).length === 0 ) return;

			const loader = new CityJSONLoader( new CityJSONParser() );
			loader.load( this.reviewCitymodel );
			this.reviewBuildingsGroup = loader.scene;
			this.scene.add( this.reviewBuildingsGroup );

		},
		// Cached id->index resolution -- see _objectIndexCache's own
		// beforeCreate() comment for why this exists (a real perf
		// regression: Object.keys(citymodel.CityObjects).indexOf(id) is
		// O(n) to BUILD the array alone, at 118,780 keys for the real
		// dataset, and was being redone on every updateScene() call).
		// citymodel is never mutated in place (only ever replaced
		// wholesale), so the built Map stays valid for the citymodel
		// object's whole lifetime -- no manual invalidation needed, the
		// WeakMap entry just becomes unreachable when the citymodel
		// reference itself is replaced.
		resolveObjectIndex( citymodel, id ) {

			if ( ! citymodel || id == null ) return - 1;

			let map = this._objectIndexCache.get( citymodel );
			if ( ! map ) {

				map = new Map();
				const keys = Object.keys( citymodel.CityObjects || {} );
				for ( let i = 0; i < keys.length; i ++ ) map.set( keys[ i ], i );
				this._objectIndexCache.set( citymodel, map );

			}

			return map.has( id ) ? map.get( id ) : - 1;

		},
		// Real per-object shader mechanism (see webui/patches/ and the
		// project plan's own risk writeup) -- a real user report that a
		// removed/under-review building's still-fully-opaque real geometry
		// visually blocks what's behind it (the OneMap review overlay's
		// proposed mesh, most concretely). removedIds/reviewOldBuildingIds
		// are STRING CityObject ids; each mesh's shader only understands its
		// OWN per-vertex integer `objectid`, indexed into THAT mesh's own
		// citymodel -- same per-mesh resolution the click-highlight fix in
		// updateScene() needs, and for the identical reason (the main scene
		// and the added/review overlay groups are built by separate loader
		// instances, each with its own citymodel and its own indexing).
		// Deliberately NOT called from updateScene() itself, which runs on
		// every camera-drag frame -- recomputing Object.keys().indexOf()
		// for every mesh on every frame would be real, avoidable overhead;
		// this only needs to re-run when removedIds/reviewOldBuildingIds
		// actually change, or when a fresh overlay material is built (see
		// this file's other updateDimState() call sites).
		updateDimState() {

			this.scene.traverse( c => {

				// Guard on setDimState itself, not just isCityObjectsMaterial --
				// a real production incident (user report, uncaught
				// "c.material.setDimState is not a function" firing on every
				// click/selection change) showed a mesh can carry
				// isCityObjectsMaterial=true from a material instance that
				// doesn't have the patched method, almost certainly a stale
				// module instance in a long-lived dev-server tab (Vite HMR
				// swapping ThreeJsViewer.vue's own code doesn't retroactively
				// upgrade already-constructed third-party material objects
				// built from an older module graph). Letting this throw
				// inside a watcher callback aborted the reactivity flush that
				// runs on every selectedObjid change -- i.e. every click --
				// which is what actually broke building-select and, from
				// there, cascaded into other UI appearing unresponsive. A
				// missing method here should just skip dim/hide for that one
				// mesh, never take down the rest of the app.
				if ( c.material && c.material.isCityObjectsMaterial && c.citymodel && typeof c.material.setDimState === 'function' ) {

					const hidden = [ ...this.removedIds, ...this.editedIds ]
						.map( id => this.resolveObjectIndex( c.citymodel, id ) )
						.filter( i => i >= 0 );
					const dimmed = this.reviewOldBuildingIds
						.map( id => this.resolveObjectIndex( c.citymodel, id ) )
						.filter( i => i >= 0 );

					c.material.setDimState( { hiddenObjIds: hidden, reviewDimObjIds: dimmed, reviewDimOpacity: 0.25 } );

				}

			} );

		},
		getLods() {

			return this.lods;

		},
		// "Stuck in zoom hell" fix: OrbitControls' dolly is multiplicative, so
		// a bad sequence of scroll gestures (or a fast trackpad fling) can land
		// the camera absurdly close to or far from the actual content with no
		// obvious way back short of a page reload. Re-fitting to the
		// last-loaded citymodel's real extent (stored in loadCitymodel(), see
		// beforeCreate()) is the same math the initial auto-fit already uses,
		// just callable on demand. topDown=true per direct user request --
		// a reset should also clear out whatever odd viewing angle you were
		// stuck at, not just the distance.
		resetView() {

			if ( ! this.loadedBoundingBox ) return;
			this.fitCameraToSelection( this.camera, this.controls, this.loadedBoundingBox, 1.2, true );
			this.updateScene();

		},
		updateIslandTerrain() {

			if ( this.islandTerrainMesh ) {

				this.scene.remove( this.islandTerrainMesh );
				this.islandTerrainMesh.geometry.dispose();
				this.islandTerrainMesh.material.dispose();
				this.islandTerrainMesh = null;

			}

			if ( ! this.terrainRaw || ! this.showIslandTerrain ) return;

			const { ncols, nrows, xmin, ymax, step, heights } = this.terrainRaw;
			// Same true-world -> local-frame conversion as updateBoundaryRing --
			// see its comment for why this subtraction is needed.
			const [ tx, ty ] = this.citymodel?.transform?.translate || [ 0, 0 ];

			const positions = new Float32Array( nrows * ncols * 3 );
			for ( let r = 0; r < nrows; r ++ ) {

				const y = ymax - r * step - ty;
				for ( let c = 0; c < ncols; c ++ ) {

					const idx = r * ncols + c;
					const h = heights[ idx ];
					positions[ idx * 3 ] = xmin + c * step - tx;
					positions[ idx * 3 + 1 ] = y;
					// NaN (masked/no-data) cells get a finite placeholder here --
					// these vertices are never referenced by the index buffer
					// below (see the valid-cell check), but a real NaN surviving
					// into the position buffer would poison
					// THREE.Box3.setFromBufferAttribute (it scans EVERY position,
					// not just indexed ones), corrupting the mesh's bounding
					// sphere/frustum culling for the whole object -- exactly the
					// "silent corruption from an unguarded edge case" category
					// this project has been bitten by before (see the 2D
					// TypedArray under-allocation bug in OrthoWebGLView).
					positions[ idx * 3 + 2 ] = Number.isNaN( h ) ? 0 : h;

				}

			}

			// Upper bound is exact, not a guess: each of the (nrows-1)*(ncols-1)
			// grid cells contributes at most 2 triangles / 6 indices, never
			// more -- trim with subarray() after building, same
			// provably-correct-bound pattern OrthoWebGLView's footprint buffers
			// use (after that earlier bug taught the lesson the hard way).
			const maxIndices = ( nrows - 1 ) * ( ncols - 1 ) * 6;
			const indices = new Uint32Array( maxIndices );
			let ptr = 0;
			for ( let r = 0; r < nrows - 1; r ++ ) {

				for ( let c = 0; c < ncols - 1; c ++ ) {

					const i00 = r * ncols + c;
					const i01 = i00 + 1;
					const i10 = i00 + ncols;
					const i11 = i10 + 1;

					if ( Number.isNaN( heights[ i00 ] ) || Number.isNaN( heights[ i01 ] ) ||
						Number.isNaN( heights[ i10 ] ) || Number.isNaN( heights[ i11 ] ) ) continue;

					indices[ ptr ++ ] = i00; indices[ ptr ++ ] = i01; indices[ ptr ++ ] = i11;
					indices[ ptr ++ ] = i00; indices[ ptr ++ ] = i11; indices[ ptr ++ ] = i10;

				}

			}

			const geometry = new THREE.BufferGeometry();
			geometry.setAttribute( 'position', new THREE.BufferAttribute( positions, 3 ) );
			geometry.setIndex( new THREE.BufferAttribute( indices.subarray( 0, ptr ), 1 ) );
			geometry.computeVertexNormals();

			// Prototype coloring -- a plain muted green, not draped/textured.
			// This overlay exists for visual orientation only (see
			// island_terrain.py's module docstring), not to be mistaken for
			// real terrain data at building-adjacent precision.
			//
			// Real bug, user report: "40% of buildings turned green" --
			// measured directly (not assumed): this coarse whole-island DTM
			// (20m-interval, 1:250,000-scale source contours) has its
			// interpolated elevation exceed the building's own roof height
			// at that spot for 66.5% of a random sample, median overshoot
			// 8m, 90th percentile 16m, up to 52m. Not fixable by nudging
			// the terrain height down a fixed amount -- the overshoot
			// distribution is too wide, any single offset either barely
			// helps or pushes genuinely-elevated real terrain unrealistically
			// low everywhere else. This data was never going to be precise
			// enough to trust for building-level depth comparisons (that's
			// the whole reason it's explicitly "visual orientation only").
			// depthWrite: false (terrain never occludes anything drawn
			// after it, regardless of whose Z is technically closer) +
			// renderOrder ensuring it draws first (so it still shows
			// correctly wherever nothing else is drawn over it) means
			// buildings always render through the terrain overlay
			// unconditionally, without needing the terrain's own elevation
			// to be trustworthy at building precision.
			const material = new THREE.MeshStandardMaterial( { color: 0x9caf7c, side: THREE.DoubleSide, depthWrite: false } );
			this.islandTerrainMesh = new THREE.Mesh( geometry, material );
			this.islandTerrainMesh.renderOrder = -1;
			this.scene.add( this.islandTerrainMesh );

		}
	}
};
</script>
