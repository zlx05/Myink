package handlers

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"myink/gateway/internal/config"
	"myink/gateway/internal/pyapi"
)

func TestAdminReadProxySecurity(t *testing.T) {
	cfg := config.Load()
	cfg.RatePerSec, cfg.RateBurst = 1000, 1000
	token := strictToken(t, nil)
	status := 200
	forwarded := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if serveAuthFixture(w, r) {
			return
		}
		forwarded++
		if r.Method != "GET" || r.URL.Path != "/internal/v1/admin/users" || r.URL.Query().Get("limit") != "1" || r.Header.Get("Authorization") != token {
			t.Errorf("incorrect admin proxy method/path/query/bearer")
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		w.Write([]byte(`{"items":[],"total":0,"limit":1,"offset":0}`))
	}))
	defer server.Close()
	router := NewRouter(cfg, nil, nil, pyapi.New(server.URL, time.Second))
	for _, code := range []int{200, 403, 401, 404, 503} {
		status = code
		req := httptest.NewRequest("GET", "/api/v1/admin/users?limit=1", nil)
		req.Header.Set("Authorization", token)
		response := httptest.NewRecorder()
		router.ServeHTTP(response, req)
		if response.Code != code || response.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("admin proxy got %d cache=%q; want %d no-store", response.Code, response.Header().Get("Cache-Control"), code)
		}
	}
	before := forwarded
	for _, method := range []string{"POST", "PUT", "PATCH", "DELETE"} {
		req := httptest.NewRequest(method, "/api/v1/admin/users?limit=1", nil)
		req.Header.Set("Authorization", token)
		response := httptest.NewRecorder()
		router.ServeHTTP(response, req)
		if response.Code < 400 || response.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("write accepted/cached: %d", response.Code)
		}
	}
	for _, token := range []string{"", "Bearer invalid"} {
		req := httptest.NewRequest("GET", "/api/v1/admin/users?limit=1", nil)
		req.Header.Set("Authorization", token)
		req.Header.Set(HeaderUser, "11111111-1111-1111-1111-111111111111")
		response := httptest.NewRecorder()
		router.ServeHTTP(response, req)
		if response.Code != 401 || response.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("unauthenticated admin got %d cache=%q", response.Code, response.Header().Get("Cache-Control"))
		}
	}
	if forwarded != before {
		t.Fatal("write/unauthenticated request forwarded")
	}
}
