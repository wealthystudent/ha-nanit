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
  @state() private _streamLoaded = false;
  @state() private _showNetwork = false;
  private _streamCheckTimer?: ReturnType<typeof setTimeout>;

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

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this._clearStreamCheck();
  }

  protected updated(changedProps: Map<string, unknown>): void {
    super.updated(changedProps);
    if (!this._streamLoaded && !this._streamCheckTimer) {
      const streamEl = this.renderRoot.querySelector("ha-camera-stream");
      if (streamEl) this._scheduleStreamCheck();
    }
  }

  private _scheduleStreamCheck(): void {
    this._streamCheckTimer = setTimeout(() => {
      this._streamCheckTimer = undefined;
      const streamEl = this.renderRoot.querySelector("ha-camera-stream");
      if (!streamEl) return;
      const root = streamEl.shadowRoot;
      if (root?.querySelector("video, img, canvas")) {
        this._streamLoaded = true;
      } else {
        this._scheduleStreamCheck();
      }
    }, 500);
  }

  private _clearStreamCheck(): void {
    if (this._streamCheckTimer) {
      clearTimeout(this._streamCheckTimer);
      this._streamCheckTimer = undefined;
    }
  }

  private _onStreamLoad(): void {
    this._streamLoaded = true;
    this._clearStreamCheck();
  }

  protected render(): TemplateResult {
    if (!this.hass || !this._config) {
      return html`<ha-card><div class="header"><span class="device-name">Nanit</span></div></ha-card>`;
    }

    const entities = this._entities();
    const cameraOn = this._isCameraOn(entities);
    if (!cameraOn && this._streamLoaded) {
      this._streamLoaded = false;
      this._clearStreamCheck();
    }
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
    const hasWifi = isEntityAvailable(this.hass, entities.wifi_ssid)
      || isEntityAvailable(this.hass, entities.wifi_signal)
      || isEntityAvailable(this.hass, entities.wifi_frequency);

    return html`
      <div class="header">
        <div class="device-badge">
          <ha-icon icon="mdi:baby-face-outline"></ha-icon>
          <span class="device-name">${deviceName}</span>
        </div>
        ${!cameraOn
          ? html`<span class="camera-off-label">Camera Off</span>`
          : nothing}
        <div class="header-actions">
          ${hasWifi
            ? html`
                <button
                  class="wifi-btn"
                  @click=${() => { this._showNetwork = !this._showNetwork; }}
                >
                  <ha-icon icon="mdi:wifi"></ha-icon>
                </button>
              `
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
      </div>
      ${this._showNetwork ? this._renderNetworkPopup(entities) : nothing}
    `;
  }

  private _renderNetworkPopup(entities: NanitEntities): TemplateResult {
    const ssid = entities.wifi_ssid ? this.hass.states[entities.wifi_ssid]?.state : undefined;
    const signal = entities.wifi_signal ? this.hass.states[entities.wifi_signal]?.state : undefined;
    const signalUnit = entities.wifi_signal
      ? (this.hass.states[entities.wifi_signal]?.attributes.unit_of_measurement as string) ?? "dBm"
      : "dBm";
    const freq = entities.wifi_frequency ? this.hass.states[entities.wifi_frequency]?.state : undefined;
    const freqUnit = entities.wifi_frequency
      ? (this.hass.states[entities.wifi_frequency]?.attributes.unit_of_measurement as string) ?? "MHz"
      : "MHz";

    const signalNum = signal ? parseInt(signal, 10) : -100;
    let signalLabel = "Weak";
    let signalColor = "#e74c3c";
    if (signalNum >= -50) { signalLabel = "Excellent"; signalColor = "#2ecc71"; }
    else if (signalNum >= -60) { signalLabel = "Good"; signalColor = "var(--nanit-teal)"; }
    else if (signalNum >= -70) { signalLabel = "Fair"; signalColor = "var(--nanit-amber)"; }

    return html`
      <div class="network-backdrop" @click=${() => { this._showNetwork = false; }}></div>
      <div class="network-popup">
        <div class="network-header">
          <ha-icon icon="mdi:wifi"></ha-icon>
          <span>Network</span>
        </div>
        ${ssid
          ? html`
              <div class="network-row">
                <ha-icon icon="mdi:router-wireless"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">WiFi Name</span>
                  <span class="network-value">${ssid}</span>
                </div>
              </div>
            `
          : nothing}
        ${signal
          ? html`
              <div class="network-row">
                <ha-icon icon="mdi:signal" style="color: ${signalColor}"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">Signal Strength</span>
                  <span class="network-value">${signal} ${signalUnit} · <span style="color: ${signalColor}">${signalLabel}</span></span>
                </div>
              </div>
            `
          : nothing}
        ${freq
          ? html`
              <div class="network-row">
                <ha-icon icon="mdi:frequency"></ha-icon>
                <div class="network-detail">
                  <span class="network-label">Frequency</span>
                  <span class="network-value">${freq} ${freqUnit}</span>
                </div>
              </div>
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
                  @load=${this._onStreamLoad}
                ></ha-camera-stream>
              </div>
              <div class="stream-loader ${this._streamLoaded ? "hidden" : ""}">
                <div class="loader-content">
                  <ha-icon icon="mdi:camera"></ha-icon>
                  <div class="loader-spinner"></div>
                </div>
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
      const val = parseFloat(this.hass.states[entities.temperature!].state);
      const display = isNaN(val) ? this.hass.states[entities.temperature!].state : val.toFixed(1);
      const unit = (this.hass.states[entities.temperature!].attributes.unit_of_measurement as string) ?? "";
      pills.push(html`
        <div class="pill pill-temp" @click=${() => this._fireMoreInfo(entities.temperature!)}>
          <ha-icon icon="mdi:thermometer"></ha-icon>
          <span>${display}${unit}</span>
        </div>
      `);
    }

    if (isEntityAvailable(this.hass, entities.humidity)) {
      const val = parseFloat(this.hass.states[entities.humidity!].state);
      const display = isNaN(val) ? this.hass.states[entities.humidity!].state : val.toFixed(1);
      pills.push(html`
        <div class="pill pill-humid" @click=${() => this._fireMoreInfo(entities.humidity!)}>
          <ha-icon icon="mdi:water-percent"></ha-icon>
          <span>${display}%</span>
        </div>
      `);
    }

    if (isEntityAvailable(this.hass, entities.light)) {
      const val = parseFloat(this.hass.states[entities.light!].state);
      const display = isNaN(val) ? this.hass.states[entities.light!].state : Math.round(val).toString();
      pills.push(html`
        <div class="pill pill-light" @click=${() => this._fireMoreInfo(entities.light!)}>
          <ha-icon icon="mdi:brightness-5"></ha-icon>
          <span>${display} lx</span>
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
      <div class="control-section control-section-light">
        <span class="control-label">Night Light</span>
        <div class="control-row">
          <button
            class="icon-btn ${isOn ? "active" : ""}"
            @click=${() => this._toggleService("light", "toggle", entityId)}
          >
            <ha-icon icon="mdi:lightbulb${isOn ? "" : "-outline"}"></ha-icon>
          </button>
          <div class="slider-row">
            <div class="nanit-slider" style="--slider-pct: ${brightnessPercent}%">
              <input
                type="range"
                min="0"
                max="100"
                .value=${String(brightnessPercent)}
                @input=${(ev: Event) => {
                  const slider = (ev.target as HTMLInputElement).closest(".nanit-slider") as HTMLElement;
                  if (slider) slider.style.setProperty("--slider-pct", `${(ev.target as HTMLInputElement).value}%`);
                }}
                @change=${(ev: Event) => {
                  const val = Number((ev.target as HTMLInputElement).value);
                  if (val === 0) {
                    this.hass.callService("light", "turn_off", {
                      entity_id: entityId,
                    });
                  } else {
                    this.hass.callService("light", "turn_on", {
                      entity_id: entityId,
                      brightness: Math.round((val / 100) * 255),
                    });
                  }
                }}
              />
            </div>
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
      <div class="control-section control-section-sound">
        <div class="section-header">
          <span class="control-label">Sound Machine</span>
          ${sourceList.length > 0
            ? html`
                <div class="source-list">
                  ${sourceList.map(
                    (source) => html`
                      <button
                        class="source-icon ${source === currentSource ? "active" : ""}"
                        title=${this._formatSourceName(source)}
                        @click=${() =>
                          this.hass.callService("media_player", "select_source", {
                            entity_id: entityId,
                            source,
                          })}
                      >
                        <ha-icon icon=${this._sourceIcon(source)}></ha-icon>
                      </button>
                    `,
                  )}
                </div>
              `
            : nothing}
        </div>
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
          ${isPlaying
            ? html`<span class="track-name">${this._formatSourceName(currentSource)}</span>`
            : nothing}
          <div class="slider-row">
            <div class="nanit-slider" style="--slider-pct: ${volumePercent}%">
              <input
                type="range"
                min="0"
                max="100"
                .value=${String(volumePercent)}
                @input=${(ev: Event) => {
                  const slider = (ev.target as HTMLInputElement).closest(".nanit-slider") as HTMLElement;
                  if (slider) slider.style.setProperty("--slider-pct", `${(ev.target as HTMLInputElement).value}%`);
                }}
                @change=${(ev: Event) => {
                  const val = Number((ev.target as HTMLInputElement).value);
                  this.hass.callService("media_player", "volume_set", {
                    entity_id: entityId,
                    volume_level: val / 100,
                  });
                }}
              />
            </div>
            <span class="slider-value">${volumePercent}%</span>
          </div>
        </div>
      </div>
    `;
  }

  private _sourceIcon(source: string): string {
    const key = source.replace(/\.wav$/i, "").toLowerCase();
    const icons: Record<string, string> = {
      white_noise: "mdi:sine-wave",
      birds: "mdi:bird",
      waves: "mdi:waves",
      wind: "mdi:weather-windy",
      rain: "mdi:weather-rainy",
      water_stream: "mdi:water",
      fan: "mdi:fan",
      heartbeat: "mdi:heart-pulse",
      dryer: "mdi:tumble-dryer",
      vacuum: "mdi:robot-vacuum",
    };
    return icons[key] ?? "mdi:music-note";
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
