package auth

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sync"
	"time"
)

// Token holds authentication credentials and baby info.
type Token struct {
	AuthToken    string    `json:"authToken"`
	RefreshToken string    `json:"refreshToken"`
	AuthTime     time.Time `json:"authTime"`
	Revision     int       `json:"revision,omitempty"`
	Babies       []Baby    `json:"babies,omitempty"`
}

// Baby represents a baby profile from the Nanit account.
type Baby struct {
	UID       string `json:"uid"`
	Name      string `json:"name"`
	CameraUID string `json:"camera_uid"`
}

// Authenticator handles token refresh against the Nanit API.
type Authenticator interface {
	Refresh(ctx context.Context, current *Token) (*Token, error)
}

// TokenStore persists tokens to disk.
type TokenStore interface {
	Load(ctx context.Context) (*Token, error)
	Save(ctx context.Context, t *Token) error
}

// ErrNoToken is returned when no token is found in the store.
var ErrNoToken = errors.New("no token found")

// TokenManager combines auth + persistence with automatic refresh.
type TokenManager struct {
	auth  Authenticator
	store TokenStore
	mu    sync.RWMutex
	token *Token
	log   *slog.Logger
}

// NewTokenManager creates a manager that auto-refreshes tokens.
func NewTokenManager(auth Authenticator, store TokenStore, log *slog.Logger) *TokenManager {
	return &TokenManager{
		auth:  auth,
		store: store,
		log:   log,
	}
}

// Init loads the stored token. Must be called before Token().
func (m *TokenManager) Init(ctx context.Context) error {
	tok, err := m.store.Load(ctx)
	if err != nil {
		if errors.Is(err, ErrNoToken) {
			return ErrNoToken
		}
		return fmt.Errorf("auth: load token: %w", err)
	}
	m.mu.Lock()
	m.token = tok
	m.mu.Unlock()
	m.log.Info("loaded auth token", "baby_count", len(tok.Babies))
	return nil
}

// IsInitialized returns true if a token has been loaded.
func (m *TokenManager) IsInitialized() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.token != nil
}

// Token returns the current valid auth token, refreshing if necessary.
func (m *TokenManager) Token(ctx context.Context) (*Token, error) {
	m.mu.RLock()
	tok := m.token
	m.mu.RUnlock()

	if tok == nil {
		return nil, fmt.Errorf("auth: no token loaded, call Init first")
	}

	// Refresh if token is older than 2 hours (JWT typically expires in ~3h)
	if time.Since(tok.AuthTime) > 2*time.Hour {
		m.log.Info("token expired, refreshing", "age", time.Since(tok.AuthTime).Round(time.Second))
		return m.doRefresh(ctx, tok)
	}
	return tok, nil
}

// ForceRefresh triggers a token refresh regardless of expiry.
func (m *TokenManager) ForceRefresh(ctx context.Context) (*Token, error) {
	m.log.Info("force-refreshing token")
	m.mu.RLock()
	tok := m.token
	m.mu.RUnlock()
	if tok == nil {
		return nil, fmt.Errorf("auth: no token loaded")
	}
	return m.doRefresh(ctx, tok)
}

func (m *TokenManager) doRefresh(ctx context.Context, current *Token) (*Token, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Double-check: another goroutine may have refreshed while we waited
	if m.token != current && time.Since(m.token.AuthTime) < 2*time.Hour {
		return m.token, nil
	}

	newTok, err := m.auth.Refresh(ctx, current)
	if err != nil {
		return nil, fmt.Errorf("auth: refresh: %w", err)
	}

	// Preserve babies from previous token if not returned
	if len(newTok.Babies) == 0 && len(current.Babies) > 0 {
		newTok.Babies = current.Babies
	}

	m.token = newTok
	if err := m.store.Save(ctx, newTok); err != nil {
		m.log.Error("failed to save refreshed token", "error", err)
	}
	m.log.Info("token refreshed successfully", "revision", newTok.Revision, "baby_count", len(newTok.Babies))
	return newTok, nil
}

// --- NanitAuth: REST API authenticator ---

// NanitAuth implements Authenticator using the Nanit REST API.
type NanitAuth struct {
	apiBase string
	client  *http.Client
	log     *slog.Logger
}

// NewNanitAuth creates a new REST authenticator.
func NewNanitAuth(apiBase string, log *slog.Logger) *NanitAuth {
	return &NanitAuth{
		apiBase: apiBase,
		client:  &http.Client{Timeout: 30 * time.Second},
		log:     log,
	}
}

type refreshRequest struct {
	RefreshToken string `json:"refresh_token"`
}

type refreshResponse struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
}

// Refresh exchanges a refresh token for a new access + refresh token pair.
func (a *NanitAuth) Refresh(ctx context.Context, current *Token) (*Token, error) {
	body, err := json.Marshal(refreshRequest{RefreshToken: current.RefreshToken})
	if err != nil {
		return nil, err
	}

	url := a.apiBase + "/tokens/refresh"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", current.AuthToken)

	a.log.Debug("refreshing token", "url", url)

	resp, err := a.client.Do(req)
	if err != nil {
		a.log.Error("token refresh HTTP request failed", "error", err)
		return nil, fmt.Errorf("refresh request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		a.log.Error("token refresh returned non-200", "status", resp.StatusCode)
		return nil, fmt.Errorf("refresh: HTTP %d", resp.StatusCode)
	}

	a.log.Info("token refresh succeeded")

	var result refreshResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("refresh: decode: %w", err)
	}

	return &Token{
		AuthToken:    result.AccessToken,
		RefreshToken: result.RefreshToken,
		AuthTime:     time.Now(),
		Revision:     current.Revision + 1,
		Babies:       current.Babies,
	}, nil
}

