import { LitElement, html, nothing, type TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import { keyed } from "lit/directives/keyed.js";
import type { HomeAssistant, NanitCardConfig, NanitEntities } from "./types";
import { resolveEntities, isEntityAvailable, getDeviceName } from "./utils";
import { cardStyles } from "./styles";
import "./nanit-card-editor";

const STREAM_WATCHDOG_INTERVAL_MS = 1000;
const STREAM_STARTUP_RELOAD_TICKS = 8;
const STREAM_STALL_TICKS = 8;
const STREAM_MAX_RELOADS = 3;
const STREAM_RELOAD_COOLDOWN_MS = 60000;
const STREAM_HEALTHY_RESET_TICKS = 10;
const STREAM_PROGRESS_EPSILON = 0.05;
// Hide the loader after this long even if we can't confirm liveness, so a
// detection miss (WebRTC currentTime quirks, blocked inline autoplay, unusual
// player nesting) can never permanently mask a working stream.
const STREAM_LOADED_FAILOPEN_MS = 10000;

@customElement("nanit-card")
export class NanitCard extends LitElement {
  @property({ attribute: false }) hass!: HomeAssistant;
  @state() private _config!: NanitCardConfig;
  @state() private _streamLoaded = false;
  @state() private _showNetwork = false;
  @state() private _streamEpoch = 0;
  private _streamWatchdog?: ReturnType<typeof setInterval>;
  private _lastVideoTime = 0;
  private _sawProgress = false;
  private _stallStrikes = 0;
  private _startupStrikes = 0;
  private _healthyTicks = 0;
  private _reloadCount = 0;
  private _reloadWindowStart = 0;
  private _cooldownUntil = 0;
  private _streamMountedAt = 0;
  private _watchedEpoch = -1;

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
    this._clearStreamWatchdog();
  }

  protected updated(changedProps: Map<string, unknown>): void {
    super.updated(changedProps);
    const streamEl = this.renderRoot.querySelector("ha-camera-stream");
    if (streamEl) {
      // A new <ha-camera-stream> mounted (first render or a reload) — restart the
      // fail-open grace window for it.
      if (this._watchedEpoch !== this._streamEpoch) {
        this._watchedEpoch = this._streamEpoch;
        this._streamMountedAt = Date.now();
      }
      if (!this._streamWatchdog) this._startStreamWatchdog();
    } else {
      this._clearStreamWatchdog();
    }
  }

  private _startStreamWatchdog(): void {
    this._streamWatchdog = setInterval(() => this._checkStreamLiveness(), STREAM_WATCHDOG_INTERVAL_MS);
    this._checkStreamLiveness();
  }

  private _clearStreamWatchdog(): void {
    if (this._streamWatchdog) {
      clearInterval(this._streamWatchdog);
      this._streamWatchdog = undefined;
    }
  }

  private _findStreamVideo(streamEl: Element): HTMLVideoElement | null {
    const root = streamEl.shadowRoot;
    if (!root) return null;

    const direct = root.querySelector("video");
    if (direct) return direct as HTMLVideoElement;

    const player = root.querySelector("ha-hls-player, ha-web-rtc-player") as HTMLElement | null;
    const known = player?.shadowRoot?.querySelector("video");
    if (known) return known as HTMLVideoElement;

    for (const child of Array.from(root.querySelectorAll("*"))) {
      const video = (child as HTMLElement).shadowRoot?.querySelector("video");
      if (video) return video as HTMLVideoElement;
    }

    return null;
  }

  private _checkStreamLiveness(): void {
    const streamEl = this.renderRoot.querySelector("ha-camera-stream");
    if (!streamEl) return;

    const video = this._findStreamVideo(streamEl);

    // Fail open: after a grace window, hide the loader regardless — never let it
    // permanently cover a stream that is actually playing (e.g. WebRTC, where
    // currentTime is unreliable, or when the <video> can't be located).
    if (
      !this._streamLoaded &&
      this._streamMountedAt > 0 &&
      Date.now() - this._streamMountedAt > STREAM_LOADED_FAILOPEN_MS
    ) {
      this._streamLoaded = true;
    }

    if (!video) {
      this._startupStrikes += 1;
      if (this._startupStrikes >= STREAM_STARTUP_RELOAD_TICKS) this._recoverStream();
      return;
    }

    // A decoded frame is available (readyState >= HAVE_CURRENT_DATA). This is the
    // robust "there is a picture to show" signal for both HLS and WebRTC, where
    // currentTime may never advance — so hide the loader immediately.
    if (!this._streamLoaded && video.readyState >= 2) {
      this._streamLoaded = true;
    }

    if (video.readyState < 2) {
      this._startupStrikes += 1;
      if (this._startupStrikes >= STREAM_STARTUP_RELOAD_TICKS) this._recoverStream();
      return;
    }

    if (video.currentTime > this._lastVideoTime + STREAM_PROGRESS_EPSILON) {
      this._lastVideoTime = video.currentTime;
      this._sawProgress = true;
      this._stallStrikes = 0;
      this._startupStrikes = 0;
      this._streamLoaded = true;
      // Refill the reload budget only after *sustained* playback, so a feed
      // that flaps (one frame, then freeze) can't keep topping it up.
      if (!video.paused && ++this._healthyTicks >= STREAM_HEALTHY_RESET_TICKS) {
        this._reloadCount = 0;
        this._cooldownUntil = 0;
        this._reloadWindowStart = 0;
      }
      return;
    }

    this._healthyTicks = 0;

    if (!this._sawProgress || video.paused) return;

    this._stallStrikes += 1;
    if (this._stallStrikes >= STREAM_STALL_TICKS) this._recoverStream();
  }

  private _requestBackendStreamReset(): void {
    const entities = this._entities();
    if (!entities.camera) return;
    void this.hass.callService("nanit", "reset_stream", {
      entity_id: entities.camera,
    });
  }

  private _recoverStream(): void {
    this._requestBackendStreamReset();
    this._reloadStream();
  }

  private _reloadStream(): void {
    const now = Date.now();
    // Backing off after hitting the cap — don't remount.
    if (now < this._cooldownUntil) {
      this._stallStrikes = 0;
      return;
    }
    // Start a fresh window once the previous one has fully elapsed.
    if (now - this._reloadWindowStart > STREAM_RELOAD_COOLDOWN_MS) {
      this._reloadWindowStart = now;
      this._reloadCount = 0;
    }

    this._reloadCount += 1;
    if (this._reloadCount >= STREAM_MAX_RELOADS) {
      this._cooldownUntil = now + STREAM_RELOAD_COOLDOWN_MS;
    }

    this._streamEpoch += 1;
    this._streamLoaded = false;
    this._lastVideoTime = 0;
    this._sawProgress = false;
    this._stallStrikes = 0;
    this._startupStrikes = 0;
    this._healthyTicks = 0;
  }

  private _onStreamLoad(): void {
    this._streamLoaded = true;
  }

  private _resetStreamState(): void {
    this._streamLoaded = false;
    this._clearStreamWatchdog();
    this._lastVideoTime = 0;
    this._sawProgress = false;
    this._stallStrikes = 0;
    this._startupStrikes = 0;
    this._healthyTicks = 0;
    this._reloadWindowStart = 0;
    this._streamMountedAt = 0;
    this._watchedEpoch = -1;
  }

  protected render(): TemplateResult {
    if (!this.hass || !this._config) {
      return html`<ha-card><div class="header"><span class="device-name">Nanit</span></div></ha-card>`;
    }

    const entities = this._entities();
    const cameraOn = this._isCameraOn(entities);
    if (!cameraOn) {
      this._resetStreamState();
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
    const hasWifi = !this._config.hide_connectivity_status
      && (isEntityAvailable(this.hass, entities.wifi_ssid)
        || isEntityAvailable(this.hass, entities.wifi_signal)
        || isEntityAvailable(this.hass, entities.wifi_frequency));
    const showDeviceBadge = !this._config.hide_baby_name;
    const showPowerButton = entities.power && !this._config.hide_power_button;
    const showHeader = showDeviceBadge || !cameraOn || hasWifi || showPowerButton;

    if (!showHeader) return html``;

    return html`
      <div class="header">
        ${showDeviceBadge
          ? html`
              <div class="device-badge">
                <ha-icon icon="mdi:baby-face-outline"></ha-icon>
                <span class="device-name">${deviceName}</span>
              </div>
            `
          : nothing}
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
          ${showPowerButton
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
                ${keyed(`${entities.camera}-${this._streamEpoch}`, html`
                  <ha-camera-stream
                    muted
                    data-stream-epoch=${this._streamEpoch}
                    .hass=${this.hass}
                    .stateObj=${cameraState}
                    @load=${this._onStreamLoad}
                  ></ha-camera-stream>
                `)}
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
    const temperatureEntity = this._config.temperature_entity_id || entities.temperature;
    const humidityEntity = this._config.humidity_entity_id || entities.humidity;

    if (isEntityAvailable(this.hass, temperatureEntity)) {
      const val = parseFloat(this.hass.states[temperatureEntity!].state);
      const display = isNaN(val) ? this.hass.states[temperatureEntity!].state : val.toFixed(1);
      const unit = (this.hass.states[temperatureEntity!].attributes.unit_of_measurement as string) ?? "";
      pills.push(html`
        <div class="pill pill-temp" @click=${() => this._fireMoreInfo(temperatureEntity!)}>
          <ha-icon icon="mdi:thermometer"></ha-icon>
          <span>${display}${unit}</span>
        </div>
      `);
    }

    if (isEntityAvailable(this.hass, humidityEntity)) {
      const val = parseFloat(this.hass.states[humidityEntity!].state);
      const display = isNaN(val) ? this.hass.states[humidityEntity!].state : val.toFixed(1);
      const unit = (this.hass.states[humidityEntity!].attributes.unit_of_measurement as string) ?? "%";
      pills.push(html`
        <div class="pill pill-humid" @click=${() => this._fireMoreInfo(humidityEntity!)}>
          <ha-icon icon="mdi:water-percent"></ha-icon>
          <span>${display}${unit}</span>
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
    const hasNightLight = !this._config.hide_night_light
      && isEntityAvailable(this.hass, entities.night_light);
    const hasSoundMachine = !this._config.hide_sound_machine
      && isEntityAvailable(this.hass, entities.sound_machine);
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
