import type { HomeAssistant, NanitEntities } from "./types";

export function resolveEntities(
  hass: HomeAssistant,
  cameraEntityId: string,
): NanitEntities {
  const result: NanitEntities = {};
  result.camera = cameraEntityId;

  const regEntry = hass.entities[cameraEntityId];

  if (regEntry?.device_id) {
    // Primary: device_id matching via entity registry
    const deviceId = regEntry.device_id;
    const siblings: string[] = [];
    for (const eid of Object.keys(hass.entities)) {
      if (
        hass.entities[eid].device_id === deviceId &&
        hass.entities[eid].entity_category !== "diagnostic"
      ) {
        siblings.push(eid);
      }
    }
    assignEntities(result, siblings, hass);
  } else {
    // Fallback: suffix-based matching from camera entity_id
    const cameraKey = cameraEntityId.split(".")[1] ?? "";
    const candidates = Object.keys(hass.states).filter(
      (eid) =>
        eid !== cameraEntityId &&
        eid.split(".")[1]?.startsWith(cameraKey.split("_camera")[0] || cameraKey),
    );
    assignEntities(result, candidates, hass);
  }

  return result;
}

function assignEntities(result: NanitEntities, eids: string[], hass: HomeAssistant): void {
  for (const eid of eids) {
    const [domain] = eid.split(".", 1);
    const suffix = eid.split(".")[1] ?? "";
    const deviceClass = hass.states[eid]?.attributes.device_class as string | undefined;

    if (domain === "sensor") {
      // Match by device_class (locale-independent)
      if (deviceClass === "temperature") result.temperature = eid;
      else if (deviceClass === "humidity") result.humidity = eid;
      else if (deviceClass === "illuminance") result.light = eid;
    } else if (domain === "binary_sensor") {
      if (deviceClass === "motion" || suffix.endsWith("_motion") || suffix.endsWith("_cloud_motion")) {
        result.motion = eid;
      } else if (deviceClass === "sound" || suffix.endsWith("_sound") || suffix.endsWith("_cloud_sound")) {
        result.sound = eid;
      }
    } else if (domain === "switch" && suffix.endsWith("_camera_power")) {
      result.power = eid;
    } else if (domain === "light" && suffix.endsWith("_night_light") && !suffix.includes("sl_")) {
      result.night_light = eid;
    } else if (domain === "media_player" && suffix.endsWith("_sound_machine")) {
      result.sound_machine = eid;
    }
  }
}

export function isEntityAvailable(hass: HomeAssistant, entityId: string | undefined): boolean {
  if (!entityId) return false;
  const reg = hass.entities[entityId];
  // If registered and disabled, not available
  if (reg?.disabled_by) return false;
  // Available if state exists (even without registry entry)
  return entityId in hass.states;
}

export function getDeviceName(hass: HomeAssistant, cameraEntityId: string): string {
  const state = hass.states[cameraEntityId];
  if (!state) return "Nanit";
  const friendly = (state.attributes.friendly_name as string) ?? "Nanit";
  return friendly.replace(/ Camera$/i, "");
}
