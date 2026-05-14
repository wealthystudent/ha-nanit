import { LitElement, html, nothing, type TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { HomeAssistant, NanitCardConfig, NanitEntities } from "./types";
import { resolveEntities, isEntityAvailable, getDeviceName } from "./utils";
import { cardStyles } from "./styles";
import "./nanit-card-editor";

@customElement("nanit-card")
export class NanitCard extends LitElement {
  @property({ attribute: false }) hass!: HomeAssistant;
  @state() private _config!: NanitCardConfig;

  static styles = cardStyles;

  static getConfigElement() {
    return document.createElement("nanit-card-editor");
  }

  static getStubConfig(hass: HomeAssistant) {
    const cameraEntity = Object.keys(hass.states).find(
      (e) => e.startsWith("camera.") && hass.entities[e]?.platform === "nanit",
    );
    return { type: "custom:nanit-card", camera_entity_id: cameraEntity || "" };
  }

  setConfig(config: NanitCardConfig): void {
    if (!config) throw new Error("Invalid configuration");
    this._config = config;
  }

  getCardSize(): number {
    return 5;
  }

  private _entities(): NanitEntities {
    if (!this._config?.camera_entity_id || !this.hass) return {};
    return resolveEntities(this.hass, this._config.camera_entity_id);
  }

  private _isCameraOn(entities: NanitEntities): boolean {
    if (!entities.power) return true;
    return this.hass.states[entities.power]?.state === "on";
  }

  private _fireMoreInfo(entityId: string): void {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        bubbles: true,
        composed: true,
        detail: { entityId },
      }),
    );
  }

  private _toggleService(domain: string, service: string, entityId: string): void {
    this.hass.callService(domain, service, { entity_id: entityId });
  }

  protected render(): TemplateResult {
    if (!this.hass || !this._config) {
      return html`<ha-card><div class="header"><span class="device-name">Nanit</span></div></ha-card>`;
    }

    const entities = this._entities();
    const cameraOn = this._isCameraOn(entities);
    const deviceName = entities.camera
      ? getDeviceName(this.hass, entities.camera)
      : "Nanit";

    return html`
      <ha-card>
        ${this._renderHeader(deviceName, entities, cameraOn)}
        <div class="card-content ${cameraOn ? "" : "collapsed"}">
          ${cameraOn ? this._renderStream(entities) : nothing}
          ${cameraOn ? this._renderControls(entities) : nothing}
        </div>
      </ha-card>
    `;
  }

  private _renderHeader(
    deviceName: string,
    entities: NanitEntities,
    cameraOn: boolean,
  ): TemplateResult {
    return html`
      <div class="header">
        <span class="device-name">${deviceName}</span>
        ${!cameraOn
          ? html`<span class="camera-off-label">Camera Off</span>`
          : nothing}
        ${entities.power
          ? html`
              <button
                class="power-btn ${cameraOn ? "" : "off"}"
                @click=${() => this._toggleService("switch", "toggle", entities.power!)}
              >
                <ha-icon icon="mdi:power"></ha-icon>
              </button>
            `
          : nothing}
      </div>
    `;
  }

  private _renderStream(entities: NanitEntities): TemplateResult {
    const cameraState = entities.camera
      ? this.hass.states[entities.camera]
      : undefined;

    return html`
      <div class="stream-wrap">
        ${cameraState
          ? html`
              <div
                class="stream-click"
                @click=${() => entities.camera && this._fireMoreInfo(entities.camera)}
              >
                <ha-camera-stream
                  muted
                  .hass=${this.hass}
                  .stateObj=${cameraState}
                ></ha-camera-stream>
              </div>
            `
          : html`
              <div
                class="stream-placeholder"
                @click=${() => entities.camera && this._fireMoreInfo(entities.camera)}
              >
                <ha-icon icon="mdi:camera-off"></ha-icon>
              </div>
            `}
        ${this._renderSensorOverlays(entities)}
        ${this._renderDetectionOverlays(entities)}
      </div>
    `;
  }

  private _renderSensorOverlays(entities: NanitEntities): TemplateResult {
    const pills: TemplateResult[] = [];

    if (isEntityAvailable(this.hass, entities.temperature)) {
      const val = this.hass.states[entities.temperature!].state;
      const unit = (this.hass.states[entities.temperature!].attributes.unit_of_measurement as string) ?? "";
      pills.push(html`
        <div class="pill pill-temp" @click=${() => this._fireMoreInfo(entities.temperature!)}>
          <ha-icon icon="mdi:thermometer"></ha-icon>
          <span>${val}${unit}</span>
        </div>
      `);
    }

    if (isEntityAvailable(this.hass, entities.humidity)) {
      const val = this.hass.states[entities.humidity!].state;
      pills.push(html`
        <div class="pill pill-humid" @click=${() => this._fireMoreInfo(entities.humidity!)}>
          <ha-icon icon="mdi:water-percent"></ha-icon>
          <span>${val}%</span>
        </div>
      `);
    }

    if (isEntityAvailable(this.hass, entities.light)) {
      const val = this.hass.states[entities.light!].state;
      pills.push(html`
        <div class="pill pill-light" @click=${() => this._fireMoreInfo(entities.light!)}>
          <ha-icon icon="mdi:brightness-5"></ha-icon>
          <span>${val} lx</span>
        </div>
      `);
    }

    if (pills.length === 0) return html``;
    return html`<div class="overlay-top">${pills}</div>`;
  }

  private _renderDetectionOverlays(entities: NanitEntities): TemplateResult {
    const hasMotion = isEntityAvailable(this.hass, entities.motion);
    const hasSound = isEntityAvailable(this.hass, entities.sound);
    if (!hasMotion && !hasSound) return html``;

    const motionOn = hasMotion && this.hass.states[entities.motion!].state === "on";
    const soundOn = hasSound && this.hass.states[entities.sound!].state === "on";

    return html`
      <div class="overlay-bottom">
        ${hasMotion
          ? html`
              <div
                class="pill ${motionOn ? "active motion-active" : ""}"
                @click=${() => this._fireMoreInfo(entities.motion!)}
              >
                <ha-icon icon="mdi:motion-sensor"></ha-icon>
                <span>${motionOn ? "Motion" : "Clear"}</span>
              </div>
            `
          : html`<div></div>`}
        ${hasSound
          ? html`
              <div
                class="pill ${soundOn ? "active sound-active" : ""}"
                @click=${() => this._fireMoreInfo(entities.sound!)}
              >
                <ha-icon icon="mdi:ear-hearing"></ha-icon>
                <span>${soundOn ? "Sound" : "Quiet"}</span>
              </div>
            `
          : nothing}
      </div>
    `;
  }

  private _renderControls(entities: NanitEntities): TemplateResult {
    const hasNightLight = isEntityAvailable(this.hass, entities.night_light);
    const hasSoundMachine = isEntityAvailable(this.hass, entities.sound_machine);
    if (!hasNightLight && !hasSoundMachine) return html``;

    return html`
      <div class="controls">
        ${hasNightLight ? this._renderNightLight(entities.night_light!) : nothing}
        ${hasSoundMachine ? this._renderSoundMachine(entities.sound_machine!) : nothing}
      </div>
    `;
  }

  private _renderNightLight(entityId: string): TemplateResult {
    const state = this.hass.states[entityId];
    const isOn = state?.state === "on";
    const brightness = (state?.attributes.brightness as number) ?? 0;
    const brightnessPercent = Math.round((brightness / 255) * 100);

    return html`
      <div class="control-section">
        <span class="control-label">Night Light</span>
        <div class="control-row">
          <button
            class="icon-btn ${isOn ? "active" : ""}"
            @click=${() => this._toggleService("light", "toggle", entityId)}
          >
            <ha-icon icon="mdi:lightbulb${isOn ? "" : "-outline"}"></ha-icon>
          </button>
          <div class="slider-row">
            <ha-slider
              .min=${0}
              .max=${100}
              .value=${brightnessPercent}
              .disabled=${!isOn}
              @change=${(ev: Event) => {
                const val = Number((ev.target as HTMLInputElement).value);
                this.hass.callService("light", "turn_on", {
                  entity_id: entityId,
                  brightness: Math.round((val / 100) * 255),
                });
              }}
            ></ha-slider>
            <span class="slider-value">${isOn ? `${brightnessPercent}%` : "Off"}</span>
          </div>
        </div>
      </div>
    `;
  }

  private _renderSoundMachine(entityId: string): TemplateResult {
    const state = this.hass.states[entityId];
    const isPlaying = state?.state === "playing";
    const currentSource = (state?.attributes.source as string) ?? "";
    const sourceList = (state?.attributes.source_list as string[]) ?? [];
    const volume = (state?.attributes.volume_level as number) ?? 0;
    const volumePercent = Math.round(volume * 100);

    return html`
      <div class="control-section">
        <span class="control-label">Sound Machine</span>
        <div class="control-row">
          <button
            class="icon-btn ${isPlaying ? "active" : ""}"
            @click=${() =>
              this._toggleService(
                "media_player",
                isPlaying ? "media_stop" : "media_play",
                entityId,
              )}
          >
            <ha-icon icon="mdi:${isPlaying ? "stop" : "play"}"></ha-icon>
          </button>
          <span class="track-name">${this._formatSourceName(currentSource) || "No track"}</span>
          <div class="slider-row">
            <ha-slider
              .min=${0}
              .max=${100}
              .value=${volumePercent}
              @change=${(ev: Event) => {
                const val = Number((ev.target as HTMLInputElement).value);
                this.hass.callService("media_player", "volume_set", {
                  entity_id: entityId,
                  volume_level: val / 100,
                });
              }}
            ></ha-slider>
            <span class="slider-value">${volumePercent}%</span>
          </div>
        </div>
        ${sourceList.length > 0
          ? html`
              <div class="source-list">
                ${sourceList.map(
                  (source) => html`
                    <button
                      class="source-chip ${source === currentSource ? "active" : ""}"
                      @click=${() =>
                        this.hass.callService("media_player", "select_source", {
                          entity_id: entityId,
                          source,
                        })}
                    >
                      ${this._formatSourceName(source)}
                    </button>
                  `,
                )}
              </div>
            `
          : nothing}
      </div>
    `;
  }

  private _formatSourceName(source: string): string {
    return source.replace(/\.wav$/i, "").replace(/_/g, " ");
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "nanit-card": NanitCard;
  }
  interface Window {
    customCards?: Array<{
      type: string;
      name: string;
      description: string;
      preview: boolean;
    }>;
  }
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "nanit-card",
  name: "Nanit Camera",
  description: "Camera stream with controls for Nanit baby cameras",
  preview: true,
});
