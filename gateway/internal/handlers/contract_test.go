// 契约一致性测试（阶段 5 契约测试形式化）：网关转发契约与 Python 侧单一事实源
// spec/api-openapi.json 对齐。
//
// 独立运行：不依赖 Redis（不调 newTestRedis/newRouter），只依赖已提交的契约文件 +
// 标准库 encoding/json。两类断言：
//  1. TestContractHasAllForwardedPaths —— 网关每个转发内部路径必须在契约里有同形状
//     path + method（防 BatchControl 式路径漂移：曾转发 /internal/v1/batches/... 404）。
//  2. TestFakePyFieldsWithinContractSchema —— 假服务（fakePy）响应与契约 200 schema
//     双向键对齐：契约 required ⊆ 假键（抓"缺字段"）且假键 ⊆ properties（抓"多/错
//     字段"），数组递归。单向"假⊆契约"抓不住"假项目缺 current_chapter"这类缺字段失配。
package handlers

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"
)

// forwardedPaths：网关转发的内部路径表（tasks.go 各 handler 注释 + pyapi.GetTaskDetail +
// health.go 的 /readyz；healthz 网关本地回，不转发）。path 用 FastAPI 模板形式。
var forwardedPaths = []struct{ method, path string }{
	{"GET", "/readyz"}, // Ready 探针：Redis ping + Python /readyz 转发（health.go）
	{"POST", "/internal/v1/auth/token"},
	{"GET", "/internal/v1/projects"},
	{"POST", "/internal/v1/projects"},
	{"PUT", "/internal/v1/projects/{project_id}"},
	{"DELETE", "/internal/v1/projects/{project_id}"},
	{"GET", "/internal/v1/projects/{project_id}/chapters"},
	{"GET", "/internal/v1/projects/{project_id}/chapters/{chapter_id}"},
	{"PUT", "/internal/v1/projects/{project_id}/chapters/{chapter_id}/content"},
	{"POST", "/internal/v1/projects/{project_id}/chapters/{chapter_id}/correct-memory"},
	{"DELETE", "/internal/v1/projects/{project_id}/chapters/{chapter_id}"},
	{"GET", "/internal/v1/projects/{project_id}/chapters/{chapter_id}/versions"},
	{"POST", "/internal/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version}/restore"},
	{"POST", "/internal/v1/projects/{project_id}/global-audit"},
	{"GET", "/internal/v1/projects/{project_id}/global-audit"},
	{"GET", "/internal/v1/projects/{project_id}/global-audit/{report_id}"},
	{"GET", "/internal/v1/projects/{project_id}/settings"},
	{"PUT", "/internal/v1/projects/{project_id}/settings"},
	{"POST", "/internal/v1/projects/{project_id}/settings/models"},
	{"POST", "/internal/v1/projects/{project_id}/settings/test-connection"},
	{"GET", "/internal/v1/environment"},
	{"PUT", "/internal/v1/environment"},
	{"POST", "/internal/v1/environment/models"},
	{"POST", "/internal/v1/environment/test-connection"},
	{"POST", "/internal/v1/environment/test-rankings"},
	{"GET", "/internal/v1/skill-presets"},
	{"GET", "/internal/v1/genre-packs"},
	{"PUT", "/internal/v1/projects/{project_id}/genre-pack"},
	{"POST", "/internal/v1/projects/{project_id}/genre-pack/restore"},
	{"POST", "/internal/v1/projects/{project_id}/style-samples"},
	{"PUT", "/internal/v1/projects/{project_id}/style-profile"},
	{"POST", "/internal/v1/projects/{project_id}/setup-draft"},
	{"PUT", "/internal/v1/projects/{project_id}/setup"},
	// 整书大纲（§11 建书 ③：草稿 / 确认落库 / 读取）
	{"POST", "/internal/v1/projects/{project_id}/outline-draft"},
	{"PUT", "/internal/v1/projects/{project_id}/outline"},
	{"GET", "/internal/v1/projects/{project_id}/outline"},
	{"GET", "/internal/v1/projects/{project_id}/world"},
	{"GET", "/internal/v1/projects/{project_id}/characters"},
	{"GET", "/internal/v1/projects/{project_id}/characters/{character_id}/state-history"},
	{"GET", "/internal/v1/projects/{project_id}/events"},
	{"GET", "/internal/v1/projects/{project_id}/entities"},
	{"GET", "/internal/v1/projects/{project_id}/graph"},
	{"GET", "/internal/v1/projects/{project_id}/foreshadows"},
	{"GET", "/internal/v1/rankings"},
	{"GET", "/internal/v1/tasks/{task_id}"},
	{"POST", "/internal/v1/tasks/{task_id}/plan/confirm"},
	{"POST", "/internal/v1/tasks/{task_id}/pause"},
	{"POST", "/internal/v1/tasks/{task_id}/resume"},
	{"POST", "/internal/v1/tasks/{task_id}/cancel"},
	{"GET", "/internal/v1/projects/{project_id}/tasks"},
	{"GET", "/internal/v1/projects/{project_id}/candidates"},
	{"POST", "/internal/v1/projects/{project_id}/candidates/{candidate_id}/confirm"},
	{"POST", "/internal/v1/projects/{project_id}/candidates/{candidate_id}/reject"},
	{"GET", "/internal/v1/projects/{project_id}/lessons"},
	{"POST", "/internal/v1/projects/{project_id}/lessons/{lesson_id}/confirm"},
	{"POST", "/internal/v1/projects/{project_id}/lessons/{lesson_id}/reject"},
}

