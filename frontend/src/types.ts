export interface HomeAssistant {
  states: Record<string, HassEntity>;
  entities: Record<string, HassEntityRegistryEntry>;
  callService(
    domain: string,
    service: string,
    data?: Record<string, unknown>,
  ): Promise<void>;
}

export interface HassEntity {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
  last_changed: string;
  last_updated: string;
}

export interface HassEntityRegistryEntry {
  entity_id: string;
  device_id?: string;
  disabled_by?: string | null;
  entity_category?: string | null;
  platform: string;
}

export interface NanitCardConfig {
  type: string;
  camera_entity_id?: string;
}

export interface NanitEntities {
  camera?: string;
  power?: string;
  temperature?: string;
  humidity?: string;
  light?: string;
  motion?: string;
  sound?: string;
  night_light?: string;
  sound_machine?: string;
}
