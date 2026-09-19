// Package handlers 网关 HTTP 层（阶段 2，唯一公网入口）。
// 身份阶段 3：JWT 验证（§14.1 ③ 归属校验双保险第一道门，替换阶段 2 X-Myink-User 占位）。
// 网关验签 → 解出可信 sub(user_id) → 写入上下文 + 透传 X-Myink-User 头给 Python API
// （Python 侧 require_owner 断言 project.user_id == 该身份，RLS 之外第二道门，见 api/auth.py）。
package handlers

import (
	"errors"
	"math"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"

	"myink/gateway/internal/trace"
)

const HeaderUser = "X-Myink-User"

// AuthError 是 JWT 校验失败（缺/坏 token）的错误，调用方转 401。
type AuthError struct{ msg string }

func (e *AuthError) Error() string { return e.msg }

// JWT 校验失败统一 401（缺 token / 坏 token / 过期 / 签名不符）。
var (
	errMissingToken   = &AuthError{"missing_bearer_token"}
	errInvalidToken   = &AuthError{"invalid_token"}
	errInvalidSubject = &AuthError{"invalid_subject"}
)

// JWTMiddleware 要求 `Authorization: Bearer <jwt>`，验签（HS256，secret 与 Python 共享
// .env）后把可信 sub(user_id) 写入 context 并透传 X-Myink-User 响应头。
// 缺/坏 token → 401：网关是唯一公网入口，身份头从此只在网关内部写入，外部不可伪造
// （否则 Python 侧归属断言可被绕过，§14.1 ③）。
func JWTMiddleware(secret []byte) gin.HandlerFunc {
	return func(c *gin.Context) {
		sub, tier, err := verifyJWT(c.GetHeader("Authorization"), secret)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": err.Error()})
			return
		}
		c.Set("user_id", sub)
		c.Set("user_tier", tier)
		// Claims were verified above; retain expiry to bound long-lived SSE requests.
		var claims jwt.RegisteredClaims
		_, _, _ = jwt.NewParser().ParseUnverified(strings.SplitN(c.GetHeader("Authorization"), " ", 2)[1], &claims)
		if claims.ExpiresAt != nil {
			c.Set("auth_expires", claims.ExpiresAt.Time)
		}
		c.Header(HeaderUser, sub)
		c.Next()
	}
}

// verifyJWT 解析 `Bearer <token>` 并验签，返回可信 sub + tier。失败返回对应 AuthError。
// tier 用于入队优先级（VIP → 高 priority；老 token 无 claim 回落 normal，优先级退化）。
func verifyJWT(authHeader string, secret []byte) (string, string, error) {
	if authHeader == "" {
		return "", "", errMissingToken
	}
	parts := strings.SplitN(authHeader, " ", 2)
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || strings.TrimSpace(parts[1]) == "" {
		return "", "", errInvalidToken
	}
	token, err := jwt.Parse(parts[1], func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, errors.New("unexpected_signing_method")
		}
		return secret, nil
	}, jwt.WithValidMethods([]string{"HS256"}), jwt.WithIssuer("myink"), jwt.WithExpirationRequired(), jwt.WithIssuedAt())
	if err != nil || !token.Valid {
		return "", "", errInvalidToken
	}
	sub, err := token.Claims.GetSubject()
	if err != nil || sub == "" {
		return "", "", errInvalidSubject
	}
	if _, err := uuid.Parse(sub); err != nil {
		return "", "", errInvalidSubject
	}
	claims, ok := token.Claims.(jwt.MapClaims)
	if !ok {
		return "", "", errInvalidToken
	}
	ver, ok := claims["ver"].(float64)
	if !ok || ver < 1 || math.Trunc(ver) != ver {
		return "", "", errInvalidToken
	}
	if issued, err := claims.GetIssuedAt(); err != nil || issued == nil {
		return "", "", errInvalidToken
	}
	tier := "normal"
	if claims, ok := token.Claims.(jwt.MapClaims); ok {
		if t, _ := claims["tier"].(string); t != "" {
			tier = t
		}
	}
	return sub, tier, nil
}

// GetUserID 取当前请求用户 ID（JWTMiddleware 之后，可信 sub）。
func GetUserID(c *gin.Context) string {
	if v, ok := c.Get("user_id"); ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return ""
}

// GetUserTier 取当前请求用户等级（JWTMiddleware 之后，tier claim；缺省 normal）。
func GetUserTier(c *gin.Context) string {
	if v, ok := c.Get("user_tier"); ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return "normal"
}

// GetTraceID 取当前请求 trace_id（trace.Middleware 之后）。
func GetTraceID(c *gin.Context) string {
	return trace.GetRequestID(c)
}
