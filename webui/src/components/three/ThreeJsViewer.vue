<template>
  <div
    id="viewer"
    class="col-12 px-0 h-100"
  ></div>
</template>

<script>
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls';
import { AttributeEvaluator, CityJSONLoader, CityJSONWorkerParser, CityObjectsMaterial, TextureManager } from 'cityjson-threejs-loader';
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
			// Re-shift too: same true-world -> local-frame conversion as the
			// boundary/highlight overlays, and this citymodel change is the
			// only thing that re-triggers it if the terrain finished loading
			// before citymodel.transform was available.
			this.updateIslandTerrain();

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
		// islandTerrainMesh: the built THREE.Mesh (kept out of data() like
		// everything else here). terrainRaw: the raw fetched heightfield (a
		// Float32Array wrapped in a plain object) -- also kept out of
		// data(), same reasoning as `parser`: Vue 3's deep reactivity would
		// wrap this in a Proxy, and re-reading millions of elements through
		// a Proxy would be needless overhead even though this one is never
		// postMessage'd.
		this.islandTerrainMesh = null;
		this.terrainRaw = null;

	},
	mounted() {

		this.initScene();
		this.updateBoundaryRing();
		this.updateHighlightFootprints();

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

				if ( this.autoFitOnLoad ) {

					const bbox = loader.boundingBox.clone();
					bbox.applyMatrix4( loader.matrix );

					this.fitCameraToSelection( this.camera, this.controls, bbox );

				}

				this.scene.add( loader.scene );

			}

		},
		fitCameraToSelection( camera, controls, box, fitOffset = 1.2 ) {

			// From https://discourse.threejs.org/t/camera-zoom-to-fit-object/936/24

			// const box.makeEmpty();
			// for ( const object of selection ) {

			//   box.expandByObject( object );

			// }
			const size = new THREE.Vector3();
			const center = new THREE.Vector3();

			box.getSize( size );
			box.getCenter( center );

			const maxSize = Math.max( size.x, size.y, size.z );
			const fitHeightDistance = maxSize / ( 2 * Math.atan( Math.PI * camera.fov / 360 ) );
			const fitWidthDistance = fitHeightDistance / camera.aspect;
			const distance = fitOffset * Math.max( fitHeightDistance, fitWidthDistance );

			const direction = controls.target.clone()
				.sub( camera.position )
				.normalize()
				.multiplyScalar( distance );

			controls.maxDistance = distance * 10;
			controls.target.copy( center );

			camera.near = distance / 100;
			camera.far = distance * 100;
			camera.updateProjectionMatrix();

			camera.position.copy( controls.target ).sub( direction );

			controls.update();

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

			const idx = Object.keys( this.citymodel.CityObjects || {} ).indexOf( this.selectedObjid );

			this.scene.traverse( c => {

				if ( c.material ) {

					const mats = Array.isArray( c.material ) ? c.material : [ c.material ];

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

			const updateGtaoMaterial = () => {

				gtaoPass.updateGtaoMaterial( aoParameters );
				this.updateScene();

			};

			if ( "debug" in this.getParams() ) {

				const gui = new GUI();

				gui.add( gtaoPass, 'blendIntensity' ).min( 0 ).max( 1 ).step( 0.01 );
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
			const keep = new Set( [ this.ambientLight, this.spotLight, this.spotLight.target, this.boundaryLine, this.islandTerrainMesh, ...this.highlightLines ] );
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
		getLods() {

			return this.lods;

		},
		updateIslandTerrain() {

			if ( this.islandTerrainMesh ) {

				this.scene.remove( this.islandTerrainMesh );
				this.islandTerrainMesh.geometry.dispose();
				this.islandTerrainMesh.material.dispose();
				this.islandTerrainMesh = null;

			}

			if ( ! this.terrainRaw ) return;

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
			const material = new THREE.MeshStandardMaterial( { color: 0x9caf7c, side: THREE.DoubleSide } );
			this.islandTerrainMesh = new THREE.Mesh( geometry, material );
			this.scene.add( this.islandTerrainMesh );

		}
	}
};
</script>
