package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/trymwestin/nanit/internal/config"
	"github.com/trymwestin/nanit/internal/core/auth"
	"github.com/trymwestin/nanit/internal/core/camera"
	"github.com/trymwestin/nanit/internal/core/state"
	"github.com/trymwestin/nanit/internal/core/transport"
	"github.com/trymwestin/nanit/internal/hlsproxy"
	"github.com/trymwestin/nanit/internal/httpapi"
	"github.com/trymwestin/nanit/internal/mqtt"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "fatal: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	configPath := flag.String("config", "", "path to config YAML file")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		return fmt.Errorf("load config: %w", err)
	}

	log := setupLogger(cfg.Log)
	log.Info("starting nanitd")

	tokenStore := auth.NewFileTokenStore(cfg.Session.Path)
	nanitAuth := auth.NewNanitAuth(cfg.Nanit.APIBase, log.With("component", "auth"))
	tokenMgr := auth.NewTokenManager(nanitAuth, tokenStore, log.With("component", "token_mgr"))

	nanitAPI := auth.NewNanitAPI(cfg.Nanit.APIBase, log.With("component", "nanit_api"))

	// Create HTTP server early for auth endpoints
	// Dependencies (cam, hlsProxy) are nil initially
	apiServer := httpapi.NewServer(
		nil,
		tokenMgr,
		nanitAPI,
		"", // BabyUID not known yet
		cfg.HTTP.UIDir,
		cfg.HTTP.CORSAll,
		nil,
		log.With("component", "httpapi"),
	)

	httpSrv := &http.Server{
		Addr:         cfg.HTTP.Addr,
		Handler:      apiServer.Handler(),
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 15 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		log.Info("HTTP server starting", "addr", cfg.HTTP.Addr)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			errCh <- fmt.Errorf("http server: %w", err)
		}
	}()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	// Try to load token
	if err := tokenMgr.Init(ctx); err != nil {
		if err == auth.ErrNoToken {
			log.Info("no token found, waiting for authentication via API...")
			// Wait for token from API
			select {
			case tok := <-apiServer.TokenReady():
				log.Info("received token from API")
				if err := tokenStore.Save(ctx, &tok); err != nil {
					return fmt.Errorf("save token: %w", err)
				}
				// Now init manager
				if err := tokenMgr.Init(ctx); err != nil {
					return fmt.Errorf("init auth after provision: %w", err)
				}
			case err := <-errCh:
				return err
			case sig := <-sigCh:
				log.Info("received shutdown signal while waiting for auth", "signal", sig)
				return nil
			}
		} else {
			return fmt.Errorf("init auth: %w", err)
		}
	}

	tok, err := tokenMgr.Token(ctx)
	if err != nil {
		return fmt.Errorf("get token: %w", err)
	}

	cameraUID := cfg.Nanit.CameraUID
	babyUID := cfg.Nanit.BabyUID
	if cameraUID == "" || babyUID == "" {
		if len(tok.Babies) == 0 {
			return fmt.Errorf("no babies in token and no camera_uid/baby_uid configured")
		}
		if cameraUID == "" {
			cameraUID = tok.Babies[0].CameraUID
		}
		if babyUID == "" {
			babyUID = tok.Babies[0].UID
		}
		log.Info("auto-detected camera", "camera_uid", cameraUID, "baby_uid", babyUID, "baby_name", tok.Babies[0].Name)
	}

	bus := state.NewEventBus(log.With("component", "event_bus"))
	store := state.NewStateStore(bus, log.With("component", "state"))

	// Build transport dialer: local (with fallback to cloud) or cloud-only
	var dialer transport.Dialer
	cloudDialer := transport.NewCloudDialer(cfg.Nanit.APIBase, log.With("component", "transport"))
	if cfg.Nanit.CameraIP != "" {
		localDialer := transport.NewLocalDialer(cfg.Nanit.CameraIP, log.With("component", "transport_local"))
		dialer = transport.NewFallbackDialer(localDialer, cloudDialer, log.With("component", "transport"))
		log.Info("local dialer enabled", "camera_ip", cfg.Nanit.CameraIP)
	} else {
		dialer = cloudDialer
	}

	cam := camera.NewClient(
		cameraUID,
		dialer,
		tokenMgr,
		store,
		bus,
		log.With("component", "camera"),
	)

	if err := cam.Start(ctx); err != nil {
		return fmt.Errorf("start camera: %w", err)
	}

	// MQTT publisher
	var mqttPub mqtt.Publisher
	if cfg.MQTT.Enabled && cfg.MQTT.Broker != "" {
		mqttCfg := mqtt.MQTTConfig{
			Broker:      cfg.MQTT.Broker,
			Username:    cfg.MQTT.Username,
			Password:    cfg.MQTT.Password,
			TopicPrefix: cfg.MQTT.TopicPrefix,
			DeviceID:    cfg.MQTT.DeviceID,
			BabyName:    cfg.MQTT.BabyName,
			CameraModel: cfg.MQTT.CameraModel,
		}
		mqttPub = mqtt.NewHAPublisher(mqttCfg, cam, store, bus, log.With("component", "mqtt"))
		if err := mqttPub.Start(ctx); err != nil {
			log.Error("MQTT publisher failed to start", "error", err)
			mqttPub = mqtt.NewStubPublisher(log.With("component", "mqtt"))
			mqttPub.Start(ctx)
		}
	} else {
		mqttPub = mqtt.NewStubPublisher(log.With("component", "mqtt"))
		mqttPub.Start(ctx)
	}

	// HLS proxy
	var hlsProxy *hlsproxy.Proxy
	if cfg.HLS.Enabled {
		hlsCfg := hlsproxy.Config{
			OutputDir:    cfg.HLS.OutputDir,
			SegmentTime:  cfg.HLS.SegmentTime,
			PlaylistSize: cfg.HLS.PlaylistSize,
			FFmpegPath:   cfg.HLS.FFmpegPath,
		}
		hlsProxy = hlsproxy.New(hlsCfg, log.With("component", "hls"))
		log.Info("HLS proxy enabled")
	}

	// Inject dependencies into API server now that everything is ready
	apiServer.SetDependencies(cam, hlsProxy, babyUID)

	select {
	case sig := <-sigCh:
		log.Info("received shutdown signal", "signal", sig)
	case err := <-errCh:
		return err
	}

	log.Info("shutting down...")
	cancel()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Error("HTTP server shutdown error", "error", err)
	}

	if err := mqttPub.Stop(shutdownCtx); err != nil {
		log.Error("MQTT publisher stop error", "error", err)
	}

	if hlsProxy != nil {
		if err := hlsProxy.Stop(shutdownCtx); err != nil {
			log.Error("HLS proxy stop error", "error", err)
		}
	}

	if err := cam.Stop(shutdownCtx); err != nil {
		log.Error("camera client stop error", "error", err)
	}

	log.Info("nanitd stopped")
	return nil
}

func setupLogger(cfg config.LogConfig) *slog.Logger {
	var level slog.Level
	switch strings.ToLower(cfg.Level) {
	case "debug":
		level = slog.LevelDebug
	case "warn", "warning":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	default:
		level = slog.LevelInfo
	}

	opts := &slog.HandlerOptions{Level: level}

	var handler slog.Handler
	if strings.ToLower(cfg.Format) == "json" {
		handler = slog.NewJSONHandler(os.Stdout, opts)
	} else {
		handler = slog.NewTextHandler(os.Stdout, opts)
	}

	return slog.New(handler)
}
