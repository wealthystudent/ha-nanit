package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

	"github.com/trymwestin/nanit/internal/core/auth"
	"github.com/trymwestin/nanit/internal/core/camera"
	"github.com/trymwestin/nanit/internal/hlsproxy"
)

// Server is the HTTP API server.
type Server struct {
	cam        *camera.Client
	tokenMgr   *auth.TokenManager
	api        *auth.NanitAPI
	babyUID    string
	uiDir      string
	corsAll    bool
	log        *slog.Logger
	mux        *http.ServeMux
	hlsProxy   *hlsproxy.Proxy // nil when HLS is disabled
	ready      atomic.Bool
	tokenReady chan auth.Token
}

// NewServer creates a new HTTP API server.
func NewServer(
	cam *camera.Client,
	tokenMgr *auth.TokenManager,
	api *auth.NanitAPI,
	babyUID string,
	uiDir string,
	corsAll bool,
	hlsProxy *hlsproxy.Proxy,
	log *slog.Logger,
) *Server {
	s := &Server{
		cam:        cam,
		tokenMgr:   tokenMgr,
		api:        api,
		babyUID:    babyUID,
		uiDir:      uiDir,
		corsAll:    corsAll,
		hlsProxy:   hlsProxy,
		log:        log,
		mux:        http.NewServeMux(),
		tokenReady: make(chan auth.Token, 1),
	}
	// If dependencies are provided, mark as ready
	if cam != nil && tokenMgr != nil && tokenMgr.IsInitialized() {
		s.ready.Store(true)
	}
	s.routes()
	return s
}

// SetDependencies injects dependencies after initialization and marks server as ready.
func (s *Server) SetDependencies(
	cam *camera.Client,
	hlsProxy *hlsproxy.Proxy,
	babyUID string,
) {
	s.cam = cam
	s.hlsProxy = hlsProxy
	s.babyUID = babyUID
	s.ready.Store(true)
}

// TokenReady returns a channel that receives a token when it's provisioned via API.
func (s *Server) TokenReady() <-chan auth.Token {
	return s.tokenReady
}

// Handler returns the HTTP handler.
func (s *Server) Handler() http.Handler {
	if !s.corsAll {
		return s.mux
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		s.mux.ServeHTTP(w, r)
	})
}

func (s *Server) routes() {
	s.mux.HandleFunc("GET /api/auth/status", s.handleAuthStatus)
	s.mux.HandleFunc("POST /api/auth/token", s.handleAuthToken)

	s.mux.HandleFunc("GET /api/status", s.requireReady(s.handleGetStatus))
	s.mux.HandleFunc("GET /api/sensors", s.requireReady(s.handleGetSensors))
	s.mux.HandleFunc("GET /api/settings", s.requireReady(s.handleGetSettings))
	s.mux.HandleFunc("GET /api/events", s.requireReady(s.handleGetEvents))
	s.mux.HandleFunc("GET /api/stream-url", s.requireReady(s.handleGetStreamURL))

	s.mux.HandleFunc("POST /api/control/nightlight", s.requireReady(s.handleControlNightlight))
	s.mux.HandleFunc("POST /api/control/sleep", s.requireReady(s.handleControlSleep))
	s.mux.HandleFunc("POST /api/control/volume", s.requireReady(s.handleControlVolume))
	s.mux.HandleFunc("POST /api/control/mic", s.requireReady(s.handleControlMic))
	s.mux.HandleFunc("POST /api/control/statusled", s.requireReady(s.handleControlStatusLED))
	s.mux.HandleFunc("POST /api/streaming/start", s.requireReady(s.handleStreamStart))
	s.mux.HandleFunc("POST /api/streaming/stop", s.requireReady(s.handleStreamStop))

	s.mux.HandleFunc("POST /api/hls/start", s.requireReady(s.handleHLSStart))
	s.mux.HandleFunc("POST /api/hls/stop", s.requireReady(s.handleHLSStop))
	s.mux.HandleFunc("GET /api/hls/status", s.requireReady(s.handleHLSStatus))
	if s.hlsProxy != nil {
		s.mux.Handle("/hls/", http.StripPrefix("/hls/", s.hlsProxy.Handler()))
	} else {
		// If hlsProxy is nil, we might still receive requests if HLS is enabled later.
		// However, the standard library ServeMux matches strictly.
		// If we add the handler later, we need to rebuild the mux or use a dynamic mux.
		// Since we're using stdlib Mux, we can't easily add routes later.
		// But wait, hlsProxy depends on config which is loaded at start.
		// If HLS is enabled in config, hlsProxy will be non-nil OR we will initialize it later?
		// The requirements say "create TokenManager (but DON'T call Init), start HTTP server... then start the full camera/MQTT/HLS stack".
		// This implies HLS proxy might be created later.
		// I should probably register a handler that checks if hlsProxy is set.
		s.mux.HandleFunc("/hls/", s.handleHLSProxy)
	}

	// Serve static UI
	if s.uiDir != "" {
		s.mux.Handle("/", http.FileServer(http.Dir(s.uiDir)))
	} else {
		// Try to find the built-in UI directory relative to the binary
		s.mux.HandleFunc("/", s.handleStaticFallback)
	}
}

