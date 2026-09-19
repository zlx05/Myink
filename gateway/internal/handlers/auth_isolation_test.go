package handlers

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"myink/gateway/internal/config"
	"myink/gateway/internal/pyapi"
)

const isolationUser = "a1111111-1111-4111-8111-111111111111"

func strictToken(t *testing.T, change func(jwt.MapClaims)) string {
	t.Helper()
	claims := jwt.MapClaims{"sub": isolationUser, "iss": "myink", "iat": time.Now().Unix(), "exp": time.Now().Add(time.Hour).Unix(), "ver": 1, "tier": "normal"}
	if change != nil {
		change(claims)
	}
	token, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString([]byte(config.Load().JWTSecret))
	if err != nil {
		t.Fatal(err)
	}
	return "Bearer " + token
}

func TestJWTRejectsMissingSecurityClaims(t *testing.T) {
	for _, version := range []any{0, -1, 1.5, true, "1"} {
		token := strictToken(t, func(c jwt.MapClaims) { c["ver"] = version })
		if _, _, err := verifyJWT(token, []byte(config.Load().JWTSecret)); err == nil {
			t.Fatalf("accepted invalid session version %v", version)
		}
	}
	for _, claim := range []string{"iss", "exp", "iat", "ver"} {
		t.Run(claim, func(t *testing.T) {
			token := strictToken(t, func(c jwt.MapClaims) { delete(c, claim) })
			if _, _, err := verifyJWT(token, []byte(config.Load().JWTSecret)); err == nil {
				t.Fatalf("accepted token without %s", claim)
			}
		})
	}
	for _, value := range []string{"wrong-issuer", "invalid-subject"} {
		t.Run(value, func(t *testing.T) {
			token := strictToken(t, func(c jwt.MapClaims) {
				if value == "wrong-issuer" {
					c["iss"] = "other"
				} else {
					c["sub"] = "not-a-uuid"
				}
			})
			if _, _, err := verifyJWT(token, []byte(config.Load().JWTSecret)); err == nil {
				t.Fatal("accepted invalid identity")
			}
		})
	}
}

func TestRevokedSessionCannotReadProjects(t *testing.T) {
	businessCalls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/internal/v1/auth/session" {
			w.WriteHeader(401)
			fmt.Fprint(w, `{"detail":"invalid_token"}`)
			return
		}
		businessCalls++
		fmt.Fprint(w, `[]`)
	}))
	defer upstream.Close()
	router := NewRouter(config.Load(), nil, nil, pyapi.New(upstream.URL, time.Second))
	request := httptest.NewRequest("GET", "/api/v1/projects", nil)
	request.Header.Set("Authorization", strictToken(t, nil))
	result := httptest.NewRecorder()
	router.ServeHTTP(result, request)
	if result.Code != 401 || businessCalls != 0 {
		t.Fatalf("status=%d business calls=%d", result.Code, businessCalls)
	}
}

func TestForeignWritingOperationsDeniedBeforeSideEffects(t *testing.T) {
	for _, endpoint := range []struct{ method, path, body string }{
		{"POST", "/api/v1/projects/b1111111-1111-4111-8111-111111111111/chapters/new/generate", `{"seq":1}`},
		{"POST", "/api/v1/projects/b1111111-1111-4111-8111-111111111111/batches/generate", `{"size":1,"start":1}`},
		{"GET", "/api/v1/tasks/c1111111-1111-4111-8111-111111111111/events", ""},
	} {
		t.Run(endpoint.path, func(t *testing.T) {
			accessCalls := 0
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				if r.URL.Path == "/internal/v1/auth/session" {
					json.NewEncoder(w).Encode(map[string]string{"user_id": isolationUser, "username": "owner", "tier": "normal"})
					return
				}
				if strings.HasSuffix(r.URL.Path, "/access") {
					accessCalls++
					if r.Header.Get(HeaderUser) != isolationUser {
						t.Errorf("untrusted identity forwarded: %s", r.Header.Get(HeaderUser))
					}
					w.WriteHeader(403)
					fmt.Fprint(w, `{"detail":"forbidden"}`)
					return
				}
				t.Errorf("unexpected upstream call %s", r.URL.Path)
			}))
			defer upstream.Close()
			// Nil queue/Redis ensures authorization happens before touching either.
			router := NewRouter(config.Load(), nil, nil, pyapi.New(upstream.URL, time.Second))
			request := httptest.NewRequest(endpoint.method, endpoint.path, strings.NewReader(endpoint.body))
			request.Header.Set("Content-Type", "application/json")
			request.Header.Set("Authorization", strictToken(t, nil))
			request.Header.Set(HeaderUser, "forged-owner")
			result := httptest.NewRecorder()
			router.ServeHTTP(result, request)
			if result.Code != 403 || accessCalls != 1 {
				t.Fatalf("status=%d access checks=%d body=%s", result.Code, accessCalls, result.Body.String())
			}
		})
	}
}

