package handlers

import (
	"context"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

// Installed before authentication/rate limiting so denied reads cannot be cached.
func AdminNoStore() gin.HandlerFunc {
	return func(c *gin.Context) {
		if c.Request.URL.Path == "/api/v1/admin" || strings.HasPrefix(c.Request.URL.Path, "/api/v1/admin/") {
			c.Header("Cache-Control", "no-store")
		}
		c.Next()
	}
}

// AdminRead only serves explicitly registered GET routes. Python rechecks the
// current database role and revoked session before using its reporting session.
func (h *TaskHandler) AdminRead(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	if c.Request.Method != http.MethodGet {
		c.AbortWithStatus(http.StatusMethodNotAllowed)
		return
	}
	if h.py == nil {
		c.AbortWithStatusJSON(503, gin.H{"error": "admin_unavailable"})
		return
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
	defer cancel()
	path := "/internal/v1/admin" + strings.TrimPrefix(c.Request.URL.Path, "/api/v1/admin")
	header := http.Header{"Authorization": []string{c.GetHeader("Authorization")}}
	resp, err := h.py.Forward(ctx, http.MethodGet, path, c.Request.URL.Query(), header, nil)
	if err != nil {
		c.AbortWithStatusJSON(502, gin.H{"error": "admin_unavailable"})
		return
	}
	defer resp.Body.Close()
	c.Header("Content-Type", resp.Header.Get("Content-Type"))
	c.Status(resp.StatusCode)
	if _, err := io.Copy(c.Writer, resp.Body); err != nil {
		c.Abort()
	}
}
