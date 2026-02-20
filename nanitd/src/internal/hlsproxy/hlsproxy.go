package hlsproxy

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Config holds the HLS proxy configuration.
type Config struct {
	OutputDir    string // temp dir for HLS segments, default os.TempDir()+"/nanit-hls"
	SegmentTime  int    // HLS segment duration seconds, default 2
	PlaylistSize int    // segments in playlist, default 5
	FFmpegPath   string // path to ffmpeg, default "ffmpeg"
}

// Proxy transcodes an RTMPS stream to HLS via ffmpeg and serves the segments over HTTP.
type Proxy struct {
	cfg       Config
	log       *slog.Logger
	mu        sync.Mutex
	cmd       *exec.Cmd
	cancel    context.CancelFunc
	running   bool
	streamURL string

	lastSnapshot   []byte
	lastSnapshotAt time.Time
	snapshotMu     sync.Mutex
}

// New creates a new HLS proxy with the given config and logger.
// Zero-value config fields are replaced with sensible defaults.
func New(cfg Config, log *slog.Logger) *Proxy {
	if cfg.OutputDir == "" {
		cfg.OutputDir = filepath.Join(os.TempDir(), "nanit-hls")
	}
	if cfg.SegmentTime == 0 {
		cfg.SegmentTime = 2
	}
	if cfg.PlaylistSize == 0 {
		cfg.PlaylistSize = 5
	}
	if cfg.FFmpegPath == "" {
		cfg.FFmpegPath = "ffmpeg"
	}

	return &Proxy{
		cfg: cfg,
		log: log,
	}
}

// Start begins transcoding the given RTMPS URL to HLS segments.
// The ffmpeg process is automatically killed when ctx is cancelled.
func (p *Proxy) Start(ctx context.Context, rtmpsURL string) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.running {
		return fmt.Errorf("hlsproxy: already running")
	}

	// Create output directory.
	if err := os.MkdirAll(p.cfg.OutputDir, 0o755); err != nil {
		return fmt.Errorf("hlsproxy: create output dir: %w", err)
	}

	// Clean stale files from previous runs.
	if err := p.cleanFiles(); err != nil {
		return fmt.Errorf("hlsproxy: clean stale files: %w", err)
	}

	playlistPath := filepath.Join(p.cfg.OutputDir, "stream.m3u8")
	segmentPattern := filepath.Join(p.cfg.OutputDir, "segment_%03d.ts")

	// Build ffmpeg command with a cancellable context.
	cmdCtx, cancel := context.WithCancel(ctx)
	cmd := exec.CommandContext(cmdCtx, p.cfg.FFmpegPath,
		"-i", rtmpsURL,
		"-c:v", "copy",
		"-c:a", "aac",
		"-f", "hls",
		"-hls_time", strconv.Itoa(p.cfg.SegmentTime),
		"-hls_list_size", strconv.Itoa(p.cfg.PlaylistSize),
		"-hls_flags", "delete_segments+append_list",
		"-hls_segment_filename", segmentPattern,
		playlistPath,
	)

	// Capture ffmpeg stderr for logging.
	cmd.Stderr = &logWriter{log: p.log, level: slog.LevelDebug, prefix: "ffmpeg"}

	if err := cmd.Start(); err != nil {
		cancel()
		return fmt.Errorf("hlsproxy: start ffmpeg: %w", err)
	}

	p.cmd = cmd
	p.cancel = cancel
	p.running = true
	p.streamURL = rtmpsURL

	p.log.Info("hlsproxy started",
		"rtmps_url", rtmpsURL,
		"output_dir", p.cfg.OutputDir,
		"segment_time", p.cfg.SegmentTime,
		"playlist_size", p.cfg.PlaylistSize,
	)

	// Monitor ffmpeg in background.
	go p.monitor(cmd, cancel)

	return nil
}

// Stop terminates the ffmpeg process and cleans up HLS files.
func (p *Proxy) Stop(_ context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	if !p.running {
		return nil
	}

	p.log.Info("hlsproxy stopping")

	// Cancel the context to kill ffmpeg.
	p.cancel()

	// Wait for the process to exit with a timeout.
	done := make(chan error, 1)
	go func() {
		done <- p.cmd.Wait()
	}()

	select {
	case <-done:
		// Process exited.
	case <-time.After(5 * time.Second):
		p.log.Warn("hlsproxy: ffmpeg did not exit in time, killing")
		if p.cmd.Process != nil {
			_ = p.cmd.Process.Kill()
		}
	}

	// Clean up HLS files.
	if err := p.cleanFiles(); err != nil {
		p.log.Error("hlsproxy: failed to clean files on stop", "error", err)
	}

	p.running = false
	p.streamURL = ""
	p.cmd = nil
	p.cancel = nil

	p.log.Info("hlsproxy stopped")
	return nil
}

