// The Elevator Status card, rendered in jsdom against a fake lift. CI: tests.yml "card".


import { JSDOM } from "jsdom";
const dom = new JSDOM("<!doctype html><body></body>", { url: "http://localhost/" });
for (const k of ["window", "document", "HTMLElement", "customElements", "CustomEvent", "history"]) globalThis[k] = dom.window[k];
const { floorWindow, statusLine } = await import("../../custom_components/kone/frontend/kone-elevator-card.js");
let fails = 0; const check = (n, ok, got) => { console.log(`${ok ? "ok  " : "FAIL"} ${n}${ok ? "" : " — " + got}`); if (!ok) fails++; };
check("window keeps the floor central", floorWindow(["G","1","2","3","4","5","6","7","8"], "4", 5).join() === "6,5,4,3,2", floorWindow(["G","1","2","3","4","5","6","7","8"], "4", 5));
check("window at the bottom", floorWindow(["G","1","2","3","4","5","6"], "G", 3).join() === "2,1,G");
check("status moving", statusLine({ floor: "4", target: "6", direction: "up", moving: true }) === "Moving UP (Floor 4 → 6)");
check("status doors", statusLine({ floor: "4", direction: "none", moving: false, doorOpen: true }) === "Doors open at floor 4");
const E = (id, state, attributes = {}) => ({ entity_id: id, state, attributes, last_updated: "2026-10-26T03:32:01Z" });
const p = "sensor.b_lift_1_", b = "binary_sensor.b_lift_1_";
const ids = [p+"floor", p+"direction", p+"motion", p+"mode", b+"door", b+"fault", b+"in_service"];
const hass = {
  devices: { d1: { id: "d1", name: "Main Building A", model: "Elevator" } },
  entities: Object.fromEntries(ids.map((id) => [id, { entity_id: id, device_id: "d1", platform: "kone" }])),
  states: Object.fromEntries([
    E(p+"floor", "4", { floors: ["G","1","2","3","4","5","6"], next_floor: "6", lift_name: "1" }),
    E(p+"direction", "up"), E(p+"motion", "moving"), E(p+"mode", "normal", { code: 0, status: { nominal_speed: 1.2 } }),
    E(b+"door", "off"), E(b+"fault", "off"), E(b+"in_service", "on"),
  ].map((s) => [s.entity_id, s])),
};
const card = document.createElement("kone-elevator-card");
card.setConfig({ type: "custom:kone-elevator-card", device: "Main Building A", title: "Main Building" });
card.hass = hass;
const r = card.shadowRoot;
const txt = (sel) => r.querySelector(sel)?.textContent.replace(/\s+/g, " ").trim();
check("title", txt(".head") === "Elevator Status: Main Building", txt(".head"));
check("lift name on the car", txt(".car .name") === "1", txt(".car .name"));
check("current floor marked", txt(".floor.here") === "4", txt(".floor.here"));
check("seven landings, top first", [...r.querySelectorAll(".floor")].map((e) => e.textContent).join() === "6,5,4,3,2,1,G");
check("up arrow lit, down not", r.querySelector(".arrow.up").classList.contains("on") && !r.querySelector(".arrow.down").classList.contains("on"));
check("status line", txt(".line") === "Moving UP (Floor 4 → 6)", txt(".line"));
check("operation", /Operation: Normal/.test(txt(".facts .left")), txt(".facts .left"));
check("faults none", /Faults: None/.test(txt(".facts .left")));
check("speed and target", /Speed: ?1.2 m\/s/.test(txt(".facts .right")) && /Target floor: ?6/.test(txt(".facts .right")), txt(".facts .right"));
check("doors closed", /Door status: ?Closed/.test(txt(".facts .right")), txt(".facts .right"));
hass.states[b+"fault"] = E(b+"fault", "on"); card.hass = { ...hass };
check("a fault shows on the card", /Faults: Fault/.test(txt(".facts .left")) && r.querySelector(".car").classList.contains("bad"));
check("editor picks KONE lifts", customElements.get("kone-elevator-card").getConfigForm().schema[0].selector.device.filter.integration === "kone");
console.log(fails ? `${fails} failed` : "all passed"); process.exit(fails ? 1 : 0);