func (s *Server) requireReady(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !s.ready.Load() {
			s.writeError(w, http.StatusServiceUnavailable, "nanitd is not yet authenticated. Please set up the Nanit integration in Home Assistant.")
			return
		}
		next(w, r)
	}
}

type authStatusResponse struct {
	Authenticated bool `json:"authenticated"`
	Ready         bool `json:"ready"`
}

func (s *Server) handleAuthStatus(w http.ResponseWriter, r *http.Request) {
	// Authenticated if token manager has a token
	authenticated := s.tokenMgr != nil && s.tokenMgr.IsInitialized()
	ready := s.ready.Load()

	s.writeJSON(w, authStatusResponse{
		Authenticated: authenticated,
		Ready:         ready,
	})
}

type authTokenRequest struct {
	AccessToken  string      `json:"access_token"`
	RefreshToken string      `json:"refresh_token"`
	Babies       []auth.Baby `json:"babies"`
}

func (s *Server) handleAuthToken(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	var req authTokenRequest
	if err := s.readJSON(r, &req); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}

	if req.AccessToken == "" || req.RefreshToken == "" {
		s.writeError(w, http.StatusBadRequest, "access_token and refresh_token are required")
		return
	}

	// Create token object
	tok := auth.Token{
		AuthToken:    req.AccessToken,
		RefreshToken: req.RefreshToken,
		AuthTime:     time.Now(),
		Babies:       req.Babies,
	}

	// Notify main loop via channel
	select {
	case s.tokenReady <- tok:
		s.log.Info("received auth token via API")
		s.writeJSON(w, map[string]string{"status": "ok"})
	default:
		s.log.Warn("received auth token but channel is full or nobody listening")
		s.writeError(w, http.StatusServiceUnavailable, "server not ready to accept tokens")
	}
}

func (s *Server) handleHLSProxy(w http.ResponseWriter, r *http.Request) {
	if !s.ready.Load() {
		s.writeError(w, http.StatusServiceUnavailable, "nanitd is not yet authenticated")
		return
	}

	if s.hlsProxy == nil {
		s.writeError(w, http.StatusServiceUnavailable, "HLS proxy not enabled")
		return
	}
	http.StripPrefix("/hls/", s.hlsProxy.Handler()).ServeHTTP(w, r)
}

