// 网关健康检查：存活探针 + 就绪探针（Redis / Python API / worker 心跳）。
package handlers

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"myink/gateway/internal/config"
	"myink/gateway/internal/pyapi"
	"myink/gateway/internal/redis"
)

type HealthHandler struct {
	cfg config.Config
	r   *redis.Client
	py  *pyapi.Client
}

func NewHealthHandler(cfg config.Config, r *redis.Client, py *pyapi.Client) *HealthHandler {
	return &HealthHandler{cfg: cfg, r: r, py: py}
}

// Live 存活探针：进程在即 200（K8s liveness，不做依赖检查）。
// GET /healthz
func (h *HealthHandler) Live(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}

// Ready 就绪探针：Redis ping + Python API readyz + worker 心跳，任一降级 503。
// GET /readyz
func (h *HealthHandler) Ready(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
	defer cancel()
	degraded := []string{}

	if err := h.r.Ping(ctx); err != nil {
		degraded = append(degraded, "redis")
	}
	// Python API 就绪（内部探活，含其自身 DB 检查）
	if resp, err := h.py.Forward(ctx, http.MethodGet, "/readyz", nil, http.Header{}, nil); err != nil || resp.StatusCode != http.StatusOK {
		degraded = append(degraded, "python_api")
	} else {
		resp.Body.Close()
	}
	// worker 心跳：queue:heartbeat:* 任一条存在即认为 worker 存活（阶段 2 单进程探活）
	keys, err := h.r.Raw().Keys(ctx, "queue:heartbeat:*").Result()
	if err != nil || len(keys) == 0 {
		degraded = append(degraded, "worker")
	}

	if len(degraded) > 0 {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "degraded", "degraded": degraded})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}