func TestSessionCheckFailsClosedWhenUpstreamFails(t *testing.T) {
	for _, status := range []int{500, 503, 200} {
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status); fmt.Fprint(w, `{"invalid":true}`) }))
		router := NewRouter(config.Load(), nil, nil, pyapi.New(upstream.URL, time.Second))
		req := httptest.NewRequest("GET", "/api/v1/projects", nil)
		req.Header.Set("Authorization", strictToken(t, nil))
		result := httptest.NewRecorder()
		router.ServeHTTP(result, req)
		upstream.Close()
		if result.Code < 400 {
			t.Fatalf("accepted broken session response %d", status)
		}
	}
}

func TestAuthRegistrationProxyBoundsAndRateLimit(t *testing.T) {
	r := newTestRedis(t)
	peer := "198.51.100.231"
	digest := sha256.Sum256([]byte(peer))
	key := fmt.Sprintf("rate:auth:%x", digest)
	r.Raw().Del(context.Background(), key)
	t.Cleanup(func() { r.Raw().Del(context.Background(), key) })
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		calls++
		if req.URL.Path != "/internal/v1/auth/register" || req.Header.Get(HeaderUser) != "" {
			t.Error("wrong auth proxy target/identity")
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(201)
		fmt.Fprint(w, `{"token":"test","user_id":"test","username":"new","tier":"normal","expires_in":1800}`)
	}))
	defer upstream.Close()
	router := NewRouter(config.Load(), r, nil, pyapi.New(upstream.URL, time.Second))
	for i := 0; i < 21; i++ {
		body := `{"username":"new","password":"long-password-test"}`
		if i == 0 {
			body = strings.Repeat("x", 5000)
		}
		req := httptest.NewRequest("POST", "/api/v1/auth/register", strings.NewReader(body))
		req.RemoteAddr = peer + ":12345"
		req.Header.Set(HeaderUser, "forged-owner")
		req.Header.Set("X-Forwarded-For", fmt.Sprintf("203.0.113.%d", i))
		result := httptest.NewRecorder()
		router.ServeHTTP(result, req)
		want := 201
		if i == 0 {
			want = 413
		}
		if i == 20 {
			want = 429
		}
		if result.Code != want || result.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("request %d status=%d cache=%s", i, result.Code, result.Header().Get("Cache-Control"))
		}
	}
	if calls != 19 {
		t.Fatalf("unexpected upstream calls %d", calls)
	}
}

func TestOpenSSEClosesAfterSessionRevocation(t *testing.T) {
	testSSESessionDeadline(t, false)
}

func TestOpenSSEClosesAtTokenExpiry(t *testing.T) {
	testSSESessionDeadline(t, true)
}

func testSSESessionDeadline(t *testing.T, expire bool) {
	t.Helper()
	r := newTestRedis(t)
	taskID := "d1111111-1111-4111-8111-111111111111"
	key := "queue:sse:" + taskID
	r.Raw().Del(context.Background(), key)
	r.XAdd(context.Background(), key, map[string]any{"event": "status", "status": "running"})
	t.Cleanup(func() { r.Raw().Del(context.Background(), key) })
	var revoked atomic.Bool
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path == "/internal/v1/auth/session" {
			if revoked.Load() {
				w.WriteHeader(401)
				return
			}
			json.NewEncoder(w).Encode(map[string]string{"user_id": isolationUser, "username": "owner", "tier": "normal"})
			return
		}
		fmt.Fprint(w, `{"ok":true}`)
	}))
	defer upstream.Close()
	server := httptest.NewServer(NewRouter(config.Load(), r, nil, pyapi.New(upstream.URL, time.Second)))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", server.URL+"/api/v1/tasks/"+taskID+"/events", nil)
	req.Header.Set("Authorization", strictToken(t, func(claims jwt.MapClaims) {
		if expire {
			claims["exp"] = time.Now().Add(2 * time.Second).Unix()
		}
	}))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	if resp.Header.Get("Cache-Control") != "no-store" {
		t.Fatalf("private SSE must not be cached: %s", resp.Header.Get("Cache-Control"))
	}
	if !expire {
		revoked.Store(true)
	}
	started := time.Now()
	if _, err = io.ReadAll(resp.Body); err != nil {
		t.Fatalf("revoked stream did not close before deadline: %v", err)
	}
	if expire && time.Since(started) > 10*time.Second {
		t.Fatal("expired stream waited for the 15-second session refresh")
	}
}

func TestEveryAuthActionBoundsRequestBody(t *testing.T) {
	for _, method := range []string{"GET", "POST"} {
		router := gin.New()
		router.Use(gin.Recovery())
		handler := &TaskHandler{}
		router.Handle(method, "/api/v1/auth/session", handler.AuthAction)
		req := httptest.NewRequest(method, "/api/v1/auth/session", strings.NewReader(strings.Repeat("x", 5000)))
		result := httptest.NewRecorder()
		router.ServeHTTP(result, req)
		if result.Code != 413 {
			t.Fatalf("%s unbounded auth body status=%d", method, result.Code)
		}
	}
}