// Handler returns an http.Handler that serves HLS segments and playlists.
// Returns 503 Service Unavailable when the proxy is not running.
func (p *Proxy) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.mu.Lock()
		running := p.running
		p.mu.Unlock()

		if !running {
			http.Error(w, "HLS stream not available", http.StatusServiceUnavailable)
			return
		}

		// Set CORS headers.
		w.Header().Set("Access-Control-Allow-Origin", "*")

		// Resolve the requested file relative to the output dir.
		// Strip the handler prefix — the caller mounts this at e.g. "/hls/".
		name := filepath.Base(r.URL.Path)
		filePath := filepath.Join(p.cfg.OutputDir, name)

		// Security: ensure we only serve from the output dir.
		absFile, err := filepath.Abs(filePath)
		if err != nil || !strings.HasPrefix(absFile, p.cfg.OutputDir) {
			http.NotFound(w, r)
			return
		}

		// Set appropriate Content-Type and caching headers.
		switch {
		case strings.HasSuffix(name, ".m3u8"):
			w.Header().Set("Content-Type", "application/vnd.apple.mpegurl")
			w.Header().Set("Cache-Control", "no-cache")
		case strings.HasSuffix(name, ".ts"):
			w.Header().Set("Content-Type", "video/MP2T")
		default:
			http.NotFound(w, r)
			return
		}

		http.ServeFile(w, r, filePath)
	})
}

// Running returns whether the proxy is currently active.
func (p *Proxy) Running() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.running
}

// StreamURL returns the RTMPS URL currently being proxied.
func (p *Proxy) StreamURL() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.streamURL
}

// Snapshot captures a single JPEG frame from the RTMPS stream.
// It caches the result for 10 seconds to avoid overloading ffmpeg.
func (p *Proxy) Snapshot(ctx context.Context, rtmpsURL string) ([]byte, error) {
	p.snapshotMu.Lock()
	if time.Since(p.lastSnapshotAt) < 10*time.Second && len(p.lastSnapshot) > 0 {
		defer p.snapshotMu.Unlock()
		return p.lastSnapshot, nil
	}
	p.snapshotMu.Unlock()

	// Use a timeout context for the ffmpeg command.
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, p.cfg.FFmpegPath,
		"-i", rtmpsURL,
		"-vframes", "1",
		"-f", "image2",
		"-c:v", "mjpeg",
		"pipe:1",
	)

	// Capture stderr for debugging if needed, but don't fail on it unless cmd fails.
	cmd.Stderr = &logWriter{log: p.log, level: slog.LevelDebug, prefix: "ffmpeg-snap"}

	output, err := cmd.Output()
	if err != nil {
		p.log.Error("snapshot failed", "error", err)
		return nil, fmt.Errorf("ffmpeg snapshot failed: %w", err)
	}

	p.snapshotMu.Lock()
	p.lastSnapshot = output
	p.lastSnapshotAt = time.Now()
	p.snapshotMu.Unlock()

	return output, nil
}

// monitor waits for the ffmpeg process to exit and logs unexpected exits.
func (p *Proxy) monitor(cmd *exec.Cmd, cancel context.CancelFunc) {
	err := cmd.Wait()

	p.mu.Lock()
	defer p.mu.Unlock()

	// Only log if we're still supposed to be running (unexpected exit).
	if p.running && p.cmd == cmd {
		if err != nil {
			p.log.Error("hlsproxy: ffmpeg exited unexpectedly", "error", err)
		} else {
			p.log.Warn("hlsproxy: ffmpeg exited with status 0 unexpectedly")
		}
		p.running = false
		p.streamURL = ""
		p.cmd = nil
		cancel()
	}
}

// cleanFiles removes all .ts and .m3u8 files from the output directory.
func (p *Proxy) cleanFiles() error {
	entries, err := os.ReadDir(p.cfg.OutputDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}

	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if strings.HasSuffix(name, ".ts") || strings.HasSuffix(name, ".m3u8") {
			path := filepath.Join(p.cfg.OutputDir, name)
			if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
				p.log.Warn("hlsproxy: failed to remove stale file", "path", path, "error", err)
			}
		}
	}

	return nil
}

// logWriter adapts slog to an io.Writer for capturing ffmpeg stderr output.
type logWriter struct {
	log    *slog.Logger
	level  slog.Level
	prefix string
}

func (w *logWriter) Write(p []byte) (int, error) {
	msg := strings.TrimRight(string(p), "\n")
	if msg != "" {
		w.log.Log(context.Background(), w.level, msg, "source", w.prefix)
	}
	return len(p), nil
}
