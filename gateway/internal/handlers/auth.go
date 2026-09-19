package handlers

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"myink/gateway/internal/pyapi"
	"myink/gateway/internal/redis"
)

// SessionMiddleware checks revocation and obtains the account's current tier.
func SessionMiddleware(py *pyapi.Client) gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		if py == nil {
			c.AbortWithStatusJSON(503, gin.H{"error": "auth_unavailable"})
			return
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()
		header := http.Header{"Authorization": []string{c.GetHeader("Authorization")}}
		resp, err := py.Forward(ctx, http.MethodGet, "/internal/v1/auth/session", nil, header, nil)
		if err != nil {
			c.AbortWithStatusJSON(503, gin.H{"error": "auth_unavailable"})
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode == 401 || resp.StatusCode == 403 {
			c.AbortWithStatusJSON(401, gin.H{"error": "invalid_token"})
			return
		}
		var session struct {
			UserID   string `json:"user_id"`
			Username string `json:"username"`
			Tier     string `json:"tier"`
		}
		if resp.StatusCode != 200 || json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&session) != nil || session.UserID != GetUserID(c) || session.Username == "" {
			c.AbortWithStatusJSON(503, gin.H{"error": "auth_unavailable"})
			return
		}
		c.Set("user_tier", session.Tier)
		c.Next()
	}
}

func checkAccess(c *gin.Context, py *pyapi.Client, kind, id string, writing ...bool) bool {
	if py == nil {
		c.AbortWithStatusJSON(503, gin.H{"error": "authorization_unavailable"})
		return false
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()
	header := http.Header{HeaderUser: []string{GetUserID(c)}, "Authorization": []string{c.GetHeader("Authorization")}}
	query := url.Values{}
	if len(writing) > 0 && writing[0] {
		query.Set("write", "true")
	}
	resp, err := py.Forward(ctx, http.MethodGet, "/internal/v1/"+kind+"/"+url.PathEscape(id)+"/access", query, header, nil)
	if err != nil {
		c.AbortWithStatusJSON(503, gin.H{"error": "authorization_unavailable"})
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		if resp.StatusCode == http.StatusConflict && query.Get("write") == "true" {
			c.AbortWithStatusJSON(http.StatusConflict, gin.H{"error": "PROJECT_NOT_READY"})
			return false
		}
		status := resp.StatusCode
		if status != 400 && status != 401 && status != 403 && status != 404 {
			status = 503
		}
		c.AbortWithStatusJSON(status, gin.H{"error": "access_denied"})
		return false
	}
	var result struct {
		OK bool `json:"ok"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&result) != nil || !result.OK {
		c.AbortWithStatusJSON(503, gin.H{"error": "authorization_unavailable"})
		return false
	}
	return true
}

// Shared Redis counters use the TCP peer, not user-supplied forwarding headers.
func AuthRateLimit(r *redis.Client) gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 4096)
		if r == nil {
			c.AbortWithStatusJSON(503, gin.H{"error": "auth_unavailable"})
			return
		}
		peer, _, err := net.SplitHostPort(c.Request.RemoteAddr)
		if err != nil {
			peer = c.Request.RemoteAddr
		}
		digest := sha256.Sum256([]byte(peer))
		key := fmt.Sprintf("rate:auth:%x", digest)
		count, err := r.Eval(c.Request.Context(), `local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n`, []string{key})
		if err != nil {
			c.AbortWithStatusJSON(503, gin.H{"error": "auth_unavailable"})
			return
		}
		if n, ok := count.(int64); !ok || n > 20 {
			c.Header("Retry-After", "60")
			c.AbortWithStatusJSON(429, gin.H{"error": "auth_rate_limited"})
			return
		}
		c.Next()
	}
}

func (h *TaskHandler) AuthAction(c *gin.Context) {
	body, err := io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, 4096))
	if err != nil {
		c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "auth_body_too_large"})
		return
	}
	action := strings.TrimPrefix(c.FullPath(), "/api/v1/auth/")
	h.forwardToPy(c, "/internal/v1/auth/"+action, body)
}