func loadContract(t *testing.T) map[string]any {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("无法定位测试源码路径")
	}
	p := filepath.Join(filepath.Dir(file), "..", "..", "..", "spec", "api-openapi.json")
	raw, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("读契约文件失败: %v（先 myink contract export 并提交 spec/api-openapi.json）", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("契约 JSON 解析失败: %v", err)
	}
	return doc
}

var pathParamRe = regexp.MustCompile(`\{[^}]*\}`)

// normPath：{project_id}/{task_id} 等模板参数名归一为 {}——契约方改参数名不误报路径漂移。
func normPath(p string) string {
	return pathParamRe.ReplaceAllString(p, "{}")
}

// resolveSchema：解 $ref / anyOf（pydantic v2 的 `X | None` 会产 anyOf=[{$ref},{type:null}]，
// 取首个非 null 分支）到含 properties 的最终 schema。
func resolveSchema(schema map[string]any, components map[string]any) map[string]any {
	for i := 0; i < 8; i++ { // 防循环 $ref
		if ref, ok := schema["$ref"].(string); ok {
			comps, _ := components["schemas"].(map[string]any)
			next, ok := comps[ref[strings.LastIndex(ref, "/")+1:]].(map[string]any)
			if !ok {
				return schema
			}
			schema = next
			continue
		}
		if arr, ok := schema["anyOf"].([]any); ok {
			var picked map[string]any
			for _, item := range arr {
				m, _ := item.(map[string]any)
				if m == nil || m["type"] == "null" {
					continue
				}
				picked = m
				break
			}
			if picked != nil {
				schema = picked
				continue
			}
		}
		break
	}
	return schema
}

// checkValue：双向键检查——契约 required ⊆ 假值键（缺字段失败）且假值键 ⊆ properties
// （多/错字段失败）；数组递归 items；无 properties（自由形状/标量）跳过。
func checkValue(t *testing.T, value any, schema map[string]any, components map[string]any, where string) {
	t.Helper()
	if ty, ok := schema["type"].(string); ok && ty == "array" {
		items, _ := schema["items"].(map[string]any)
		if items == nil {
			return
		}
		list, ok := value.([]any)
		if !ok {
			return
		}
		for _, v := range list {
			checkValue(t, v, items, components, where+"[]")
		}
		return
	}
	schema = resolveSchema(schema, components)
	props, _ := schema["properties"].(map[string]any)
	if props == nil {
		return // 自由形状（additionalProperties）或标量：不校验
	}
	doc, ok := value.(map[string]any)
	if !ok {
		return
	}
	if req, ok := schema["required"].([]any); ok {
		for _, r := range req {
			rk, _ := r.(string)
			if _, exists := doc[rk]; !exists {
				t.Errorf("%s: 缺契约必填字段 %q", where, rk)
			}
		}
	}
	for k, v := range doc {
		p, ok := props[k].(map[string]any)
		if !ok {
			t.Errorf("%s: 假服务多出契约未声明字段 %q", where, k)
			continue
		}
		checkValue(t, v, p, components, where+"."+k)
	}
}

