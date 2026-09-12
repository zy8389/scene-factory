import * as THREE from "three";
import { OrbitControls } from "/vendor/OrbitControls.js";
import { GLTFLoader } from "/vendor/GLTFLoader.js";

THREE.Object3D.DEFAULT_UP.set(0, 0, 1);

export class SceneViewer {
  constructor(container, statusElement) {
    this.container = container;
    this.statusElement = statusElement;
    this.loader = new GLTFLoader();
    this.assetCache = new Map();
    this.revision = 0;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0xdde4e2);
    this.scene.fog = new THREE.Fog(0xdde4e2, 8, 22);
    this.camera = new THREE.PerspectiveCamera(38, 4 / 3, 0.01, 100);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.domElement.setAttribute("aria-label", "Three.js 场景三维预览");
    this.container.appendChild(this.renderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.075;
    this.controls.screenSpacePanning = true;
    this.controls.minDistance = 0.4;
    this.controls.maxDistance = 30;
    this.controls.maxPolarAngle = Math.PI * 0.49;

    this.world = new THREE.Group();
    this.room = new THREE.Group();
    this.scene.add(this.world);
    this.scene.add(this.room);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x64706b, 2.1));
    const keyLight = new THREE.DirectionalLight(0xffffff, 3.2);
    keyLight.position.set(4, -5, 8);
    keyLight.castShadow = true;
    keyLight.shadow.mapSize.set(2048, 2048);
    keyLight.shadow.camera.near = 0.1;
    keyLight.shadow.camera.far = 25;
    keyLight.shadow.camera.left = -6;
    keyLight.shadow.camera.right = 6;
    keyLight.shadow.camera.top = 6;
    keyLight.shadow.camera.bottom = -6;
    this.scene.add(keyLight);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.container);
    this.renderer.setAnimationLoop(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
    this.resize();
  }

  async renderScene(sceneData, assets = {}) {
    const revision = ++this.revision;
    this.clearWorld();
    this.buildRoom(sceneData.room_dimensions_m);
    this.frameRoom(sceneData.room_dimensions_m);
    this.setStatus("正在加载场景资产…", "loading");

    const loads = sceneData.objects.map(async (item) => {
      const asset = assets[item.asset_id] || {};
      const holder = new THREE.Group();
      holder.name = item.object_id;
      holder.position.fromArray(item.pose.position);
      holder.rotation.z = THREE.MathUtils.degToRad(item.pose.yaw_deg || 0);
      this.world.add(holder);

      const proxy = this.createProxy(item, asset);
      holder.add(proxy);
      if (!asset.visual_url) return false;
      try {
        const source = await this.loadAsset(asset.visual_url);
        if (revision !== this.revision) return false;
        const visual = this.fitVisual(
          source.clone(true),
          item.bbox_m,
          asset.color,
          asset.visual_transform,
        );
        holder.remove(proxy);
        this.disposeProxy(proxy);
        holder.add(visual);
        return true;
      } catch (error) {
        console.warn(`Unable to load ${item.asset_id}; using its collision proxy.`, error);
        return false;
      }
    });

    const loaded = (await Promise.all(loads)).filter(Boolean).length;
    if (revision !== this.revision) return;
    const proxyCount = sceneData.objects.length - loaded;
    const detail = loaded
      ? `${loaded} 个真实资产 · ${proxyCount} 个代理`
      : `${proxyCount} 个包围盒代理`;
    this.setStatus(detail, loaded ? "ready" : "proxy");
  }

  resize() {
    const width = Math.max(1, this.container.clientWidth);
    const height = Math.max(1, this.container.clientHeight);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  }

  setStatus(text, state) {
    this.statusElement.textContent = text;
    this.statusElement.dataset.state = state;
  }

  clearWorld() {
    for (const group of [this.world, this.room]) {
      for (const child of [...group.children]) {
        group.remove(child);
        child.traverse((node) => {
          if (node.userData.disposable && node.geometry) node.geometry.dispose();
          if (node.userData.disposable && node.material) node.material.dispose();
        });
      }
    }
  }

  buildRoom(dimensions) {
    const [width, depth, height] = dimensions;
    const floor = new THREE.Mesh(
      new THREE.BoxGeometry(width, depth, 0.04),
      new THREE.MeshStandardMaterial({ color: 0xb9b4a9, roughness: 0.9, metalness: 0.03 }),
    );
    floor.position.z = -0.025;
    floor.receiveShadow = true;
    floor.userData.disposable = true;
    this.room.add(floor);

    const wallMaterial = new THREE.MeshStandardMaterial({ color: 0xf0eee8, roughness: 0.94 });
    const backsplashMaterial = new THREE.MeshStandardMaterial({
      color: 0xd6d9d5,
      roughness: 0.42,
      metalness: 0.06,
    });
    const wallThickness = 0.08;
    const backWall = new THREE.Mesh(new THREE.BoxGeometry(width, wallThickness, height), wallMaterial);
    backWall.position.set(0, depth / 2 + wallThickness / 2, height / 2);
    backWall.receiveShadow = true;
    backWall.userData.disposable = true;
    this.room.add(backWall);
    const leftWall = new THREE.Mesh(
      new THREE.BoxGeometry(wallThickness, depth, height),
      wallMaterial.clone(),
    );
    leftWall.position.set(-width / 2 - wallThickness / 2, 0, height / 2);
    leftWall.receiveShadow = true;
    leftWall.userData.disposable = true;
    this.room.add(leftWall);

    const backsplash = new THREE.Mesh(
      new THREE.BoxGeometry(width * 0.78, 0.025, Math.min(0.7, height * 0.25)),
      backsplashMaterial,
    );
    backsplash.position.set(0, depth / 2 - 0.055, Math.min(1.25, height * 0.46));
    backsplash.receiveShadow = true;
    backsplash.userData.disposable = true;
    this.room.add(backsplash);

    const ceilingLight = new THREE.PointLight(0xfff1d5, 42, Math.max(width, depth) * 2.2, 2);
    ceilingLight.position.set(0, 0.3, height - 0.2);
    this.room.add(ceilingLight);

    const divisions = Math.max(8, Math.round(Math.max(width, depth) * 2));
    const grid = new THREE.GridHelper(Math.max(width, depth), divisions, 0x71807a, 0xb7bcb7);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = 0.002;
    grid.material.transparent = true;
    grid.material.opacity = 0.18;
    grid.userData.disposable = true;
    this.room.add(grid);

    const outlinePoints = [
      new THREE.Vector3(-width / 2, -depth / 2, 0.012),
      new THREE.Vector3(width / 2, -depth / 2, 0.012),
      new THREE.Vector3(width / 2, depth / 2, 0.012),
      new THREE.Vector3(-width / 2, depth / 2, 0.012),
      new THREE.Vector3(-width / 2, -depth / 2, 0.012),
    ];
    const outline = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(outlinePoints),
      new THREE.LineBasicMaterial({ color: 0x24302d }),
    );
    outline.userData.disposable = true;
    this.room.add(outline);
  }

  frameRoom(dimensions) {
    const span = Math.max(Number(dimensions[0]), Number(dimensions[1]), 1);
    const targetHeight = Math.min(0.85, Number(dimensions[2]) * 0.28);
    this.controls.target.set(0, 0, targetHeight);
    this.camera.position.set(span * 0.95, -span * 1.15, span * 0.9);
    this.camera.near = Math.max(0.01, span / 1000);
    this.camera.far = Math.max(50, span * 12);
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }

  createProxy(item, asset) {
    const bbox = item.bbox_m.map(Number);
    const primitive = String(asset.primitive || "cube").toLowerCase();
    let geometry;
    if (primitive === "sphere") {
      geometry = new THREE.SphereGeometry(0.5, 28, 18);
    } else if (primitive === "cylinder" || primitive === "capsule") {
      geometry = new THREE.CylinderGeometry(0.5, 0.5, 1, 32);
      geometry.rotateX(Math.PI / 2);
    } else {
      geometry = new THREE.BoxGeometry(1, 1, 1);
    }
    const color = new THREE.Color().fromArray(asset.color || [0.56, 0.59, 0.58]);
    const material = new THREE.MeshStandardMaterial({
      color,
      roughness: item.dynamic ? 0.62 : 0.82,
      metalness: primitive === "cylinder" ? 0.08 : 0.02,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.scale.fromArray(bbox);
    mesh.castShadow = item.dynamic;
    mesh.receiveShadow = true;
    mesh.userData.disposable = true;
    return mesh;
  }

  fitVisual(model, bbox, fallbackColor, transform = {}) {
    const oriented = new THREE.Group();
    const rotation = Array.isArray(transform.rotation_euler_deg)
      ? transform.rotation_euler_deg
      : [90, 0, 0];
    oriented.rotation.set(
      THREE.MathUtils.degToRad(Number(rotation[0]) || 0),
      THREE.MathUtils.degToRad(Number(rotation[1]) || 0),
      THREE.MathUtils.degToRad(Number(rotation[2]) || 0),
    );
    oriented.add(model);
    oriented.updateMatrixWorld(true);

    const initialBox = new THREE.Box3().setFromObject(oriented);
    const initialSize = initialBox.getSize(new THREE.Vector3());
    const desired = new THREE.Vector3().fromArray(bbox);
    const epsilon = 1e-6;
    const scale = Math.min(
      desired.x / Math.max(initialSize.x, epsilon),
      desired.y / Math.max(initialSize.y, epsilon),
      desired.z / Math.max(initialSize.z, epsilon),
    );
    oriented.scale.setScalar(scale);
    oriented.updateMatrixWorld(true);
    const scaledBox = new THREE.Box3().setFromObject(oriented);
    const center = scaledBox.getCenter(new THREE.Vector3());
    oriented.position.set(-center.x, -center.y, -scaledBox.min.z);
    model.traverse((node) => {
      if (!node.isMesh) return;
      node.material = node.material.clone();
      if (!node.material.map && fallbackColor) {
        node.material.color.fromArray(fallbackColor);
      }
      node.castShadow = true;
      node.receiveShadow = true;
    });
    return oriented;
  }

  loadAsset(url) {
    if (!this.assetCache.has(url)) {
      this.assetCache.set(url, new Promise((resolve, reject) => {
        this.loader.load(url, (gltf) => resolve(gltf.scene), undefined, reject);
      }));
    }
    return this.assetCache.get(url);
  }

  disposeProxy(proxy) {
    if (proxy.geometry) proxy.geometry.dispose();
    if (proxy.material) proxy.material.dispose();
  }
}
