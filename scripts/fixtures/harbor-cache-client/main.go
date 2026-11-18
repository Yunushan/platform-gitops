// A disposable protocol fixture built inside the exact reviewed Harbor source
// tree. It uses the real public constructors, not a replacement Redis client.
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/goharbor/harbor/src/lib/cache"
	harborcache "github.com/goharbor/harbor/src/lib/cache/redis"
	legacy "github.com/goharbor/harbor/src/lib/redis"
	"github.com/gomodule/redigo/redis"
)

func main() {
	password := os.Getenv("HARBOR_FIXTURE_PASSWORD")
	address := os.Getenv("HARBOR_FIXTURE_ADDRESS")
	if !regexp.MustCompile(`^[A-Za-z0-9_-]{32,128}$`).MatchString(password) ||
		(address != "192.0.2.71:6381" && address != "192.0.2.72:6381") {
		os.Exit(2)
	}
	redisURL := func(database int) string {
		return (&url.URL{Scheme: "rediss", Host: address, User: url.UserPassword("harbor_component", password), Path: "/" + strconv.Itoa(database), RawQuery: "dial_timeout=1s&read_timeout=1s&write_timeout=1s&max_retries=1"}).String()
	}
	core, err := harborcache.New(cache.Options{Address: redisURL(0), Prefix: "harbor-component:"})
	if err != nil {
		os.Exit(2)
	}
	poolURL := (&url.URL{Scheme: "rediss", Host: address, User: url.UserPassword("harbor_component", password), Path: "/1"}).String()
	pool, err := legacy.GetRedisPool("disposable-component-check", poolURL, nil)
	if err != nil {
		os.Exit(2)
	}
	defer pool.Close()
	var mu sync.Mutex
	var sequence uint64
	probe := func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != "GET" || request.URL.Path != "/roundtrip" {
			writer.WriteHeader(http.StatusNotFound)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		sequence++
		ctx, cancel := context.WithTimeout(request.Context(), 4*time.Second)
		defer cancel()
		key := "witness-" + strconv.FormatUint(sequence, 10)
		payload := "synthetic-component-value"
		var read string
		coreOK := core.Ping(ctx) == nil && core.Save(ctx, key, payload, 180*time.Second) == nil &&
			core.Fetch(ctx, key, &read) == nil && read == payload && core.Contains(ctx, key)
		connection, connectionErr := pool.GetContext(ctx)
		defer connection.Close()
		legacyOK, separate, scoped := false, false, false
		if connectionErr == nil {
			pong, pingErr := redis.String(connection.Do("PING"))
			other, otherErr := connection.Do("GET", "harbor-component:"+key)
			separate = coreOK && otherErr == nil && other == nil
			_, saveErr := connection.Do("SET", "harbor-component:legacy", payload, "PX", 180000)
			got, readErr := redis.String(connection.Do("GET", "harbor-component:legacy"))
			_, deleteErr := connection.Do("DEL", "harbor-component:legacy")
			legacyOK = pingErr == nil && pong == "PONG" && saveErr == nil && readErr == nil && got == payload && deleteErr == nil
			denied := func(err error) bool { return err != nil && strings.HasPrefix(err.Error(), "NOPERM ") }
			_, foreignErr := connection.Do("GET", "unreviewed:key")
			_, configErr := connection.Do("CONFIG", "GET", "*")
			client := core.(*harborcache.Cache).Client
			scoped = denied(foreignErr) && denied(configErr) && denied(client.Get(ctx, "unreviewed:key").Err()) && denied(client.ConfigGet(ctx, "*").Err())
		}
		if coreOK {
			coreOK = core.Delete(ctx, key) == nil && !core.Contains(ctx, key)
		}
		writer.Header().Set("Content-Type", "application/json")
		if !coreOK || !legacyOK || !separate || !scoped {
			writer.WriteHeader(http.StatusServiceUnavailable)
		}
		json.NewEncoder(writer).Encode(map[string]bool{"core_cache_verified": coreOK, "legacy_pool_verified": legacyOK, "logical_databases_separate": separate, "scoped_acl_verified": scoped})
	}
	server := http.Server{Addr: "127.0.0.1:19120", Handler: http.HandlerFunc(probe), ReadHeaderTimeout: time.Second, ReadTimeout: 2 * time.Second, WriteTimeout: 6 * time.Second, IdleTimeout: 2 * time.Second, MaxHeaderBytes: 4096}
	if server.ListenAndServe() != nil {
		os.Exit(2)
	}
}
