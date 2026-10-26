/*
 * KONE Elevator Status card.
 *
 * One lift: the car in its shaft with the lift's name on it, the landings
 * beside it with the current one marked, arrows that light while it moves,
 * and its operation, faults, direction, speed, target floor and doors.
 *
 * Shipped with the KONE integration and loaded by it on every page, so it
 * needs no dashboard resource. Point it at a lift's device:
 *
 *   type: custom:kone-elevator-card
 *   device: <a KONE lift device>
 *   title: Main Building        # optional
 *   floors_shown: 7             # optional, landings in the column
 */
const CARD = "kone-elevator-card";

// The entity a lift device has for each reading, by the end of its unique id.
const ROLES = ["floor", "direction", "motion", "door", "fault", "mode", "in_service"];

const MODE_NAMES = {
  normal: "Normal", out_of_service: "Out of service", inspection: "Inspection drive",
  fireman_service: "Fireman's service", emergency_power: "Emergency power",
  attendant: "Attendant service", freight: "Freight service", vip_service: "VIP service",
  independent: "Independent service", hospital_emergency: "Hospital emergency",
  hospital_bed: "Hospital bed service", priority_call: "Priority call",
  priority_call_empty_car: "Priority call, empty car", separate_collective: "Separate collective",
  failed: "Failed", special_parking: "Special parking", abnormal_dispatch: "Abnormal dispatch",
  landing_calls_disconnected: "Landing calls off", proxy_disconnected: "Proxy connection lost",
  safety_chain_break: "Safety chain break", earthquake: "Earthquake",
  parked_doors_closed: "Parked, doors closed", car_hospital: "Car hospital service",
  reserve_dispatch: "Reserve dispatch", agv: "Reserved for AGV",
  wind_reduced_speed: "Strong wind, slow", storm_stopped: "Storm, stopped",
  reduced_speed: "Reduced speed", evacuation: "Evacuation", cleaning_pending: "Going to cleaning",
  cleaning: "Cleaning", maintenance_pending: "Going to maintenance", invasion: "Invasion mode",
  setup_drive: "Setup drive", substance_release: "Substance release",
  fire_detection: "Fire detection", fire_machine_room: "Fire in machine room",
  penthouse_visitor: "Penthouse visitor", priority_goods: "Priority goods", recover: "Recovering",
  reserved: "Reserved", car_to_lobby: "Car to lobby", pet: "Pet transfer", empty_car: "Empty car",
  medical_emergency: "Medical emergency", retail: "Retail mode", vip_low: "VIP call (low)",
  vip_high: "VIP call (high)", vip_only: "VIP only", pandemic: "Pandemic mode",
  transformer: "Transformer mode", pit_inspection_failure: "Pit inspection failure",
  luggage: "Luggage handling", roof_access: "Technician roof access",
  pit_access: "Technician pit access", other: "Other",
};

const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const live = (s) => s && !["unknown", "unavailable"].includes(s.state);

/** A device id from the config: an id, or a device's (or user-given) name. */
function deviceIdOf(hass, device) {
  const devices = hass.devices || {};
  if (devices[device]) return device;
  const wanted = String(device || "").trim().toLowerCase();
  const hit = Object.values(devices).find(
    (d) => (d.name_by_user || d.name || "").trim().toLowerCase() === wanted);
  return hit ? hit.id : undefined;
}

/** role -> entity_id for a lift device, by the end of each entity's unique id. */
function entitiesOf(hass, deviceId) {
  const out = {};
  for (const e of Object.values(hass.entities || {})) {
    if (e.device_id !== deviceId || e.platform !== "kone") continue;
    const id = e.entity_id;
    for (const role of ROLES) {
      if (id.endsWith(`_${role}`) && !(role === "floor" && id.endsWith("_to_floor"))) out[role] = id;
    }
  }
  return out;
}

