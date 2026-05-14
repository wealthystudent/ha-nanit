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

    const newConfig = { ...this._config, camera_entity_id: value };
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
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "nanit-card-editor": NanitCardEditor;
  }
}