func TestContractHasAllForwardedPaths(t *testing.T) {
	doc := loadContract(t)
	paths, _ := doc["paths"].(map[string]any)
	if paths == nil {
		t.Fatal("契约缺 paths")
	}
	// 契约路径模板参数名（{project_id}/{task_id}…）与网关路径表一致，归一后对比
	normPaths := make(map[string]map[string]any, len(paths))
	for k, v := range paths {
		if ops, ok := v.(map[string]any); ok {
			normPaths[normPath(k)] = ops
		}
	}
	for _, r := range forwardedPaths {
		ops, ok := normPaths[normPath(r.path)]
		if !ok {
			t.Errorf("网关转发路径 %s 不在契约里（Python 侧可能已改路由，网关未同步）", r.path)
			continue
		}
		if _, ok := ops[strings.ToLower(r.method)]; !ok {
			t.Errorf("网关转发 %s %s 在契约里缺 method", r.method, r.path)
		}
	}
}

func TestFakePyFieldsWithinContractSchema(t *testing.T) {
	doc := loadContract(t)
	components, _ := doc["components"].(map[string]any)
	paths, _ := doc["paths"].(map[string]any)
	py := fakePy()

	fixtures := []struct {
		name   string
		method string
		req    string // 请求假服务的路径（契约模板的实例值）
		ctPath string // 契约模板路径
	}{
		{"项目列表", "GET", "/internal/v1/projects", "/internal/v1/projects"},
		{"章节列表", "GET", "/internal/v1/projects/p1/chapters", "/internal/v1/projects/{project_id}/chapters"},
		{"任务详情", "GET", "/internal/v1/tasks/detail-test", "/internal/v1/tasks/{task_id}"},
		{"批次暂停", "POST", "/internal/v1/tasks/batch-x/pause", "/internal/v1/tasks/{task_id}/pause"},
		{"签发 token", "POST", "/internal/v1/auth/token", "/internal/v1/auth/token"},
		{"扫榜", "GET", "/internal/v1/rankings", "/internal/v1/rankings"},
		{"关系图谱", "GET", "/internal/v1/projects/p1/graph", "/internal/v1/projects/{project_id}/graph"},
		{"伏笔池", "GET", "/internal/v1/projects/p1/foreshadows", "/internal/v1/projects/{project_id}/foreshadows"},
		{"整书大纲", "GET", "/internal/v1/projects/p1/outline", "/internal/v1/projects/{project_id}/outline"},
	}
	for _, f := range fixtures {
		t.Run(f.name, func(t *testing.T) {
			req, err := http.NewRequest(f.method, py.URL+f.req, nil)
			if err != nil {
				t.Fatalf("构造请求失败: %v", err)
			}
			resp, err := py.Client().Do(req)
			if err != nil {
				t.Fatalf("请求假服务失败: %v", err)
			}
			defer resp.Body.Close()
			raw, err := io.ReadAll(resp.Body)
			if err != nil {
				t.Fatalf("读假服务响应失败: %v", err)
			}

			ops, _ := paths[f.ctPath].(map[string]any)
			if ops == nil {
				t.Fatalf("契约缺路径 %s", f.ctPath)
			}
			m, _ := ops[strings.ToLower(f.method)].(map[string]any)
			if m == nil {
				t.Fatalf("契约缺 method %s %s", f.method, f.ctPath)
			}
			resp200, _ := m["responses"].(map[string]any)["200"].(map[string]any)
			schema, _ := resp200["content"].(map[string]any)["application/json"].(map[string]any)["schema"].(map[string]any)
			if schema == nil {
				t.Fatalf("契约缺 200 schema: %s %s", f.method, f.ctPath)
			}
			var body any
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Fatalf("假服务响应解析失败: %v", err)
			}
			checkValue(t, body, schema, components, f.req)
		})
	}
}
