import { LitElement, html, css, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant, NanitCardConfig } from "./types";

@customElement("nanit-card-editor")
export class NanitCardEditor extends LitElement {
  @property({ attribute: false }) hass!: HomeAssistant;
  @state() private _config!: NanitCardConfig;

  setConfig(config: NanitCardConfig): void {
    this._config = { ...config };
  }

  private _entityChanged(
    key: "camera_entity_id" | "temperature_entity_id" | "humidity_entity_id",
    ev: CustomEvent,
  ): void {
    const value = (ev.detail as { value?: string }).value || undefined;
    if (!this._config || value === this._config[key]) return;

    this._updateConfig({ [key]: value });
  }

  private _toggleChanged(
    key:
      | "hide_baby_name"
      | "hide_connectivity_status"
      | "hide_power_button"
      | "hide_night_light"
      | "hide_sound_machine",
    ev: Event,
  ): void {
    const checked = (ev.target as HTMLInputElement).checked;
    this._updateConfig({ [key]: checked });
  }

  private _updateConfig(patch: Partial<NanitCardConfig>): void {
    const newConfig = { ...this._config, ...patch };
    for (const [key, value] of Object.entries(patch) as [keyof NanitCardConfig, unknown][]) {
      if (value === undefined) delete newConfig[key];
    }
    this._config = newConfig;

    this.dispatchEvent(
      new CustomEvent("config-changed", {
        bubbles: true,
        composed: true,
        detail: { config: newConfig },
      }),
    );
  }

  protected render() {
    if (!this.hass || !this._config) return nothing;

    return html`
      <div class="editor">
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this._config.camera_entity_id || ""}
          .includeDomains=${["camera"]}
          .label=${"Camera Entity"}
          allow-custom-entity
          @value-changed=${(ev: CustomEvent) => this._entityChanged("camera_entity_id", ev)}
        ></ha-entity-picker>
        <label class="toggle-row">
          <span>Hide baby name</span>
          <ha-switch
            .checked=${this._config.hide_baby_name === true}
            @change=${(ev: Event) =>
              this._toggleChanged("hide_baby_name", ev)}
          ></ha-switch>
        </label>
        <label class="toggle-row">
          <span>Hide connectivity status</span>
          <ha-switch
            .checked=${this._config.hide_connectivity_status === true}
            @change=${(ev: Event) =>
              this._toggleChanged("hide_connectivity_status", ev)}
          ></ha-switch>
        </label>
        <label class="toggle-row">
          <span>Hide power button</span>
          <ha-switch
            .checked=${this._config.hide_power_button === true}
            @change=${(ev: Event) =>
              this._toggleChanged("hide_power_button", ev)}
          ></ha-switch>
        </label>
        <label class="toggle-row">
          <span>Hide night light controls</span>
          <ha-switch
            .checked=${this._config.hide_night_light === true}
            @change=${(ev: Event) =>
              this._toggleChanged("hide_night_light", ev)}
          ></ha-switch>
        </label>
        <label class="toggle-row">
          <span>Hide sound machine controls</span>
          <ha-switch
            .checked=${this._config.hide_sound_machine === true}
            @change=${(ev: Event) =>
              this._toggleChanged("hide_sound_machine", ev)}
          ></ha-switch>
        </label>
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this._config.temperature_entity_id || ""}
          .includeDomains=${["sensor"]}
          .label=${"Temperature Entity Override"}
          allow-custom-entity
          @value-changed=${(ev: CustomEvent) => this._entityChanged("temperature_entity_id", ev)}
        ></ha-entity-picker>
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this._config.humidity_entity_id || ""}
          .includeDomains=${["sensor"]}
          .label=${"Humidity Entity Override"}
          allow-custom-entity
          @value-changed=${(ev: CustomEvent) => this._entityChanged("humidity_entity_id", ev)}
        ></ha-entity-picker>
      </div>
    `;
  }

  static styles = css`
    .editor {
      padding: 16px;
    }
    ha-entity-picker {
      display: block;
    }
    .toggle-row {
      align-items: center;
      display: flex;
      justify-content: space-between;
      padding-top: 16px;
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "nanit-card-editor": NanitCardEditor;
  }
}