// --- FileTokenStore: JSON file persistence ---

// FileTokenStore reads/writes tokens to a JSON file.
type FileTokenStore struct {
	path string
	mu   sync.Mutex
}

// NewFileTokenStore creates a store backed by the given file path.
func NewFileTokenStore(path string) *FileTokenStore {
	return &FileTokenStore{path: path}
}

// Load reads the token from disk.
func (s *FileTokenStore) Load(_ context.Context) (*Token, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	data, err := os.ReadFile(s.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, ErrNoToken
		}
		return nil, fmt.Errorf("token store: read %s: %w", s.path, err)
	}

	var tok Token
	if err := json.Unmarshal(data, &tok); err != nil {
		return nil, fmt.Errorf("token store: parse: %w", err)
	}
	return &tok, nil
}

// Save writes the token to disk atomically.
func (s *FileTokenStore) Save(_ context.Context, t *Token) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	data, err := json.MarshalIndent(t, "", "  ")
	if err != nil {
		return fmt.Errorf("token store: marshal: %w", err)
	}

	tmpPath := s.path + ".tmp"
	if err := os.WriteFile(tmpPath, data, 0600); err != nil {
		return fmt.Errorf("token store: write tmp: %w", err)
	}
	if err := os.Rename(tmpPath, s.path); err != nil {
		return fmt.Errorf("token store: rename: %w", err)
	}
	return nil
}

// --- REST API helpers ---

// NanitAPI provides REST API calls to the Nanit cloud.
type NanitAPI struct {
	apiBase string
	client  *http.Client
	log     *slog.Logger
}

// NewNanitAPI creates a REST API client.
func NewNanitAPI(apiBase string, log *slog.Logger) *NanitAPI {
	return &NanitAPI{
		apiBase: apiBase,
		client:  &http.Client{Timeout: 30 * time.Second},
		log:     log,
	}
}

// BabiesResponse is the response from the babies endpoint.
type BabiesResponse struct {
	Babies []Baby `json:"babies"`
}

// GetBabies fetches the list of babies from the Nanit API.
func (a *NanitAPI) GetBabies(ctx context.Context, authToken string) ([]Baby, error) {
	url := a.apiBase + "/babies"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", authToken)

	resp, err := a.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("get babies: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("get babies: HTTP %d", resp.StatusCode)
	}

	var result BabiesResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("get babies: decode: %w", err)
	}
	return result.Babies, nil
}

// UnixTime is a time.Time that unmarshals from a JSON integer (Unix seconds).
type UnixTime struct {
	time.Time
}

func (t *UnixTime) UnmarshalJSON(data []byte) error {
	// Try as integer (Unix timestamp)
	var unix int64
	if err := json.Unmarshal(data, &unix); err == nil {
		t.Time = time.Unix(unix, 0)
		return nil
	}
	// Fall back to standard time.Time parsing (RFC3339 string)
	return t.Time.UnmarshalJSON(data)
}

func (t UnixTime) MarshalJSON() ([]byte, error) {
	return json.Marshal(t.Time.Unix())
}

// MessageEntry is a single message from the Nanit events API.
type MessageEntry struct {
	Type      string   `json:"type"`
	Time      UnixTime `json:"time"`
	BabyUID   string   `json:"baby_uid,omitempty"`
	ID        int64    `json:"id,omitempty"`
	CreatedAt string   `json:"created_at,omitempty"`
	Data      *struct {
		Event *struct {
			UID string `json:"uid"`
		} `json:"event,omitempty"`
		Camera *struct {
			UID string `json:"uid"`
		} `json:"camera,omitempty"`
	} `json:"data,omitempty"`
}

// MessagesResponse is the response from the messages endpoint.
type MessagesResponse struct {
	Messages []MessageEntry `json:"messages"`
}

// GetMessages fetches recent messages/events for a baby.
func (a *NanitAPI) GetMessages(ctx context.Context, authToken, babyUID string, limit int) ([]MessageEntry, error) {
	url := fmt.Sprintf("%s/babies/%s/messages?limit=%d", a.apiBase, babyUID, limit)
	a.log.Info("fetching cloud messages", "baby_uid", babyUID, "limit", limit)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", authToken)

	resp, err := a.client.Do(req)
	if err != nil {
		a.log.Error("cloud messages request failed", "baby_uid", babyUID, "error", err)
		return nil, fmt.Errorf("get messages: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		a.log.Error("cloud messages returned non-200", "baby_uid", babyUID, "status", resp.StatusCode)
		return nil, fmt.Errorf("get messages: HTTP %d", resp.StatusCode)
	}

	var result MessagesResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("get messages: decode: %w", err)
	}
	a.log.Info("cloud messages fetched", "baby_uid", babyUID, "count", len(result.Messages))
	return result.Messages, nil
}
