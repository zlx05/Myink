package handlers

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"myink/gateway/internal/config"
)

// Existing forwarding tests model the new internal authorization endpoints too.
func serveAuthFixture(w http.ResponseWriter, req *http.Request) bool {
	if req.URL.Path == "/internal/v1/auth/session" {
		user, tier, err := verifyJWT(req.Header.Get("Authorization"), []byte(config.Load().JWTSecret))
		if err != nil {
			w.WriteHeader(401)
			return true
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"user_id": user, "username": "fixture", "tier": tier})
		return true
	}
	if strings.HasSuffix(req.URL.Path, "/access") {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"ok":true}`)
		return true
	}
	return false
}
