package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"tailscale.com/tsnet"
)

type config struct {
	mode       string
	hostname   string
	authKey    string
	stateDir   string
	listenPort int
	localHost  string
	localPort  int
	targetHost string
	targetPort int
	ephemeral  bool
	upTimeout  time.Duration
}

func main() {
	cfg := parseConfig()
	targetAddr := net.JoinHostPort(cfg.targetHost, strconv.Itoa(cfg.targetPort))

	srv := &tsnet.Server{
		Hostname:  cfg.hostname,
		AuthKey:   cfg.authKey,
		Dir:       cfg.stateDir,
		Ephemeral: cfg.ephemeral,
	}
	defer srv.Close()

	upCtx, cancel := context.WithTimeout(context.Background(), cfg.upTimeout)
	defer cancel()

	log.Printf("starting tailscale: hostname=%s listen=:%d target=%s", cfg.hostname, cfg.listenPort, targetAddr)
	status, err := srv.Up(upCtx)
	if err != nil {
		log.Fatalf("tailscale up failed: %v", err)
	}

	if cfg.mode == "client" {
		runClientProxy(srv, cfg, targetAddr)
		return
	}

	runServerProxy(srv, cfg, targetAddr, status.TailscaleIPs)
}

func runServerProxy(srv *tsnet.Server, cfg config, targetAddr string, tailscaleIPs any) {
	listenAddr := fmt.Sprintf(":%d", cfg.listenPort)
	ln, err := srv.Listen("tcp", listenAddr)
	if err != nil {
		log.Fatalf("listen on tailnet %s failed: %v", listenAddr, err)
	}
	defer ln.Close()

	log.Printf("xqshare tailscale proxy ready: hostname=%s tailscale_ips=%v listen=%s target=%s",
		cfg.hostname, tailscaleIPs, listenAddr, targetAddr)

	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Fatalf("accept failed: %v", err)
		}

		go proxyConn(conn, targetAddr)
	}
}

func runClientProxy(srv *tsnet.Server, cfg config, targetAddr string) {
	localAddr := net.JoinHostPort(cfg.localHost, strconv.Itoa(cfg.localPort))
	ln, err := net.Listen("tcp", localAddr)
	if err != nil {
		log.Fatalf("listen on local %s failed: %v", localAddr, err)
	}
	defer ln.Close()

	log.Printf("xqshare tailscale client proxy ready: local=%s target=%s", localAddr, targetAddr)

	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Fatalf("accept failed: %v", err)
		}

		go proxyTailnetConn(srv, conn, targetAddr)
	}
}

func parseConfig() config {
	defaultStateDir := filepath.Join(".", "tsnet-state")

	cfg := config{
		mode:       envString("XQSHARE_TS_MODE", "server"),
		hostname:   envString("XQSHARE_TS_HOSTNAME", "xqshare-server"),
		authKey:    envString("XQSHARE_TS_AUTHKEY", os.Getenv("TS_AUTHKEY")),
		stateDir:   envString("XQSHARE_TS_STATE_DIR", defaultStateDir),
		listenPort: envInt("XQSHARE_TS_LISTEN_PORT", envInt("XQSHARE_REMOTE_PORT", 18812)),
		localHost:  envString("XQSHARE_TS_LOCAL_HOST", "127.0.0.1"),
		localPort:  envInt("XQSHARE_TS_LOCAL_PORT", envInt("XQSHARE_REMOTE_PORT", 18812)),
		targetHost: envString("XQSHARE_TS_TARGET_HOST", "127.0.0.1"),
		targetPort: envInt("XQSHARE_TS_TARGET_PORT", envInt("XQSHARE_PORT", 18812)),
		ephemeral:  envBool("XQSHARE_TS_EPHEMERAL", false),
		upTimeout:  envDuration("XQSHARE_TS_UP_TIMEOUT", 60*time.Second),
	}

	flag.StringVar(&cfg.mode, "mode", cfg.mode, "proxy mode: server or client")
	flag.StringVar(&cfg.hostname, "hostname", cfg.hostname, "Tailscale node hostname")
	flag.StringVar(&cfg.authKey, "authkey", cfg.authKey, "Tailscale auth key; also supports TS_AUTHKEY or XQSHARE_TS_AUTHKEY")
	flag.StringVar(&cfg.stateDir, "state-dir", cfg.stateDir, "tsnet state directory")
	flag.IntVar(&cfg.listenPort, "listen-port", cfg.listenPort, "tailnet TCP port to expose")
	flag.StringVar(&cfg.localHost, "local-host", cfg.localHost, "client mode local listen host")
	flag.IntVar(&cfg.localPort, "local-port", cfg.localPort, "client mode local listen port")
	flag.StringVar(&cfg.targetHost, "target-host", cfg.targetHost, "local xqshare server host")
	flag.IntVar(&cfg.targetPort, "target-port", cfg.targetPort, "local xqshare server port")
	flag.BoolVar(&cfg.ephemeral, "ephemeral", cfg.ephemeral, "use an ephemeral Tailscale node")
	flag.DurationVar(&cfg.upTimeout, "up-timeout", cfg.upTimeout, "maximum time to wait for Tailscale to connect")
	flag.Parse()

	if cfg.mode != "server" && cfg.mode != "client" {
		log.Fatalf("invalid mode: %s", cfg.mode)
	}
	if cfg.stateDir == "" && !cfg.ephemeral {
		log.Fatal("state directory is required unless --ephemeral is set")
	}
	if cfg.listenPort <= 0 || cfg.listenPort > 65535 {
		log.Fatalf("invalid listen port: %d", cfg.listenPort)
	}
	if cfg.targetPort <= 0 || cfg.targetPort > 65535 {
		log.Fatalf("invalid target port: %d", cfg.targetPort)
	}
	if cfg.localPort <= 0 || cfg.localPort > 65535 {
		log.Fatalf("invalid local port: %d", cfg.localPort)
	}
	if cfg.upTimeout <= 0 {
		log.Fatal("up timeout must be greater than zero")
	}

	return cfg
}

func proxyConn(client net.Conn, targetAddr string) {
	defer client.Close()

	target, err := net.Dial("tcp", targetAddr)
	if err != nil {
		log.Printf("dial target %s failed: %v", targetAddr, err)
		return
	}
	defer target.Close()

	var wg sync.WaitGroup
	wg.Add(2)

	go copyAndClose(&wg, target, client)
	go copyAndClose(&wg, client, target)

	wg.Wait()
}

func proxyTailnetConn(srv *tsnet.Server, client net.Conn, targetAddr string) {
	defer client.Close()

	target, err := srv.Dial(context.Background(), "tcp", targetAddr)
	if err != nil {
		log.Printf("dial tailnet target %s failed: %v", targetAddr, err)
		return
	}
	defer target.Close()

	var wg sync.WaitGroup
	wg.Add(2)

	go copyAndClose(&wg, target, client)
	go copyAndClose(&wg, client, target)

	wg.Wait()
}

func copyAndClose(wg *sync.WaitGroup, dst net.Conn, src net.Conn) {
	defer wg.Done()
	_, _ = io.Copy(dst, src)

	if tcpConn, ok := dst.(*net.TCPConn); ok {
		_ = tcpConn.CloseWrite()
		return
	}

	_ = dst.Close()
}

func envString(name string, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) int {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}

	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}

	return parsed
}

func envBool(name string, fallback bool) bool {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}

	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}

	return parsed
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}

	parsed, err := time.ParseDuration(value)
	if err != nil {
		return fallback
	}

	return parsed
}