func (s *Server) handleStaticFallback(w http.ResponseWriter, r *http.Request) {
	// Try common UI locations
	candidates := []string{
		"internal/ui/dist",
		"/app/ui",
	}
	for _, dir := range candidates {
		indexPath := filepath.Join(dir, "index.html")
		if _, err := os.Stat(indexPath); err == nil {
			http.FileServer(http.Dir(dir)).ServeHTTP(w, r)
			return
		}
	}
	// If serving / and no UI found, return a helpful message
	if r.URL.Path == "/" {
		w.Header().Set("Content-Type", "text/html")
		fmt.Fprint(w, `<html><body><h1>Nanit Dashboard</h1><p>UI not found. Set <code>ui_dir</code> in config or <code>NANIT_UI_DIR</code> env var.</p><p><a href="/api/status">API Status</a></p></body></html>`)
		return
	}
	http.NotFound(w, r)
}

func (s *Server) corsHeaders(w http.ResponseWriter) {
	if s.corsAll {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
	}
}

func (s *Server) writeJSON(w http.ResponseWriter, v interface{}) {
	s.corsHeaders(w)
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(v); err != nil {
		s.log.Error("failed to encode JSON response", "error", err)
	}
}

func (s *Server) writeError(w http.ResponseWriter, code int, msg string) {
	s.corsHeaders(w)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

func (s *Server) readJSON(r *http.Request, v interface{}) error {
	defer r.Body.Close()
	return json.NewDecoder(r.Body).Decode(v)
}

// --- Handlers ---

type statusResponse struct {
	Connected bool        `json:"connected"`
	BabyUID   string      `json:"baby_uid"`
	Babies    []auth.Baby `json:"babies,omitempty"`
}

func (s *Server) handleGetStatus(w http.ResponseWriter, r *http.Request) {
	tok, err := s.tokenMgr.Token(r.Context())
	var babies []auth.Baby
	if err == nil {
		babies = tok.Babies
	}

	snap := s.cam.State().Snapshot()
	s.writeJSON(w, statusResponse{
		Connected: s.cam.Connected(),
		BabyUID:   s.babyUID,
		Babies:    babies,
	})
	_ = snap // we already read it to check connection
}

func (s *Server) handleGetSensors(w http.ResponseWriter, _ *http.Request) {
	snap := s.cam.State().Snapshot()
	s.writeJSON(w, snap.Sensors)
}

func (s *Server) handleGetSettings(w http.ResponseWriter, _ *http.Request) {
	snap := s.cam.State().Snapshot()
	s.writeJSON(w, map[string]interface{}{
		"settings": snap.Settings,
		"control":  snap.Control,
		"stream":   snap.Stream,
	})
}

func (s *Server) handleGetEvents(w http.ResponseWriter, r *http.Request) {
	tok, err := s.tokenMgr.Token(r.Context())
	if err != nil {
		s.writeError(w, http.StatusInternalServerError, "auth error: "+err.Error())
		return
	}

	messages, err := s.api.GetMessages(r.Context(), tok.AuthToken, s.babyUID, 20)
	if err != nil {
		s.writeError(w, http.StatusBadGateway, "failed to fetch events: "+err.Error())
		return
	}

	s.writeJSON(w, map[string]interface{}{"events": messages})
}

func (s *Server) handleGetStreamURL(w http.ResponseWriter, _ *http.Request) {
	snap := s.cam.State().Snapshot()
	s.writeJSON(w, snap.Stream)
}

type enabledBody struct {
	Enabled bool `json:"enabled"`
}

func (s *Server) handleControlNightlight(w http.ResponseWriter, r *http.Request) {
	var body enabledBody
	if err := s.readJSON(r, &body); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	if err := s.cam.SetNightLight(r.Context(), body.Enabled); err != nil {
		s.writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeJSON(w, map[string]string{"status": "ok"})
}

func (s *Server) handleControlSleep(w http.ResponseWriter, r *http.Request) {
	var body enabledBody
	if err := s.readJSON(r, &body); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	if err := s.cam.SetSleepMode(r.Context(), body.Enabled); err != nil {
		s.writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeJSON(w, map[string]string{"status": "ok"})
}

type volumeBody struct {
	Level int32 `json:"level"`
}

func (s *Server) handleControlVolume(w http.ResponseWriter, r *http.Request) {
	var body volumeBody
	if err := s.readJSON(r, &body); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	if body.Level < 0 || body.Level > 100 {
		s.writeError(w, http.StatusBadRequest, "volume must be 0-100")
		return
	}
	if err := s.cam.SetVolume(r.Context(), body.Level); err != nil {
		s.writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeJSON(w, map[string]string{"status": "ok"})
}

type mutedBody struct {
	Muted bool `json:"muted"`
}

func (s *Server) handleControlMic(w http.ResponseWriter, r *http.Request) {
	var body mutedBody
	if err := s.readJSON(r, &body); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	if err := s.cam.SetMicMute(r.Context(), body.Muted); err != nil {
		s.writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeJSON(w, map[string]string{"status": "ok"})
}

func (s *Server) handleControlStatusLED(w http.ResponseWriter, r *http.Request) {
	var body enabledBody
	if err := s.readJSON(r, &body); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	if err := s.cam.SetStatusLED(r.Context(), body.Enabled); err != nil {
		s.writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeJSON(w, map[string]string{"status": "ok"})
}

type streamStartBody struct {
	RTMPUrl string `json:"rtmp_url"`
}

func (s *Server) handleStreamStart(w http.ResponseWriter, r *http.Request) {
	var body streamStartBody
	if err := s.readJSON(r, &body); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	if body.RTMPUrl == "" {
		s.writeError(w, http.StatusBadRequest, "rtmp_url is required")
		return
	}
	if err := s.cam.StartStreaming(r.Context(), body.RTMPUrl); err != nil {
		s.writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeJSON(w, map[string]string{"status": "ok"})
}

func (s *Server) handleStreamStop(w http.ResponseWriter, r *http.Request) {
	if err := s.cam.StopStreaming(context.Background()); err != nil {
		s.writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.writeJSON(w, map[string]string{"status": "ok"})
}

func (s *Server) handleHLSStart(w http.ResponseWriter, r *http.Request) {
	if s.hlsProxy == nil {
		s.writeError(w, http.StatusServiceUnavailable, "HLS proxy is not enabled")
		return
	}

	if s.hlsProxy.Running() {
		s.writeJSON(w, map[string]string{"status": "ok"})
		return
	}

	tok, err := s.tokenMgr.Token(r.Context())
	if err != nil {
		s.writeError(w, http.StatusInternalServerError, "auth error: "+err.Error())
		return
	}

	rtmpsURL := fmt.Sprintf("rtmps://media-secured.nanit.com/nanit/%s.%s", s.babyUID, tok.AuthToken)

	if err := s.hlsProxy.Start(context.Background(), rtmpsURL); err != nil {
		s.writeError(w, http.StatusInternalServerError, "failed to start HLS: "+err.Error())
		return
	}

	s.writeJSON(w, map[string]string{"status": "ok"})
}

func (s *Server) handleHLSStop(w http.ResponseWriter, r *http.Request) {
	if s.hlsProxy == nil {
		s.writeError(w, http.StatusServiceUnavailable, "HLS proxy is not enabled")
		return
	}

	if err := s.hlsProxy.Stop(r.Context()); err != nil {
		s.writeError(w, http.StatusInternalServerError, "failed to stop HLS: "+err.Error())
		return
	}

	s.writeJSON(w, map[string]string{"status": "ok"})
}

func (s *Server) handleHLSStatus(w http.ResponseWriter, _ *http.Request) {
	if s.hlsProxy == nil {
		s.writeJSON(w, map[string]interface{}{
			"enabled": false,
			"running": false,
		})
		return
	}

	s.writeJSON(w, map[string]interface{}{
		"enabled": true,
		"running": s.hlsProxy.Running(),
	})
}
