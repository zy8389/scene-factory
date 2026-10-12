import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const coreUrl = moduleUrl(fs.readFileSync(path.join(root, "web/vendor/three.core.js"), "utf8"));
const threeSource = fs.readFileSync(path.join(root, "web/vendor/three.module.js"), "utf8")
  .replaceAll("'./three.core.js'", JSON.stringify(coreUrl))
  .replaceAll('"./three.core.js"', JSON.stringify(coreUrl));
const threeUrl = moduleUrl(threeSource);
const THREE = await import(threeUrl);
const viewerSource = fs.readFileSync(path.join(root, "web/scene-viewer.js"), "utf8")
  .replace('import * as THREE from "three";', `import * as THREE from ${JSON.stringify(threeUrl)};`)
  .replace('import { OrbitControls } from "/vendor/OrbitControls.js";', "const OrbitControls = class {};")
  .replace('import { GLTFLoader } from "/vendor/GLTFLoader.js";', "const GLTFLoader = class {};");
const { SceneViewer } = await import(moduleUrl(viewerSource));

function close(actual, expected, label) {
  assert.ok(Math.abs(actual - expected) < 1e-5, `${label}: actual=${actual}, expected=${expected}`);
}

function geometryModel(bytes) {
  assert.equal(bytes.readUInt32LE(0), 0x46546c67);
  const data = JSON.parse(bytes.subarray(20, 20 + bytes.readUInt32LE(12)).toString("utf8"));
  const nodes = data.nodes.map((node) => {
    const object = new THREE.Group();
    if (node.matrix) {
      object.matrix.fromArray(node.matrix);
      object.matrix.decompose(object.position, object.quaternion, object.scale);
    } else {
      if (node.translation) object.position.fromArray(node.translation);
      if (node.rotation) object.quaternion.fromArray(node.rotation);
      if (node.scale) object.scale.fromArray(node.scale);
    }
    if (node.mesh !== undefined) {
      for (const primitive of data.meshes[node.mesh].primitives) {
        const accessor = data.accessors[primitive.attributes.POSITION];
        assert.ok(accessor.min && accessor.max, "real asset must declare position bounds");
        const minimum = new THREE.Vector3().fromArray(accessor.min);
        const maximum = new THREE.Vector3().fromArray(accessor.max);
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(...maximum.clone().sub(minimum).toArray()),
          new THREE.MeshStandardMaterial());
        mesh.position.copy(minimum.add(maximum).multiplyScalar(0.5));
        object.add(mesh);
      }
    }
    return object;
  });
  data.nodes.forEach((node, index) => {
    for (const child of node.children || []) nodes[index].add(nodes[child]);
  });
  const model = new THREE.Group();
  for (const index of data.scenes[data.scene || 0].nodes) model.add(nodes[index]);
  return model;
}

const synthetic = new THREE.Mesh(new THREE.BoxGeometry(1, 2, 3), new THREE.MeshStandardMaterial());
synthetic.position.set(2, -3, 4);
const fitted = SceneViewer.prototype.fitVisual(synthetic, [4, 6, 8], [0.5, 0.5, 0.5],
  { rotation_euler_deg: [90, 0, 0], scale_mode: "fit_bbox" });
const fittedBox = new THREE.Box3().setFromObject(fitted);
fittedBox.getSize(new THREE.Vector3()).toArray().forEach((value, index) => close(value, [4, 6, 8][index], "rotated support scale"));
close(fittedBox.min.z, -4, "centered support bottom");
close(fittedBox.max.z, 4, "centered support top");
const prop = SceneViewer.prototype.fitVisual(new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2),
  new THREE.MeshStandardMaterial()), [2, 2, 4], null, { rotation_euler_deg: [0, 0, 0] });
close(new THREE.Box3().setFromObject(prop).min.z, -2, "contained prop bottom");
close(prop.scale.x, prop.scale.z, "prop uniform scale");

const payload = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const viewer = Object.create(SceneViewer.prototype);
viewer.world = new THREE.Group();
viewer.room = new THREE.Group();
viewer.revision = 0;
viewer.buildRoom = () => {};
viewer.frameRoom = () => {};
viewer.setStatus = () => {};
viewer.loadAsset = async (url) => geometryModel(fs.readFileSync(payload.visual_paths[url]));
await viewer.renderScene(payload.scene, payload.assets);
viewer.world.updateMatrixWorld(true);
const objects = new Map(payload.scene.objects.map((item) => [item.object_id, item]));
const bounds = new Map();
let contacts = 0;
for (const item of payload.scene.objects) {
  const holder = viewer.world.getObjectByName(item.object_id);
  const box = new THREE.Box3().setFromObject(holder);
  bounds.set(item.object_id, box);
  close(box.min.z, item.pose.position[2] - item.bbox_m[2] / 2, `${item.object_id} bottom`);
  if (payload.assets[item.asset_id].visual_transform?.scale_mode === "fit_bbox") {
    close(box.max.z, item.pose.position[2] + item.bbox_m[2] / 2, `${item.object_id} top`);
  }
}
for (const item of payload.scene.objects) {
  if (!item.support || item.support === "floor") continue;
  const [supportId, surfaceName] = item.support.split(":");
  const support = objects.get(supportId);
  const surface = payload.surfaces[supportId].find((surface) => !surfaceName || surface.name === surfaceName);
  const expectedHeight = support.pose.position[2] + surface.center[2];
  close(bounds.get(item.object_id).min.z, expectedHeight, `${item.object_id} contact`);
  if (payload.assets[support.asset_id].visual_transform?.scale_mode === "fit_bbox") {
    close(bounds.get(supportId).max.z, expectedHeight, `${supportId} visible surface`);
  }
  contacts += 1;
}
console.log(JSON.stringify({ objects: objects.size, support_contacts: contacts, result: "passed" }));
