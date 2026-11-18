// Isolated pgx v4 pool reproduction; never a platform service or SQL proxy.
package main

import (
	"bufio"
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v4"
	"github.com/jackc/pgx/v4/stdlib"
)

func main() {
	if err := run(); err != nil {
		os.Exit(1)
	} // Never print connection strings or driver errors.
}

func run() error {
	if len(os.Args) != 3 || (os.Args[1] != "plain" && os.Args[1] != "checked") ||
		!filepath.IsAbs(os.Args[2]) || len(os.Args[2]) > 240 {
		return errors.New("reviewed arguments required")
	}
	config, err := pgx.ParseConfig("")
	if err != nil || config.Host != "127.0.0.1" || config.Port != 6432 ||
		config.User != "harbor_fixture" || config.Database != "registry" ||
		os.Getenv("PGSSLMODE") != "verify-full" || config.TLSConfig == nil || len(config.Fallbacks) != 0 {
		return errors.New("isolated TLS endpoint required")
	}
	var rejected atomic.Int64
	var db *sql.DB
	if os.Args[1] == "plain" {
		// Exact default stdlib path used by Harbor's orm.RegisterDataBase.
		db, err = sql.Open("pgx", "host=127.0.0.1 port=6432 user=harbor_fixture dbname=registry sslmode=verify-full")
	} else {
		// Candidate pre-use health check, not a retry of an application query.
		db = stdlib.OpenDB(*config, stdlib.OptionResetSession(func(ctx context.Context, conn *pgx.Conn) error {
			bounded, cancel := context.WithTimeout(ctx, 3*time.Second)
			defer cancel()
			if conn.Ping(bounded) != nil {
				rejected.Add(1)
				return driver.ErrBadConn
			}
			return nil
		}))
	}
	if err != nil {
		return errors.New("pool initialization failed")
	}
	defer db.Close()
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(2)
	listener, err := net.Listen("unix", os.Args[2])
	if err != nil {
		return errors.New("owned socket unavailable")
	}
	defer listener.Close()
	defer os.Remove(os.Args[2])
	if os.Chmod(os.Args[2], 0600) != nil {
		return errors.New("socket restriction failed")
	}
	for _, phase := range []string{"warm", "after"} {
		connection, err := listener.Accept()
		if err != nil {
			return errors.New("owned client unavailable")
		}
		connection.SetDeadline(time.Now().Add(10 * time.Second))
		line, err := bufio.NewReader(io.LimitReader(connection, 32)).ReadString('\n')
		if err != nil || line != phase+"\n" {
			connection.Close()
			return errors.New("ordered commands required")
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		var recovery, tls bool
		var address string
		var pid int
		err = db.QueryRowContext(ctx, "SELECT pg_is_in_recovery(), inet_server_addr()::text, pg_backend_pid(), ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").Scan(&recovery, &address, &pid, &tls)
		cancel()
		category := "none"
		if err != nil {
			category = "other"
			if errors.Is(err, io.ErrUnexpectedEOF) || strings.Contains(err.Error(), "unexpected EOF") {
				category = "unexpected_eof"
			}
			if errors.Is(err, driver.ErrBadConn) {
				category = "bad_connection"
			}
		}
		result := map[string]any{"phase": phase, "mode": os.Args[1], "ok": err == nil, "error_class": category,
			"tls": tls, "in_recovery": recovery, "server_address": address, "backend_pid": pid,
			"application_query_calls": 1, "pre_use_rejections": rejected.Load(),
			"max_open_connections": db.Stats().MaxOpenConnections}
		if json.NewEncoder(connection).Encode(result) != nil {
			connection.Close()
			return errors.New("owned result unavailable")
		}
		connection.Close()
	}
	return nil
}
