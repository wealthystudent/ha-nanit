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

  private _entityChanged(ev: CustomEvent): void {
    const value = (ev.detail as { value: string }).value;
    if (!this._config || value === this._config.camera_entity_id) return;

    this._updateConfig({ camera_entity_id: value });
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
          @value-changed=${this._entityChanged}
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
