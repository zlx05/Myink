package handlers

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"myink/gateway/internal/config"
	"myink/gateway/internal/pyapi"
)

func TestDraftProjectCannotEnterGenerationQueue(t *testing.T) {
	for _, batch := range []bool{false, true} {
		t.Run(map[bool]string{false: "chapter", true: "batch"}[batch], func(t *testing.T) {
			py := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Query().Get("write") != "true" {
					t.Error("generation checked ownership but not creation readiness")
				}
				w.WriteHeader(http.StatusConflict)
				_, _ = w.Write([]byte(`{"detail":"PROJECT_NOT_READY"}`))
			}))
			defer py.Close()
			// No queue/Redis: attempting enqueue at all is a test failure.
			h := NewTaskHandler(config.Config{}, nil, nil, pyapi.New(py.URL, time.Second))
			router := gin.New()
			router.Use(func(c *gin.Context) { c.Set("user_id", "owner"); c.Next() })
			path, body := "/projects/p/chapters/c/generate", `{"seq":1}`
			if batch {
				router.POST("/projects/:project_id/batches/generate", h.CreateBatch)
				path, body = "/projects/p/batches/generate", `{"size":1,"start":1}`
			} else {
				router.POST("/projects/:project_id/chapters/:chapter_id/generate", h.CreateChapter)
			}
			req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			response := httptest.NewRecorder()
			router.ServeHTTP(response, req)
			if response.Code != http.StatusConflict || !strings.Contains(response.Body.String(), "PROJECT_NOT_READY") {
				t.Fatalf("expected draft refusal, got %d %s", response.Code, response.Body.String())
			}
		})
	}
}
