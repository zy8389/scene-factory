import * as THREE from "three";
import { SceneViewer } from "./scene-viewer.js";

const COLORS = { end: 0x2563eb, left: 0xc026d3, right: 0x0891b2, target: 0xd97706, contact: 0x16a34a };
const vector = (value, length) => Array.isArray(value) && value.length === length && value.every(Number.isFinite);

/** Render measured object, end-effector and finger states without inferring robot links. */
export class RuntimeReplay {
  constructor(container, statusElement) {
    this.container = container;
    this.statusElement = statusElement;
    this.viewer = new SceneViewer(container, statusElement);
    this.viewer.renderer.domElement.setAttribute("aria-label", "真实仿真状态回放：物体、末端与夹指轨迹");
    this.epoch = 0;
    this.payload = null;
    this.index = 0;
    this.disposed = false;
    this.overlay = new THREE.Group();
    this.viewer.scene.add(this.overlay);
    this.markers = {};
    this.tracks = [];
    this.objectHolders = new Map();
  }

  async load(payload) {
    if (this.disposed) throw new Error("回放器已关闭");
    if (payload?.source !== "recorded_state" || !Array.isArray(payload.frames) || !payload.frames.length || !payload.scene) {
      throw new Error("缺少可回放的真实状态记录");
    }
    const epoch = ++this.epoch;
    this.payload = null;
    this.clearOverlay();
    // SceneViewer clones visual materials while its cached model shares geometry
    // and textures. Release those clones before replacing the current scene.
    this.viewer.world.traverse((node) => {
      if (!node.isMesh || node.userData.disposable) return;
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      for (const material of materials) material?.dispose();
    });
    await this.viewer.renderScene(payload.scene, payload.assets || {});
    if (epoch !== this.epoch || this.disposed) return null;
    this.payload = payload;
    this.objectHolders = new Map(this.viewer.world.children.map((holder) => [holder.name, holder]));
    for (const [key, color] of Object.entries(COLORS).filter(([key]) => key !== "contact")) {
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(key === "end" ? 0.016 : 0.011, 16, 12),
        new THREE.MeshBasicMaterial({ color, depthTest: false, transparent: true, opacity: 0.92 }),
      );
      marker.renderOrder = 10;
      this.markers[key] = marker;
      this.overlay.add(marker);
    }
    // Only this measured pose has an orientation; finger spheres communicate position only.
    this.endAxes = new THREE.AxesHelper(0.065);
    this.endAxes.material.depthTest = false;
    this.endAxes.renderOrder = 11;
    this.overlay.add(this.endAxes);
    const target = payload.scene.task?.target_object || payload.scene.task?.success?.subject;
    this.target = target;
    this.addTrack("end", (frame) => frame.robot.end_effector_pose?.position);
    this.addTrack("left", (frame) => frame.robot.finger_positions?.left);
    this.addTrack("right", (frame) => frame.robot.finger_positions?.right);
    this.addTrack("target", (frame) => frame.objects[target]?.position);
    this.frameMotion();
    return this.seek(0);
  }

  addTrack(key, readPosition) {
    const coordinates = [];
    const ends = [];
    for (let index = 1; index < this.payload.frames.length; index++) {
      const before = readPosition(this.payload.frames[index - 1]);
      const after = readPosition(this.payload.frames[index]);
      // Missing measurements create a gap instead of an interpolated pose.
      if (!vector(before, 3) || !vector(after, 3)) continue;
      coordinates.push(...before, ...after);
      ends.push(index);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(coordinates, 3));
    geometry.setDrawRange(0, 0);
    const line = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({
      color: COLORS[key], transparent: true, opacity: 0.75, depthTest: false,
    }));
    line.renderOrder = 8;
    this.overlay.add(line);
    this.tracks.push({ line, ends });
  }

  frameMotion() {
    const bounds = new THREE.Box3();
    for (const frame of this.payload.frames) {
      for (const position of [frame.robot.end_effector_pose?.position, frame.objects[this.target]?.position]) {
        if (vector(position, 3)) bounds.expandByPoint(new THREE.Vector3().fromArray(position));
      }
    }
    if (bounds.isEmpty()) return;
    const center = bounds.getCenter(new THREE.Vector3());
    const span = Math.max(1.5, bounds.getSize(new THREE.Vector3()).length() * 1.8);
    this.viewer.controls.target.copy(center);
    this.viewer.camera.position.set(center.x + span * 0.9, center.y - span, center.z + span * 0.65);
    this.viewer.controls.update();
  }

  seek(index) {
    if (!this.payload || this.disposed) return null;
    if (!Number.isFinite(index)) throw new Error("无效的回放帧号");
    this.index = Math.max(0, Math.min(this.payload.frames.length - 1, Math.trunc(index)));
    const frame = this.payload.frames[this.index];
    for (const item of this.payload.scene.objects) {
      const holder = this.objectHolders.get(item.object_id);
      const measured = frame.objects[item.object_id];
      if (!holder || !measured) continue;
      holder.position.fromArray(measured.position);
      const orientation = measured.orientation_wxyz;
      if (vector(orientation, 4)) {
        holder.quaternion.set(orientation[1], orientation[2], orientation[3], orientation[0]).normalize();
      } else {
        holder.rotation.set(0, 0, THREE.MathUtils.degToRad(item.pose.yaw_deg || 0));
      }
    }
    this.updateMarker("end", frame.robot.end_effector_pose?.position);
    this.updateMarker("left", frame.robot.finger_positions?.left, frame.contacts?.left);
    this.updateMarker("right", frame.robot.finger_positions?.right, frame.contacts?.right);
    this.updateMarker("target", frame.objects[this.target]?.position);
    this.endAxes.visible = vector(frame.robot.end_effector_pose?.position, 3);
    if (this.endAxes.visible) {
      this.endAxes.position.fromArray(frame.robot.end_effector_pose.position);
      const quaternion = frame.robot.end_effector_pose.orientation_wxyz;
      this.endAxes.visible = vector(quaternion, 4);
      if (this.endAxes.visible) this.endAxes.quaternion.set(quaternion[1], quaternion[2], quaternion[3], quaternion[0]).normalize();
    }
    for (const { line, ends } of this.tracks) {
      let low = 0;
      let high = ends.length;
      while (low < high) {
        const middle = Math.floor((low + high) / 2);
        if (ends[middle] <= this.index) low = middle + 1;
        else high = middle;
      }
      line.geometry.setDrawRange(0, low * 2);
    }
    const state = frame.failure_reason ? ` · ${frame.failure_reason}` : "";
    this.viewer.setStatus(
      `末端与夹指轨迹 · ${this.index + 1}/${this.payload.frames.length} 帧 · ${frame.timestamp.toFixed(2)} 秒${state}`,
      "ready",
    );
    return frame;
  }

  updateMarker(key, position, contact = false) {
    const marker = this.markers[key];
    marker.visible = vector(position, 3);
    if (!marker.visible) return;
    marker.position.fromArray(position);
    marker.material.color.setHex(contact ? COLORS.contact : COLORS[key]);
  }

  clearOverlay() {
    for (const child of [...this.overlay.children]) {
      this.overlay.remove(child);
      child.geometry?.dispose();
      const materials = Array.isArray(child.material) ? child.material : [child.material];
      for (const material of materials) material?.dispose();
    }
    this.markers = {};
    this.tracks = [];
    this.endAxes = null;
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    ++this.epoch;
    ++this.viewer.revision;
    this.payload = null;
    this.clearOverlay();
    this.viewer.scene.remove(this.overlay);
    if (typeof this.viewer.dispose === "function") {
      this.viewer.dispose();
      return;
    }
    this.viewer.resizeObserver.disconnect();
    this.viewer.controls.dispose();
    this.viewer.renderer.setAnimationLoop(null);
    const resources = new Set();
    const collectResources = (node) => {
      if (node.geometry) resources.add(node.geometry);
      const materials = Array.isArray(node.material) ? node.material : [node.material];
      for (const material of materials) {
        if (!material) continue;
        resources.add(material);
        for (const value of Object.values(material)) if (value?.isTexture) resources.add(value);
      }
    };
    this.viewer.scene.traverse(collectResources);
    for (const resource of resources) resource.dispose();
    // Cached or still loading models may contain resources no longer in world.
    for (const pending of this.viewer.assetCache.values()) {
      Promise.resolve(pending).then((source) => {
        source.traverse(collectResources);
        for (const resource of resources) resource.dispose();
      }).catch(() => {});
    }
    this.viewer.assetCache.clear();
    this.viewer.renderer.dispose();
    this.viewer.renderer.domElement.remove();
  }
}