/** The landings to show: `count` of them, the current one as near the middle as fits. */
export function floorWindow(floors, current, count = 7) {
  if (!floors.length) return current != null ? [String(current)] : [];
  if (floors.length <= count) return [...floors].reverse();
  let at = floors.indexOf(String(current));
  if (at < 0) at = 0;
  let start = Math.max(0, Math.min(at - Math.floor(count / 2), floors.length - count));
  return floors.slice(start, start + count).reverse();
}

/** "Moving UP (Floor 4 → 6)", "Doors open at 4", "Stopped at 4". */
export function statusLine({ floor, target, direction, moving, doorOpen }) {
  const at = floor ?? "?";
  if (moving && (direction === "up" || direction === "down")) {
    const to = target && target !== floor ? ` → ${target}` : "";
    return `Moving ${direction.toUpperCase()} (Floor ${at}${to})`;
  }
  if (doorOpen) return `Doors open at floor ${at}`;
  return `Stopped at floor ${at}`;
}

class KoneElevatorCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.device) throw new Error("device is required: a KONE lift device");
    this._config = { floors_shown: 7, ...config };
    this._key = undefined;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 7;
  }

  static getStubConfig(hass) {
    const lift = Object.values(hass?.devices || {}).find(
      (d) => d.model === "Elevator" && Object.values(hass.entities || {}).some(
        (e) => e.device_id === d.id && e.platform === "kone"));
    return lift ? { device: lift.id } : {};
  }

  static getConfigForm() {
    return {
      schema: [
        { name: "device", required: true,
          selector: { device: { filter: { integration: "kone", model: "Elevator" } } } },
        { name: "title", selector: { text: {} } },
        { name: "floors_shown", selector: { number: { min: 3, max: 15, mode: "box" } } },
      ],
      computeLabel: (f) => ({ device: "Lift", title: "Title (e.g. the building)",
        floors_shown: "Landings shown" })[f.name] ?? f.name,
    };
  }

  _render() {
    const hass = this._hass;
    if (!hass || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const deviceId = deviceIdOf(hass, this._config.device);
    const ents = deviceId ? entitiesOf(hass, deviceId) : {};
    if (!ents.floor) {
      this.shadowRoot.innerHTML = `<ha-card><div style="padding:16px">${esc(this._config.device)} is not a KONE lift.</div></ha-card>`;
      return;
    }
    const st = (role) => hass.states[ents[role]];
    const floorState = st("floor");
    const floor = live(floorState) ? floorState.state : null;
    const fa = floorState?.attributes || {};
    const target = fa.next_floor ?? null;
    const dirState = st("direction");
    const direction = live(dirState) ? dirState.state : "none";
    const motion = st("motion");
    const moving = live(motion) && !["standing", "stopped", "idle"].includes(String(motion.state).toLowerCase())
      && direction !== "none";
    const door = st("door");
    const doorOpen = door?.state === "on";
    const fault = st("fault");
    const faulted = fault?.state === "on";
    const mode = st("mode");
    const modeKey = live(mode) ? mode.state : null;
    const inService = st("in_service")?.state === "on";
    const speed = mode?.attributes?.status?.nominal_speed;
    const liftName = fa.lift_name || (hass.devices[deviceId]?.name || "").split(" ").pop() || "?";
    const updated = floorState?.last_updated ? new Date(floorState.last_updated) : null;

    const key = JSON.stringify([floor, target, direction, moving, doorOpen, faulted, modeKey, inService,
      speed, liftName, fa.floors, this._config.title, updated && updated.getTime()]);
    if (key === this._key) return;
    this._key = key;

    const floors = (fa.floors || []).map(String);
    const shown = floorWindow(floors, floor, Number(this._config.floors_shown) || 7);
    const opText = faulted ? "Fault" : modeKey ? (MODE_NAMES[modeKey] || modeKey) : "Unknown";
    const opClass = faulted ? "bad" : inService ? "good" : modeKey ? "warn" : "dim";
    const title = this._config.title
      ? `Elevator Status: ${this._config.title}` : `Elevator ${liftName}`;
    const dirText = { up: "Ascending", down: "Descending" }[direction] || "Stopped";

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head"><ha-icon icon="mdi:elevator-passenger"></ha-icon><span>${esc(title)}</span></div>
        <div class="body">
          <div class="floors">
            ${shown.map((f) => `<div class="floor ${f === String(floor) ? "here" : ""}${f === String(target) && moving ? " target" : ""}">${esc(f)}</div>`).join("")}
          </div>
          <div class="pointer"></div>
          <div class="shaft">
            <div class="rail left"></div><div class="rail right"></div>
            <div class="arrow up ${moving && direction === "up" ? "on" : ""}"><span>UP</span></div>
            <div class="car ${doorOpen ? "open" : ""} ${opClass}">
              <div class="door l"></div><div class="door r"></div>
              <div class="name">${esc(liftName)}</div>
            </div>
            <div class="arrow down ${moving && direction === "down" ? "on" : ""}"><span>DOWN</span></div>
          </div>
        </div>
        <div class="line">${esc(statusLine({ floor, target, direction, moving, doorOpen }))}</div>
        <div class="facts">
          <div class="left">
            <div><b>Operation:</b> <span class="${opClass}">${esc(opText)}</span> <i class="dot ${opClass}"></i></div>
            <div><b>Faults:</b> ${faulted ? '<span class="bad">Fault</span> <ha-icon class="bad" icon="mdi:alert-circle"></ha-icon>'
              : fault && live(fault) ? '<span class="good">None</span> <ha-icon class="good" icon="mdi:check-circle"></ha-icon>'
              : '<span class="dim">Unknown</span>'}</div>
          </div>
          <div class="right">
            <span>Direction:</span><span class="${direction === "none" ? "" : "good"}">${dirText}</span>
            <span>Speed:</span><span>${speed != null ? `${esc(speed)} m/s` : "–"}</span>
            <span>Target floor:</span><span>${esc(target ?? "–")}</span>
            <span>Door status:</span><span class="${doorOpen ? "warn" : "good"}">${door ? (doorOpen ? "Open" : "Closed") : "–"}</span>
            ${fa.deck_floors ? `<span>Decks:</span><span>${fa.deck_floors.map(esc).join(" / ")}</span>` : ""}
          </div>
        </div>
        <div class="foot">
          <span>Updated: ${updated ? updated.toLocaleTimeString() : "–"}</span>
          <span class="tools">
            <ha-icon-button-like data-act="settings" title="Lift device"><ha-icon icon="mdi:cog"></ha-icon></ha-icon-button-like>
            <ha-icon-button-like data-act="history" title="History"><ha-icon icon="mdi:chart-line"></ha-icon></ha-icon-button-like>
          </span>
        </div>
      </ha-card>`;
    this.shadowRoot.querySelectorAll("[data-act]").forEach((el) => el.addEventListener("click", () => {
      if (el.dataset.act === "history") {
        this.dispatchEvent(new CustomEvent("hass-more-info", {
          detail: { entityId: ents.floor }, bubbles: true, composed: true }));
      } else {
        history.pushState(null, "", `/config/devices/device/${deviceId}`);
        window.dispatchEvent(new CustomEvent("location-changed"));
      }
    }));
  }
}

const STYLE = `
  ha-card { padding: 14px 16px 10px; }
  .head { display: flex; align-items: center; gap: 10px; font-size: 1.15em; margin-bottom: 10px; }
  .body { display: flex; align-items: center; justify-content: center; gap: 10px; }
  .floors { display: flex; flex-direction: column; gap: 2px; border: 1px solid var(--divider-color);
    border-radius: 8px; padding: 3px; }
  .floor { width: 34px; height: 30px; display: flex; align-items: center; justify-content: center;
    border-radius: 6px; font-size: 1.05em; color: var(--secondary-text-color); }
  .floor.here { color: var(--primary-text-color); font-weight: 600;
    box-shadow: 0 0 0 2px var(--primary-color), 0 0 10px var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 22%, transparent); }
  .floor.target { outline: 1px dashed var(--secondary-text-color); }
  .pointer { width: 0; height: 0; border: 7px solid transparent; border-right-color: var(--primary-color);
    margin-left: -6px; }
  .shaft { position: relative; width: 170px; height: 230px; border-radius: 10px;
    background: linear-gradient(90deg, #1b1d21, #2a2d33 50%, #1b1d21); display: flex;
    flex-direction: column; align-items: center; justify-content: space-between; padding: 8px 0; }
  .rail { position: absolute; top: 6px; bottom: 6px; width: 6px; border-radius: 3px;
    background: linear-gradient(90deg, #8a9099, #c9ced6, #8a9099); }
  .rail.left { left: 12px; } .rail.right { right: 12px; }
  .arrow { width: 44px; height: 38px; display: flex; align-items: flex-end; justify-content: center;
    font-size: 0.72em; font-weight: 700; color: #1b1d21; background: #4a4e55;
    clip-path: polygon(50% 0, 100% 45%, 70% 45%, 70% 100%, 30% 100%, 30% 45%, 0 45%); opacity: .55; }
  .arrow.down { transform: rotate(180deg); }
  .arrow.down span { transform: rotate(180deg); }
  .arrow span { padding-bottom: 3px; }
  .arrow.on { background: #ffa726; opacity: 1; filter: drop-shadow(0 0 8px #ffa726);
    animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .55; } }
  .car { position: relative; width: 104px; height: 104px; border-radius: 12px; overflow: hidden;
    background: linear-gradient(160deg, #e9edf3, #b7bfca); border: 4px solid #7d8590;
    box-shadow: 0 4px 14px rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; }
  .car.bad { border-color: var(--error-color, #db4437); }
  .car.warn { border-color: var(--warning-color, #ffa600); }
  .door { position: absolute; top: 0; bottom: 0; width: 50%; background: linear-gradient(90deg, #d5dbe3, #c3cad4);
    transition: transform .6s ease; }
  .door.l { left: 0; border-right: 1px solid #9aa2ad; } .door.r { right: 0; border-left: 1px solid #9aa2ad; }
  .car.open .door.l { transform: translateX(-80%); } .car.open .door.r { transform: translateX(80%); }
  .name { position: relative; font-size: 3.2em; font-weight: 700; color: #202328; }
  .line { text-align: center; font-size: 1.05em; margin: 10px 0 8px; }
  .facts { display: flex; justify-content: space-between; gap: 12px; font-size: .92em; }
  .facts .left { display: flex; flex-direction: column; gap: 8px; }
  .facts .right { display: grid; grid-template-columns: auto auto; column-gap: 10px; row-gap: 2px; }
  .facts .right span:nth-child(even) { text-align: right; }
  .good { color: var(--success-color, #43a047); } .bad { color: var(--error-color, #db4437); }
  .warn { color: var(--warning-color, #ffa600); } .dim { color: var(--secondary-text-color); }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: currentColor; vertical-align: middle; }
  .dot.good { background: var(--success-color, #43a047); } .dot.bad { background: var(--error-color, #db4437); }
  .dot.warn { background: var(--warning-color, #ffa600); } .dot.dim { background: var(--disabled-text-color, #888); }
  ha-icon.good, ha-icon.bad { --mdc-icon-size: 18px; vertical-align: middle; }
  .foot { display: flex; justify-content: space-between; align-items: center; margin-top: 10px;
    padding-top: 8px; border-top: 1px solid var(--divider-color); color: var(--secondary-text-color); font-size: .85em; }
  .tools { display: flex; gap: 10px; }
  .tools [data-act] { cursor: pointer; color: var(--primary-text-color); }
`;

// Loaded on every page by the integration, and perhaps by a resource too:
// a second copy stands down.
if (!customElements.get(CARD)) {
  customElements.define(CARD, KoneElevatorCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD,
    name: "KONE Elevator Status",
    description: "One lift in its shaft: floor, direction, doors, operation and faults.",
    preview: true,
    documentationURL: "https://github.com/rellis-erigon/KONE-HA",
  });
}
