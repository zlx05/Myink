// Package pyapi 网关到 Python API 的内部 HTTP 转发（网关不直连 DB，RLS 真源在 Python 侧，§17.2）。
package pyapi

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

type Client struct {
	base    string
	httpc   *http.Client
	timeout time.Duration
	longc   *http.Client // 长耗时同步生成端点专用（LLM 草稿生成常态超 30s）
}

func New(base string, timeout time.Duration) *Client {
	return &Client{
		base:    base,
		timeout: timeout,
		httpc:   &http.Client{Timeout: timeout},
		longc:   &http.Client{Timeout: 180 * time.Second},
	}
}

// Forward 把网关请求原样转发给 Python API（方法/路径/查询/头/体），响应透传。
// 用于 tasks 详情、pause/resume/cancel、章节 CRUD 等同步查询与控制端点。
func (c *Client) ForwardLong(ctx context.Context, method, path string, query url.Values, header http.Header, body io.Reader) (*http.Response, error) {
	u := c.base + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, body)
	if err != nil {
		return nil, err
	}
	// 透传对 RLS/租户上下文有意义的头（含 X-Myink-User 占位身份）
	for k := range header {
		req.Header.Set(k, header.Get(k))
	}
	return c.longc.Do(req)
}
func (c *Client) Forward(ctx context.Context, method, path string, query url.Values, header http.Header, body io.Reader) (*http.Response, error) {
	u := c.base + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, body)
	if err != nil {
		return nil, err
	}
	// 透传对 RLS/租户上下文有意义的头（含 X-Myink-User 占位身份）
	for k := range header {
		req.Header.Set(k, header.Get(k))
	}
	return c.httpc.Do(req)
}

// GetTaskDetail 调 Python API 取任务详情（含批次进度 i/N），供网关任务查询与 SSE 兜底。
// 返回原始 JSON（透传，前端无需感知内部结构）+ 上游状态码（调用方需区分 404 vs 故障）。
func (c *Client) GetTaskDetail(ctx context.Context, taskID string) ([]byte, int, error) {
	u := fmt.Sprintf("%s/internal/v1/tasks/%s", c.base, url.PathEscape(taskID))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, 0, err
	}
	resp, err := c.httpc.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}
	if resp.StatusCode >= 400 {
		return nil, resp.StatusCode, fmt.Errorf("python api %d: %s", resp.StatusCode, string(b))
	}
	return b, resp.StatusCode, nil
}

// JSONResp 解析转发响应的 JSON 体（失败返回 nil，调用方按需兜底）。
func JSONResp(resp *http.Response) (map[string]any, error) {
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}
